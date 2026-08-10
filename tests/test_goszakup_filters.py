"""Regression tests for the production bug: GosZakup V3 rejects
nameDescriptionRu/nameDescriptionKz as arrays ("Expected type String at
value.nameDescriptionRu; String cannot represent an array value").

These tests fail if ANY code path builds a filter where nameDescriptionRu or
nameDescriptionKz is a list, and verify the corrected bootstrap (one keyword
per request, concurrency-limited) and incremental (date-only, then local
matching against all 92 keywords) strategies.
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
from app.goszakup.queries import (
    NAME_DESCRIPTION_KZ,
    NAME_DESCRIPTION_RU,
    build_date_range_filter,
    build_single_keyword_filter,
)
from app.services.monitor import MonitorService
from app.telegram.client import TelegramClient

KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"


# --------------------------------------------------------------------------- filter builders


def test_single_keyword_filter_is_a_string_never_a_list():
    filt = build_single_keyword_filter(
        keyword="стеллаж", field=NAME_DESCRIPTION_RU, last_update_date_range=("a", "b")
    )
    assert isinstance(filt["nameDescriptionRu"], str)
    assert filt["nameDescriptionRu"] == "стеллаж"
    assert not isinstance(filt["nameDescriptionRu"], list)


def test_single_keyword_filter_kz_field_is_a_string():
    filt = build_single_keyword_filter(keyword="сөре", field=NAME_DESCRIPTION_KZ)
    assert isinstance(filt["nameDescriptionKz"], str)
    assert filt["nameDescriptionKz"] == "сөре"


def test_single_keyword_filter_rejects_non_string_keyword():
    with pytest.raises(TypeError):
        build_single_keyword_filter(keyword=["стеллаж", "стеллажи"], field=NAME_DESCRIPTION_RU)  # type: ignore[arg-type]


def test_single_keyword_filter_rejects_unknown_field():
    with pytest.raises(ValueError):
        build_single_keyword_filter(keyword="стеллаж", field="nameRu")


def test_date_range_filter_keeps_lastUpdateDate_as_a_two_element_list():
    filt = build_date_range_filter(("2026-05-12 19:02:14", "2026-08-10 19:02:14"))
    assert filt == {"lastUpdateDate": ["2026-05-12 19:02:14", "2026-08-10 19:02:14"]}
    assert isinstance(filt["lastUpdateDate"], list)
    assert len(filt["lastUpdateDate"]) == 2


def test_date_range_filter_never_includes_name_description_fields():
    filt = build_date_range_filter(("a", "b"))
    assert "nameDescriptionRu" not in filt
    assert "nameDescriptionKz" not in filt


# --------------------------------------------------------------------------- mocked bootstrap/incremental


def _make_lot(lot_id: int, name: str, end_date_iso: str) -> dict[str, Any]:
    return {
        "id": lot_id,
        "lotNumber": f"LOT-{lot_id}",
        "nameRu": name,
        "nameKz": None,
        "descriptionRu": None,
        "descriptionKz": None,
        "amount": 100000.0,
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


class RecordingGoszakupTransport(httpx.AsyncBaseTransport):
    """Records every GosZakup GraphQL request's `filter` variable and serves
    per-keyword fixture data so we can assert on exactly what was sent.
    """

    def __init__(self, lots_by_keyword: dict[str, list[dict[str, Any]]]):
        self.lots_by_keyword = lots_by_keyword
        self.seen_filters: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body.get("variables", {})
        filt = variables.get("filter") or {}
        self.seen_filters.append(filt)

        # HARD regression guard at the transport boundary: if this ever fires,
        # a real GosZakup server would reject the request outright.
        for field in ("nameDescriptionRu", "nameDescriptionKz"):
            if isinstance(filt.get(field), list):
                return httpx.Response(
                    200,
                    json={
                        "errors": [
                            {
                                "message": (
                                    f"Expected type String at value.{field}; "
                                    "String cannot represent an array value"
                                )
                            }
                        ]
                    },
                )

        keyword = filt.get("nameDescriptionRu") or filt.get("nameDescriptionKz")
        if keyword is not None:
            lots = self.lots_by_keyword.get(keyword, [])
        elif "lastUpdateDate" in filt and "id" not in filt:
            # date-only incremental query: return every configured lot once
            seen_ids: set[int] = set()
            lots = []
            for group in self.lots_by_keyword.values():
                for lot in group:
                    if lot["id"] not in seen_ids:
                        seen_ids.add(lot["id"])
                        lots.append(lot)
        else:
            lots = []

        # Only serve data once per distinct filter signature to satisfy the
        # "fewer than limit -> stop paginating" contract.
        return httpx.Response(200, json={"data": {"Lots": lots}})


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
        log_level="INFO",
        keywords_path=KEYWORDS_PATH,
    )


async def _build_monitor(settings: Settings, transport: httpx.AsyncBaseTransport) -> MonitorService:
    conn = await init_db(settings.database_path)
    repo = Repository(conn)
    kf = KeywordFilter.from_yaml(settings.keywords_path)
    goszakup = GoszakupClient(settings.goszakup_graphql_url, settings.goszakup_api_token)
    goszakup._client = httpx.AsyncClient(transport=transport)
    telegram = TelegramClient(settings.telegram_api_base, settings.telegram_bot_token)
    telegram._client = httpx.AsyncClient(transport=transport)
    monitor = MonitorService(settings, repo, goszakup, telegram, kf, settings.telegram_chat_id)
    monitor._test_conn = conn  # type: ignore[attr-defined]
    return monitor


async def _close_monitor(monitor: MonitorService) -> None:
    await monitor._test_conn.close()  # type: ignore[attr-defined]
    await monitor.goszakup.close()
    await monitor.telegram.close()


async def test_bootstrap_sends_one_keyword_string_per_request(settings: Settings):
    now = datetime.now(timezone.utc)
    end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots_by_keyword = {
        "стеллаж": [_make_lot(1, "Стеллаж металлический", end)],
        "стеллажи": [_make_lot(1, "Стеллаж металлический", end)],  # same lot, different keyword
        "сейф": [_make_lot(2, "Сейф офисный", end)],
    }
    transport = RecordingGoszakupTransport(lots_by_keyword)
    monitor = await _build_monitor(settings, transport)

    matches = await monitor.fetch_bootstrap_matches(("2026-05-12 00:00:00", "2026-08-10 00:00:00"))

    # Every single request's filter must satisfy the schema constraint.
    assert len(transport.seen_filters) > 0
    for filt in transport.seen_filters:
        for field in ("nameDescriptionRu", "nameDescriptionKz"):
            if field in filt:
                assert isinstance(filt[field], str), f"{field} was sent as {type(filt[field])}"
        assert isinstance(filt.get("lastUpdateDate"), list)
        assert len(filt["lastUpdateDate"]) == 2

    # One request per (keyword, language) pair -- 92 keywords x 2 languages.
    keyword_count = len(KeywordFilter.from_yaml(settings.keywords_path).keywords)
    assert len(transport.seen_filters) == keyword_count * 2

    # Lot 1 was returned by TWO different keyword searches but must be merged
    # into a single candidate by Lots.id.
    assert set(matches.keys()) == {1, 2}
    await _close_monitor(monitor)


async def test_incremental_sync_uses_date_only_filter_and_matches_locally(settings: Settings):
    now = datetime.now(timezone.utc)
    end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots_by_keyword = {"стеллаж": [_make_lot(5, "Стеллаж архивный", end)]}
    transport = RecordingGoszakupTransport(lots_by_keyword)
    monitor = await _build_monitor(settings, transport)

    matches = await monitor.fetch_incremental_matches(("2026-08-10 09:00:00", "2026-08-10 10:00:00"))

    assert len(transport.seen_filters) == 1
    filt = transport.seen_filters[0]
    assert "nameDescriptionRu" not in filt
    assert "nameDescriptionKz" not in filt
    assert filt["lastUpdateDate"] == ["2026-08-10 09:00:00", "2026-08-10 10:00:00"]

    assert 5 in matches
    assert matches[5].name_ru == "Стеллаж архивный"
    await _close_monitor(monitor)


async def test_incremental_sync_ignores_non_matching_lots_locally(settings: Settings):
    now = datetime.now(timezone.utc)
    end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots_by_keyword = {"irrelevant": [_make_lot(6, "Обычный телевизор LED", end)]}
    transport = RecordingGoszakupTransport(lots_by_keyword)
    monitor = await _build_monitor(settings, transport)

    matches = await monitor.fetch_incremental_matches(("2026-08-10 09:00:00", "2026-08-10 10:00:00"))
    assert matches == {}
    await _close_monitor(monitor)


async def test_full_end_to_end_run_once_never_sends_array_filter(settings: Settings):
    """End-to-end guard: run a full incremental sync + pending check and
    verify not a single request ever violated the schema, using the same
    transport-level hard check as the live API would apply.
    """
    now = datetime.now(timezone.utc)
    end = (now + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
    lots_by_keyword = {"стеллаж": [_make_lot(7, "Стеллаж усиленный", end)]}
    transport = RecordingGoszakupTransport(lots_by_keyword)
    monitor = await _build_monitor(settings, transport)

    await monitor.run_incremental_sync()
    await monitor.run_pending_check()

    for filt in transport.seen_filters:
        assert not isinstance(filt.get("nameDescriptionRu"), list)
        assert not isinstance(filt.get("nameDescriptionKz"), list)
    await _close_monitor(monitor)
