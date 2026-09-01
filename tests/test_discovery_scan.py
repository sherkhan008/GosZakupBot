"""Regression tests for the core fix: a tender that exists but whose
lastUpdateDate has not changed recently must still be found once its deadline
enters the 5-72h window, via the periodic discovery scan -- NOT just via
incremental sync's lastUpdateDate delta (which would never see it).
"""
from __future__ import annotations

import json
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
from app.services.monitor import LAST_DISCOVERY_SCAN_KEY, MonitorService
from app.telegram.client import TelegramClient

KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"


def _raw_lot(lot_id: int, name: str, end_date_iso: str, last_update: str, amount: float = 150000.0) -> dict[str, Any]:
    return {
        "id": lot_id,
        "lotNumber": f"LOT-{lot_id}",
        "nameRu": name,
        "nameKz": None,
        "descriptionRu": None,
        "descriptionKz": None,
        "amount": amount,
        "lastUpdateDate": last_update,
        "indexDate": last_update,
        "trdBuyId": 900 + lot_id,
        "TrdBuy": {
            "id": 900 + lot_id,
            "numberAnno": f"900{lot_id}-1",
            "nameRu": "Тестовый конкурс",
            "nameKz": None,
            "startDate": last_update,
            "endDate": end_date_iso,
            "publishDate": last_update,
            "refBuyStatusId": 210,
        },
        "Plans": [],
    }


class KeywordOnlyTransport(httpx.AsyncBaseTransport):
    """Simulates the real-world scenario: a tender is only findable through
    a keyword search (nameDescriptionRu/Kz), because it's been sitting
    untouched since publication -- a pure lastUpdateDate-range query (as used
    by incremental sync) returns nothing for it, exactly like the live
    GosZakup API would for a record outside the delta window.
    """

    def __init__(self, lots_by_keyword: dict[str, list[dict[str, Any]]]):
        self.lots_by_keyword = lots_by_keyword
        self.sent_messages: list[dict[str, Any]] = []
        self.date_only_query_count = 0
        self.keyword_query_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "goszakup" in url:
            body = json.loads(request.content)
            filt = body.get("variables", {}).get("filter") or {}
            keyword = filt.get("nameDescriptionRu") or filt.get("nameDescriptionKz")
            if keyword is not None:
                self.keyword_query_count += 1
                return httpx.Response(
                    200, json={"data": {"Lots": self.lots_by_keyword.get(keyword, [])}}
                )
            # date-only (incremental sync) query -- this tender is invisible here
            self.date_only_query_count += 1
            return httpx.Response(200, json={"data": {"Lots": []}})
        if "sendMessage" in url:
            body = json.loads(request.content)
            self.sent_messages.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent_messages)}})
        if "getMe" in url or "getUpdates" in url:
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(f"Unexpected request: {url}")


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
        discovery_concurrency=4,
        min_amount_kzt=100000,
        log_level="INFO",
        keywords_path=KEYWORDS_PATH,
    )


async def _build(settings: Settings, transport: httpx.AsyncBaseTransport) -> tuple[MonitorService, Repository, GoszakupClient, TelegramClient]:
    conn = await init_db(settings.database_path)
    repo = Repository(conn)
    kf = KeywordFilter.from_yaml(settings.keywords_path)
    goszakup = GoszakupClient(settings.goszakup_graphql_url, settings.goszakup_api_token)
    goszakup._client = httpx.AsyncClient(transport=transport)
    telegram = TelegramClient(settings.telegram_api_base, settings.telegram_bot_token)
    telegram._client = httpx.AsyncClient(transport=transport)
    monitor = MonitorService(settings, repo, goszakup, telegram, kf, settings.telegram_chat_id)
    monitor._test_conn = conn  # type: ignore[attr-defined]
    return monitor, repo, goszakup, telegram


async def _close(monitor, goszakup, telegram) -> None:
    await monitor._test_conn.close()  # type: ignore[attr-defined]
    await goszakup.close()
    await telegram.close()


