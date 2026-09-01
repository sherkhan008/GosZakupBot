"""Memory-audit regression tests for the streaming fetch/ingest pipeline.

Confirms the fix for the production OOM crash: every fetch path (incremental
sync's date-only query, and the 92-keyword x 2-language discovery/bootstrap
search) streams raw lot payloads one at a time through MonitorService rather
than accumulating them into a list/dict first, so memory stays roughly
constant regardless of how many lots a query returns. Deliberately avoids
allocating real huge amounts of RAM -- a few thousand lightweight synthetic
lots is enough to prove the structural claim (return value size, request
concurrency, cross-query dedup) without a slow test.
"""
from __future__ import annotations

import asyncio
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
from app.services.monitor import MonitorService
from app.telegram.client import TelegramClient

KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"


def _settings(tmp_path, *, discovery_concurrency: int = 4) -> Settings:
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
        discovery_concurrency=discovery_concurrency,
        min_amount_kzt=100000,
        log_level="INFO",
        keywords_path=KEYWORDS_PATH,
    )


async def _build_monitor(
    settings: Settings, transport: httpx.AsyncBaseTransport, keyword_filter: KeywordFilter
):
    conn = await init_db(Path(":memory:"))
    repo = Repository(conn)
    goszakup = GoszakupClient(settings.goszakup_graphql_url, settings.goszakup_api_token)
    goszakup._client = httpx.AsyncClient(transport=transport)
    telegram = TelegramClient(settings.telegram_api_base, settings.telegram_bot_token)
    telegram._client = httpx.AsyncClient(transport=transport)
    monitor = MonitorService(settings, repo, goszakup, telegram, keyword_filter, settings.telegram_chat_id)
    return monitor, repo, conn, goszakup, telegram


async def _close(conn, goszakup, telegram) -> None:
    await conn.close()
    await goszakup.close()
    await telegram.close()


STATS_KEYS = {
    "keywords_loaded",
    "api_requests",
    "api_failures",
    "lots_received",
    "unique_lots",
    "keyword_matches",
    "rejected_amount",
    "rejected_deadline",
    "pending",
    "already_sent",
    "telegram_sent",
    "missing_end_date",
}


# --------------------------------------------------------------------------- 1. streaming, not accumulating


class _DescendingOrderTransport(httpx.AsyncBaseTransport):
    """Serves a large descending-id lot set across many pages, exactly like
    the real GosZakup API (see tests/test_pagination.py). Lot titles never
    match any real keyword, so this test stays focused purely on
    fetch/pagination volume, never touching ingest_lot/Telegram/DB.
    """

    def __init__(self, all_ids_desc: list[int]):
        self.all_ids_desc = all_ids_desc
        self.request_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        body = json.loads(request.content)
        variables = body["variables"]
        after = variables.get("after")
        limit = variables["limit"]

        candidates = self.all_ids_desc if after is None else [i for i in self.all_ids_desc if i < after]
        page_ids = candidates[:limit]
        lots = [{"id": i, "nameRu": f"Lot {i}", "nameKz": None} for i in page_ids]
        return httpx.Response(200, json={"data": {"Lots": lots}})


async def test_process_date_range_query_streams_many_pages_with_constant_size_result(tmp_path):
    """A large multi-page result (simulating the historical 160k-lot
    incremental catch-up, at reduced scale) must be processed page-by-page:
    the ONLY thing ever returned is a small, fixed-shape stats dict -- never
    a list/dict whose size grows with the number of lots streamed.
    """
    total_lots = 3000  # 15 pages at PAGE_LIMIT=200
    all_ids = list(range(100_000, 100_000 - total_lots, -1))
    transport = _DescendingOrderTransport(all_ids)
    settings = _settings(tmp_path)
    kf = KeywordFilter(["стеллаж"])  # never matches "Lot <id>" titles
    monitor, repo, conn, goszakup, telegram = await _build_monitor(settings, transport, kf)

    stats = await monitor.process_date_range_query(("2020-01-01 00:00:00", "2030-01-01 00:00:00"))

    assert stats["lots_received"] == total_lots
    assert stats["unique_lots"] == total_lots
    assert stats["keyword_matches"] == 0  # never matched, ingest_lot never touched
    assert transport.request_count > 1  # genuinely paginated across multiple requests
    assert set(stats.keys()) == STATS_KEYS  # fixed shape, independent of total_lots

    await _close(conn, goszakup, telegram)


# --------------------------------------------------------------------------- 2. bounded concurrency


