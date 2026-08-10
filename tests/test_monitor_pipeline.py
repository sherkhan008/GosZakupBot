"""End-to-end pipeline test using mocked GosZakup/Telegram HTTP responses --
no real network access. Verifies: parsing, keyword filter as final authority,
deadline classification, duplicate protection, and message sending only for
eligible tenders.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.database.db import init_db
from app.database.repository import Repository
from app.filters.keyword_filter import KeywordFilter
from app.goszakup.client import GoszakupClient
from app.services.monitor import MonitorService
from app.telegram.client import TelegramClient

KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"


def _make_lot(lot_id: int, name: str, end_date_iso: str, amount: float = 100000.0) -> dict[str, Any]:
    return {
        "id": lot_id,
        "lotNumber": f"LOT-{lot_id}",
        "nameRu": name,
        "nameKz": None,
        "descriptionRu": None,
        "descriptionKz": None,
        "amount": amount,
        "lastUpdateDate": "2026-08-10 10:00:00",
        "indexDate": "2026-08-10 10:00:00",
        "trdBuyId": 900 + lot_id,
        "TrdBuy": {
            "id": 900 + lot_id,
            "numberAnno": f"900{lot_id}-1",
            "nameRu": "Тестовый конкурс",
            "nameKz": None,
            "startDate": "2026-08-01 00:00:00",
            "endDate": end_date_iso,
            "publishDate": "2026-08-01 00:00:00",
            "refBuyStatusId": 210,
        },
        "Plans": [
            {
                "descRu": None,
                "descKz": None,
                "extraDescRu": None,
                "extraDescKz": None,
                "PlansKato": [
                    {
                        "fullDeliveryPlaceNameRu": "г. Астана",
                        "fullDeliveryPlaceNameKz": None,
                        "refKatoCode": "710000000",
                        "RefKato": {
                            "nameRu": "Астана",
                            "nameKz": None,
                            "fullNameRu": "г. Астана",
                            "fullNameKz": None,
                            "code": "710000000",
                        },
                    }
                ],
            }
        ],
    }


class RecordingTransport(httpx.AsyncBaseTransport):
    """Routes GosZakup GraphQL calls and Telegram calls to canned responses."""

    def __init__(self, lots_by_query: list[dict[str, Any]]):
        self.lots = lots_by_query
        self.sent_messages: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "goszakup" in str(request.url):
            # Every query (keyword batches x 2 langs) returns the same fixed
            # candidate set once, then empty (pagination end / subsequent batches).
            if not self._served:
                self._served = True
                return httpx.Response(200, json={"data": {"Lots": self.lots}})
            return httpx.Response(200, json={"data": {"Lots": []}})
        if "sendMessage" in str(request.url):
            import json as _json

            body = _json.loads(request.content)
            self.sent_messages.append(body)
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": len(self.sent_messages)}}
            )
        if "getMe" in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": {"username": "test_bot"}})
        if "getUpdates" in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(f"Unexpected request: {request.url}")

    _served = False


@pytest.fixture
def settings(tmp_path) -> Settings:
    from zoneinfo import ZoneInfo

    return Settings(
        goszakup_api_token="test-token",
        telegram_bot_token="test-bot-token",
        telegram_chat_id="12345",
        check_interval_seconds=300,
        min_hours_remaining=5,
        max_hours_remaining=72,
        app_timezone_name="Asia/Qyzylorda",
        database_path=tmp_path / "test.db",
        bootstrap_lookback_days=90,
        sync_overlap_minutes=10,
        log_level="INFO",
        keywords_path=KEYWORDS_PATH,
    )


async def test_eligible_match_is_sent_and_never_resent(settings: Settings):
    now = datetime.now(timezone.utc)
    eligible_end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(1, "Стеллаж металлический архивный", eligible_end)]

    transport = RecordingTransport(lots)

    conn = await init_db(settings.database_path)
    repo = Repository(conn)
    kf = KeywordFilter.from_yaml(settings.keywords_path)

    goszakup = GoszakupClient(settings.goszakup_graphql_url, settings.goszakup_api_token)
    goszakup._client = httpx.AsyncClient(transport=transport)
    telegram = TelegramClient(settings.telegram_api_base, settings.telegram_bot_token)
    telegram._client = httpx.AsyncClient(transport=transport)

    monitor = MonitorService(settings, repo, goszakup, telegram, kf, settings.telegram_chat_id)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 1
    sent_text = transport.sent_messages[0]["text"]
    assert "Стеллаж металлический архивный" in sent_text
    assert "г. Астана" in sent_text

    row = await repo.get_tender(1)
    assert row["status"] == "sent"

    # Running sync again (lot unchanged, still returned by the mocked API)
    # must NOT send a second notification.
    transport._served = False
    await monitor.run_incremental_sync()
    assert len(transport.sent_messages) == 1

    await conn.close()
    await goszakup.close()
    await telegram.close()


async def test_non_matching_lot_is_ignored(settings: Settings):
    now = datetime.now(timezone.utc)
    end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(2, "Обычный телевизор LED", end)]
    transport = RecordingTransport(lots)

    conn = await init_db(settings.database_path)
    repo = Repository(conn)
    kf = KeywordFilter.from_yaml(settings.keywords_path)
    goszakup = GoszakupClient(settings.goszakup_graphql_url, settings.goszakup_api_token)
    goszakup._client = httpx.AsyncClient(transport=transport)
    telegram = TelegramClient(settings.telegram_api_base, settings.telegram_bot_token)
    telegram._client = httpx.AsyncClient(transport=transport)

    monitor = MonitorService(settings, repo, goszakup, telegram, kf, settings.telegram_chat_id)
    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 0
    assert await repo.get_tender(2) is None

    await conn.close()
    await goszakup.close()
    await telegram.close()


async def test_pending_lot_beyond_72h_is_stored_but_not_sent(settings: Settings):
    now = datetime.now(timezone.utc)
    far_future = (now + timedelta(hours=200)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(3, "Складской стеллаж усиленный", far_future)]
    transport = RecordingTransport(lots)

    conn = await init_db(settings.database_path)
    repo = Repository(conn)
    kf = KeywordFilter.from_yaml(settings.keywords_path)
    goszakup = GoszakupClient(settings.goszakup_graphql_url, settings.goszakup_api_token)
    goszakup._client = httpx.AsyncClient(transport=transport)
    telegram = TelegramClient(settings.telegram_api_base, settings.telegram_bot_token)
    telegram._client = httpx.AsyncClient(transport=transport)

    monitor = MonitorService(settings, repo, goszakup, telegram, kf, settings.telegram_chat_id)
    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 0
    row = await repo.get_tender(3)
    assert row is not None
    assert row["status"] == "pending"

    await conn.close()
    await goszakup.close()
    await telegram.close()