async def test_incremental_sync_alone_misses_a_tender_with_stale_lastUpdateDate(settings: Settings):
    now = datetime.now(timezone.utc)
    eligible_end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    stale_update = (now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    lot = _raw_lot(55, "Стеллаж металлический архивный", eligible_end, stale_update)
    transport = KeywordOnlyTransport({"стеллаж": [lot]})
    monitor, repo, goszakup, telegram = await _build(settings, transport)

    await monitor.run_incremental_sync()

    assert len(transport.sent_messages) == 0
    assert await repo.is_sent(55) is False  # never even seen

    await _close(monitor, goszakup, telegram)


async def test_discovery_scan_rediscovers_and_sends_the_same_tender(settings: Settings):
    now = datetime.now(timezone.utc)
    eligible_end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    stale_update = (now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    lot = _raw_lot(55, "Стеллаж металлический архивный", eligible_end, stale_update)
    transport = KeywordOnlyTransport({"стеллаж": [lot]})
    monitor, repo, goszakup, telegram = await _build(settings, transport)

    # Incremental sync (as in production, run every cycle) misses it...
    await monitor.run_incremental_sync()
    assert len(transport.sent_messages) == 0

    # ...but the periodic discovery scan finds it via keyword search alone,
    # ignoring lastUpdateDate for the local match decision, and sends it.
    await monitor.run_discovery_scan()

    assert transport.keyword_query_count > 0
    assert len(transport.sent_messages) == 1
    assert "Стеллаж металлический архивный" in transport.sent_messages[0]["text"]

    assert await repo.is_sent(55) is True

    # A subsequent discovery scan re-finding the same lot must NOT resend it.
    transport.sent_messages.clear()
    await monitor.run_discovery_scan()
    assert len(transport.sent_messages) == 0
    assert await repo.is_sent(55) is True

    await _close(monitor, goszakup, telegram)


async def test_discovery_scan_rediscovers_a_tender_once_it_enters_the_window(settings: Settings):
    """A tender first discovered (e.g. by an earlier scan) with >72h remaining
    is skipped with nothing stored (no 'pending' persistence in the minimal
    schema); a later discovery scan, run when its deadline has now entered
    5-72h, must find and send it via keyword search alone -- even though
    lastUpdateDate never changed in between.
    """
    now = datetime.now(timezone.utc)
    far_future_end = (now + timedelta(hours=200)).strftime("%Y-%m-%d %H:%M:%S")
    stale_update = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    lot_far = _raw_lot(66, "Сейф офисный металлический", far_future_end, stale_update)
    transport = KeywordOnlyTransport({"сейф": [lot_far]})
    monitor, repo, goszakup, telegram = await _build(settings, transport)

    await monitor.run_discovery_scan()
    assert await repo.is_sent(66) is False
    assert len(transport.sent_messages) == 0

    # Time passes; the SAME tender's deadline is now inside 5-72h (endDate
    # itself hasn't changed -- only "now" has moved closer to it -- but to
    # keep the test deterministic we simulate the passage of time by serving
    # a refreshed endDate, exactly as GosZakup would return unchanged data
    # that simply reads differently against a later "now").
    eligible_end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lot_eligible = _raw_lot(66, "Сейф офисный металлический", eligible_end, stale_update)
    transport.lots_by_keyword["сейф"] = [lot_eligible]

    await monitor.run_discovery_scan()
    assert await repo.is_sent(66) is True
    assert len(transport.sent_messages) == 1

    await _close(monitor, goszakup, telegram)


async def test_lastUpdateDate_30_days_old_does_not_block_eligibility(settings: Settings):
    """Exact business-rule regression test: nameRu='Металлический шкаф',
    amount=850000, endDate=now+23h, lastUpdateDate=now-30 days. Keyword +
    amount + remaining time all pass -> MUST be sent, regardless of how old
    lastUpdateDate is.
    """
    now = datetime.now(timezone.utc)
    end_date = (now + timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")
    stale_update = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    lot = _raw_lot(70, "Металлический шкаф", end_date, stale_update, amount=850000)
    transport = KeywordOnlyTransport({"шкаф": [lot]})
    monitor, repo, goszakup, telegram = await _build(settings, transport)

    await monitor.run_discovery_scan()

    assert len(transport.sent_messages) == 1
    assert "Металлический шкаф" in transport.sent_messages[0]["text"]
    assert await repo.is_sent(70) is True

    await _close(monitor, goszakup, telegram)


async def test_lastUpdateDate_365_days_old_does_not_block_eligibility(settings: Settings):
    """Same as above but with lastUpdateDate a full year old: if the API
    still returns the lot (nothing in our own filter would exclude it, since
    discovery scan sends no lastUpdateDate bound at all) and keyword + amount
    + remaining time still pass, it MUST be sent.
    """
    now = datetime.now(timezone.utc)
    end_date = (now + timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")
    ancient_update = (now - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
    lot = _raw_lot(71, "Металлический шкаф", end_date, ancient_update, amount=850000)
    transport = KeywordOnlyTransport({"шкаф": [lot]})
    monitor, repo, goszakup, telegram = await _build(settings, transport)

    await monitor.run_discovery_scan()

    assert len(transport.sent_messages) == 1
    assert await repo.is_sent(71) is True

    await _close(monitor, goszakup, telegram)


async def test_discovery_scan_sends_no_lastUpdateDate_filter_at_all(settings: Settings):
    """Verify at the transport level that discovery scan's queries carry no
    lastUpdateDate key whatsoever -- not an empty one, not a wide one, none.
    """
    now = datetime.now(timezone.utc)
    end_date = (now + timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")
    ancient_update = (now - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
    lot = _raw_lot(72, "Металлический шкаф", end_date, ancient_update, amount=850000)

    seen_filters: list[dict] = []

    class RecordingTransport(KeywordOnlyTransport):
        async def handle_async_request(self, request):
            if "goszakup" in str(request.url):
                body = json.loads(request.content)
                seen_filters.append(body.get("variables", {}).get("filter") or {})
            return await super().handle_async_request(request)

    transport = RecordingTransport({"шкаф": [lot]})
    monitor, repo, goszakup, telegram = await _build(settings, transport)

    await monitor.run_discovery_scan()

    assert len(seen_filters) > 0
    for filt in seen_filters:
        assert "lastUpdateDate" not in filt

    await _close(monitor, goszakup, telegram)


async def test_maybe_run_discovery_scan_respects_interval(settings: Settings):
    transport = KeywordOnlyTransport({})
    monitor, repo, goszakup, telegram = await _build(settings, transport)

    # Never run before -> runs immediately.
    await monitor.maybe_run_discovery_scan()
    assert transport.keyword_query_count > 0
    first_count = transport.keyword_query_count

    # Just ran -> not due again yet.
    await monitor.maybe_run_discovery_scan()
    assert transport.keyword_query_count == first_count

    # Force the checkpoint into the past beyond the interval -> due again.
    past = datetime.now(timezone.utc) - timedelta(minutes=settings.discovery_scan_interval_minutes + 1)
    await repo.set_app_state(LAST_DISCOVERY_SCAN_KEY, past.isoformat())
    await monitor.maybe_run_discovery_scan()
    assert transport.keyword_query_count > first_count

    await _close(monitor, goszakup, telegram)
