"""End-to-end pipeline test using mocked GosZakup/Telegram HTTP responses --
no real network access. Verifies: parsing, keyword filter as final authority,
deadline classification, duplicate protection, and message sending only for
eligible tenders. Under the minimal storage architecture, a lot is written to
the database ONLY after a successful send (sent_lots); everything outside the
5-72h window, and any lot never successfully sent, leaves no trace in the DB.
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


def _make_lot(lot_id: int, name: str, end_date_iso: str, amount: float = 150000.0) -> dict[str, Any]:
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

    def __init__(self, lots_by_query: list[dict[str, Any]], telegram_send_fails: bool = False):
        self.lots = lots_by_query
        self.sent_messages: list[dict[str, Any]] = []
        self.telegram_send_fails = telegram_send_fails
        self._served = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "goszakup" in str(request.url):
            # Every query (keyword batches x 2 langs) returns the same fixed
            # candidate set once, then empty (pagination end / subsequent batches).
            if not self._served:
                self._served = True
                return httpx.Response(200, json={"data": {"Lots": self.lots}})
            return httpx.Response(200, json={"data": {"Lots": []}})
        if "sendMessage" in str(request.url):
            if self.telegram_send_fails:
                # A non-retryable 4xx (not 429) so TelegramError raises
                # immediately instead of exercising the retry/backoff loop.
                return httpx.Response(400, json={"ok": False, "description": "Bad Request"})
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


@pytest.fixture
def settings(tmp_path) -> Settings:
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
        discovery_scan_interval_minutes=120,
        min_amount_kzt=100000,
        log_level="INFO",
        keywords_path=KEYWORDS_PATH,
    )


async def _build(settings: Settings, transport: httpx.AsyncBaseTransport):
    conn = await init_db(settings.database_path)
    repo = Repository(conn)
    kf = KeywordFilter.from_yaml(settings.keywords_path)
    goszakup = GoszakupClient(settings.goszakup_graphql_url, settings.goszakup_api_token)
    goszakup._client = httpx.AsyncClient(transport=transport)
    telegram = TelegramClient(settings.telegram_api_base, settings.telegram_bot_token)
    telegram._client = httpx.AsyncClient(transport=transport)
    monitor = MonitorService(settings, repo, goszakup, telegram, kf, settings.telegram_chat_id)
    return monitor, repo, conn, goszakup, telegram


async def _close(conn, goszakup, telegram) -> None:
    await conn.close()
    await goszakup.close()
    await telegram.close()


async def test_eligible_match_is_sent_and_never_resent(settings: Settings):
    now = datetime.now(timezone.utc)
    eligible_end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(1, "Стеллаж металлический архивный", eligible_end)]

    transport = RecordingTransport(lots)
    monitor, repo, conn, goszakup, telegram = await _build(settings, transport)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 1
    sent_text = transport.sent_messages[0]["text"]
    assert "Стеллаж металлический архивный" in sent_text
    assert "г. Астана" in sent_text
    assert await repo.is_sent(1) is True

    # Running sync again (lot unchanged, still returned by the mocked API)
    # must NOT send a second notification.
    transport._served = False
    await monitor.run_incremental_sync()
    assert len(transport.sent_messages) == 1

    await _close(conn, goszakup, telegram)


async def test_non_matching_lot_is_ignored(settings: Settings):
    now = datetime.now(timezone.utc)
    end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(2, "Обычный телевизор LED", end)]
    transport = RecordingTransport(lots)
    monitor, repo, conn, goszakup, telegram = await _build(settings, transport)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 0
    assert await repo.is_sent(2) is False

    await _close(conn, goszakup, telegram)


async def test_amount_at_exactly_threshold_is_rejected_and_not_stored(settings: Settings):
    now = datetime.now(timezone.utc)
    end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(4, "Стеллаж металлический архивный", end, amount=100000.0)]
    transport = RecordingTransport(lots)
    monitor, repo, conn, goszakup, telegram = await _build(settings, transport)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 0
    assert await repo.is_sent(4) is False  # rejected before ever being sent/stored

    await _close(conn, goszakup, telegram)


async def test_remaining_over_72h_is_skipped_and_not_stored(settings: Settings):
    now = datetime.now(timezone.utc)
    far_future = (now + timedelta(hours=200)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(3, "Складской стеллаж усиленный", far_future)]
    transport = RecordingTransport(lots)
    monitor, repo, conn, goszakup, telegram = await _build(settings, transport)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 0
    assert await repo.is_sent(3) is False

    await _close(conn, goszakup, telegram)


async def test_remaining_under_5h_is_skipped_and_not_stored(settings: Settings):
    now = datetime.now(timezone.utc)
    almost_gone = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(9, "Стеллаж металлический архивный", almost_gone)]
    transport = RecordingTransport(lots)
    monitor, repo, conn, goszakup, telegram = await _build(settings, transport)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 0
    assert await repo.is_sent(9) is False

    await _close(conn, goszakup, telegram)


async def test_already_expired_deadline_is_skipped_and_not_stored(settings: Settings):
    now = datetime.now(timezone.utc)
    already_past = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(10, "Стеллаж металлический архивный", already_past)]
    transport = RecordingTransport(lots)
    monitor, repo, conn, goszakup, telegram = await _build(settings, transport)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 0
    assert await repo.is_sent(10) is False

    await _close(conn, goszakup, telegram)


async def test_telegram_send_failure_is_not_stored_and_is_retried_next_cycle(settings: Settings):
    now = datetime.now(timezone.utc)
    eligible_end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(11, "Стеллаж металлический архивный", eligible_end)]
    transport = RecordingTransport(lots, telegram_send_fails=True)
    monitor, repo, conn, goszakup, telegram = await _build(settings, transport)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 0
    assert await repo.is_sent(11) is False  # failed send must never be recorded as sent

    # Next cycle: Telegram recovers -- the lot must be retried and sent.
    transport.telegram_send_fails = False
    transport._served = False
    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 1
    assert await repo.is_sent(11) is True

    await _close(conn, goszakup, telegram)


async def test_future_tender_later_enters_window_and_is_sent_by_discovery(settings: Settings):
    """A tender currently >72h out is skipped with nothing stored; once time
    passes and it enters the 5-72h window, the next discovery scan (source of
    truth, not any stored 'pending' row) must find and send it.
    """
    now = datetime.now(timezone.utc)
    far_future = (now + timedelta(hours=200)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(12, "Сейф офисный металлический", far_future)]
    transport = RecordingTransport(lots)
    monitor, repo, conn, goszakup, telegram = await _build(settings, transport)

    await monitor.run_discovery_scan()
    assert len(transport.sent_messages) == 0
    assert await repo.is_sent(12) is False

    # Simulate time passing: the same lot now reports an end_date inside 5-72h.
    eligible_end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    transport.lots = [_make_lot(12, "Сейф офисный металлический", eligible_end)]
    transport._served = False

    await monitor.run_discovery_scan()
    assert len(transport.sent_messages) == 1
    assert await repo.is_sent(12) is True

    await _close(conn, goszakup, telegram)


async def test_sent_message_uses_new_procurement_domain_and_real_trd_buy_id(settings: Settings):
    now = datetime.now(timezone.utc)
    eligible_end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots = [_make_lot(13, "Стеллаж металлический архивный", eligible_end)]
    transport = RecordingTransport(lots)
    monitor, repo, conn, goszakup, telegram = await _build(settings, transport)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 1
    sent_text = transport.sent_messages[0]["text"]
    assert "https://procurement.gov.kz/ru/announce/index/913" in sent_text  # trdBuyId = 900+13

    await _close(conn, goszakup, telegram)