class _ConcurrencyTrackingTransport(httpx.AsyncBaseTransport):
    """Records the maximum number of simultaneously in-flight requests, by
    sleeping briefly inside each request so overlapping calls actually
    overlap in the event loop.
    """

    def __init__(self, delay: float = 0.02):
        self.delay = delay
        self._current = 0
        self.max_concurrent = 0
        self.request_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._current += 1
        self.max_concurrent = max(self.max_concurrent, self._current)
        self.request_count += 1
        try:
            await asyncio.sleep(self.delay)
            return httpx.Response(200, json={"data": {"Lots": []}})
        finally:
            self._current -= 1


async def test_run_keyword_search_never_exceeds_configured_concurrency(tmp_path):
    transport = _ConcurrencyTrackingTransport(delay=0.02)
    settings = _settings(tmp_path, discovery_concurrency=3)
    kf = KeywordFilter([f"keyword{i}" for i in range(10)])  # 10 keywords x 2 languages = 20 queries
    monitor, repo, conn, goszakup, telegram = await _build_monitor(settings, transport, kf)

    stats = await monitor.run_keyword_search(None, label="TEST")

    assert stats["api_requests"] == 20
    assert transport.max_concurrent <= 3  # never exceeds settings.discovery_concurrency

    await _close(conn, goszakup, telegram)


# --------------------------------------------------------------------------- 3. cross-keyword dedup


def _raw_lot(lot_id: int, name: str, end_date_iso: str, amount: float) -> dict[str, Any]:
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
        "Plans": [],
    }


class _SameLotEveryQueryTransport(httpx.AsyncBaseTransport):
    """Every keyword/language query returns the SAME single lot (as if it
    matched both keywords), and Telegram sendMessage is handled separately --
    used to prove cross-query dedup prevents a duplicate send.
    """

    def __init__(self, lot: dict[str, Any]):
        self.lot = lot
        self.sent_messages: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "sendMessage" in url:
            body = json.loads(request.content)
            self.sent_messages.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent_messages)}})
        if "goszakup" in url:
            return httpx.Response(200, json={"data": {"Lots": [self.lot]}})
        raise AssertionError(f"Unexpected request: {url}")


async def test_duplicate_lot_across_keywords_sends_telegram_exactly_once(tmp_path):
    now = datetime.now(timezone.utc)
    eligible_end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lot = _raw_lot(101, "Стеллаж металлический архивный", eligible_end, amount=850000)
    transport = _SameLotEveryQueryTransport(lot)
    settings = _settings(tmp_path)
    # Both keywords match this lot's title -- the same lot_id therefore comes
    # back from all 4 (2 keywords x 2 languages) queries.
    kf = KeywordFilter(["стеллаж", "стеллажи"])
    monitor, repo, conn, goszakup, telegram = await _build_monitor(settings, transport, kf)

    stats = await monitor.run_keyword_search(None, label="TEST")

    assert stats["unique_lots"] == 1  # deduped across the 4 queries
    assert stats["telegram_sent"] == 1
    assert len(transport.sent_messages) == 1
    assert await repo.is_sent(101) is True

    await _close(conn, goszakup, telegram)


# --------------------------------------------------------------------------- 4. retry does not leak tasks


class _FlakyThenOkTransport(httpx.AsyncBaseTransport):
    """First request raises a transient network error; every request after
    that succeeds with an empty page."""

    def __init__(self):
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        if self.call_count == 1:
            raise httpx.ReadTimeout("simulated transient network error")
        return httpx.Response(200, json={"data": {"Lots": []}})


async def test_transient_network_error_is_retried_without_leaking_tasks():
    import app.goszakup.client as goszakup_client_module

    transport = _FlakyThenOkTransport()
    client = GoszakupClient("https://ows.goszakup.gov.kz/v3/graphql", "test-token")
    client._client = httpx.AsyncClient(transport=transport)

    original_delays = goszakup_client_module.RETRY_DELAYS
    goszakup_client_module.RETRY_DELAYS = (0,) * len(original_delays)  # skip real sleeps in test
    try:
        tasks_before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

        lots = [raw async for raw in client.fetch_lots_paginated({}, limit=50)]

        tasks_after = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

        assert lots == []  # empty result, but recovered via retry rather than raising
        assert transport.call_count == 2  # one failure + one successful retry
        assert tasks_after == tasks_before  # no stray pending tasks left behind
    finally:
        goszakup_client_module.RETRY_DELAYS = original_delays
        await client.close()
