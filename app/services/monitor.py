"""Core monitoring service: ingesting candidate lots, evaluating the 5-72h
deadline window, sending Telegram notifications exactly once, and running the
incremental sync + pending-tender re-check every cycle.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import Settings
from app.database.repository import (
    STATUS_EXPIRED,
    STATUS_SENT,
    Repository,
    TenderRecord,
)
from app.filters.deadline_filter import DeadlineStatus, classify_deadline, remaining_timedelta
from app.filters.keyword_filter import KeywordFilter
from app.goszakup.client import GoszakupClient, GoszakupError
from app.goszakup.models import Lot
from app.goszakup.parser import (
    build_tender_url,
    candidate_text_fields,
    derive_delivery_place,
    derive_display_name,
    format_api_datetime,
    parse_api_datetime,
    parse_lot,
)
from app.goszakup.queries import (
    NAME_DESCRIPTION_KZ,
    NAME_DESCRIPTION_RU,
    build_date_range_filter,
    build_single_keyword_filter,
)
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.formatter import build_message

logger = logging.getLogger(__name__)

LAST_SYNC_KEY = "last_successful_sync"
BOOTSTRAP_DONE_KEY = "bootstrap_completed"
PAGE_LIMIT = 200  # documented GosZakup V3 maximum page size
BOOTSTRAP_CONCURRENCY = 4  # conservative: 3-5 simultaneous requests


class MonitorService:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        goszakup: GoszakupClient,
        telegram: TelegramClient,
        keyword_filter: KeywordFilter,
        owner_chat_id: str,
    ):
        self.settings = settings
        self.repo = repo
        self.goszakup = goszakup
        self.telegram = telegram
        self.keyword_filter = keyword_filter
        self.owner_chat_id = owner_chat_id

    # ------------------------------------------------------------ ingestion
    async def ingest_lot(self, lot: Lot, matched_keyword: Optional[str]) -> None:
        """Evaluate one keyword-matched lot and either store it as pending,
        mark it expired, or send it (if eligible) and mark it sent. Never
        touches a lot that is already 'sent'.
        """
        if await self.repo.is_sent(lot.id):
            return

        if lot.trd_buy is None or not lot.trd_buy.end_date:
            logger.warning("Lot %s has no TrdBuy.endDate; cannot evaluate deadline, skipping", lot.id)
            return

        end_date = parse_api_datetime(lot.trd_buy.end_date, self.settings.app_timezone)
        if end_date is None:
            logger.warning("Lot %s has unparseable endDate %r, skipping", lot.id, lot.trd_buy.end_date)
            return

        name = derive_display_name(lot)
        delivery_place = derive_delivery_place(lot)
        url = build_tender_url(lot.trd_buy_id or lot.trd_buy.id)

        record = TenderRecord(
            lot_id=lot.id,
            lot_number=lot.lot_number,
            trd_buy_id=lot.trd_buy_id,
            name=name,
            amount=lot.amount,
            end_date=end_date.isoformat(),
            delivery_place=delivery_place,
            tender_url=url,
            matched_keyword=matched_keyword,
        )
        current_status = await self.repo.upsert_candidate(record)
        if current_status in (STATUS_SENT, STATUS_EXPIRED):
            return

        now = datetime.now(timezone.utc)
        remaining = remaining_timedelta(end_date, now)
        status = classify_deadline(
            remaining, self.settings.min_hours_remaining, self.settings.max_hours_remaining
        )

        if status == DeadlineStatus.EXPIRED:
            await self.repo.mark_expired(lot.id)
            logger.info("Tender %s expired without being sent (remaining=%s)", lot.id, remaining)
            return

        if status == DeadlineStatus.PENDING:
            logger.info("Tender %s is pending (remaining=%s, waiting for 5-72h window)", lot.id, remaining)
            return

        # ELIGIBLE: recompute remaining immediately before sending (per spec) and send.
        now = datetime.now(timezone.utc)
        remaining = remaining_timedelta(end_date, now)
        message = build_message(
            name=name,
            amount=lot.amount,
            delivery_place=delivery_place,
            end_date=end_date,
            remaining=remaining,
            url=url,
        )
        try:
            await self.telegram.send_message(self.owner_chat_id, message)
        except TelegramError:
            logger.exception(
                "Failed to send Telegram notification for lot %s; will retry next cycle", lot.id
            )
            return
        await self.repo.mark_sent(lot.id)
        logger.info("Telegram notification sent for tender %s (remaining=%s)", lot.id, remaining)

    # ------------------------------------------------------------- fetching
    def _apply_local_match(self, raw_by_id: dict[int, dict]) -> dict[int, Lot]:
        """The local keyword filter is the final authority (spec section 7):
        every candidate returned by any server-side search is re-checked here
        against all 92 configured keywords before being accepted.
        """
        matches: dict[int, Lot] = {}
        for lot_id, raw_lot in raw_by_id.items():
            lot = parse_lot(raw_lot)
            matched_keyword = self.keyword_filter.match_any(candidate_text_fields(lot))
            if matched_keyword:
                matches[lot_id] = lot
                lot._matched_keyword = matched_keyword  # type: ignore[attr-defined]
        return matches

    async def fetch_bootstrap_matches(self, last_update_range: tuple[str, str]) -> dict[int, Lot]:
        """First-run historical search: GosZakup's `nameDescriptionRu` /
        `nameDescriptionKz` filters accept exactly ONE String each -- never a
        list -- so every one of the 92 keywords is queried individually
        against each language field, with pagination per query. Concurrency
        is limited via a semaphore so the API is never flooded.
        """
        keywords = self.keyword_filter.keywords
        if not keywords:
            return {}

        semaphore = asyncio.Semaphore(BOOTSTRAP_CONCURRENCY)
        query_plan = [
            (keyword, field)
            for keyword in keywords
            for field in (NAME_DESCRIPTION_RU, NAME_DESCRIPTION_KZ)
        ]

        async def fetch_one(keyword: str, field: str) -> tuple[bool, list[dict]]:
            filt = build_single_keyword_filter(
                keyword=keyword, field=field, last_update_date_range=last_update_range
            )
            assert isinstance(filt.get(field), str), "keyword filter must be a single string"
            async with semaphore:
                try:
                    pages = [
                        raw_lot
                        async for raw_lot in self.goszakup.fetch_lots_paginated(filt, limit=PAGE_LIMIT)
                    ]
                    return True, pages
                except GoszakupError:
                    logger.warning(
                        "Bootstrap query failed for field=%s (keyword search skipped this run)",
                        field,
                        exc_info=True,
                    )
                    return False, []

        logger.info(
            "Bootstrap: issuing %d single-keyword queries (%d keywords x 2 languages, concurrency=%d)",
            len(query_plan),
            len(keywords),
            BOOTSTRAP_CONCURRENCY,
        )
        results = await asyncio.gather(*(fetch_one(kw, field) for kw, field in query_plan))

        successes = sum(1 for ok, _ in results if ok)
        failures = len(results) - successes
        if successes == 0:
            raise GoszakupError(f"All {len(results)} bootstrap keyword queries failed")
        if failures:
            logger.warning(
                "Bootstrap: %d of %d keyword queries failed and were skipped this run",
                failures,
                len(results),
            )

        raw_by_id: dict[int, dict] = {}
        for ok, pages in results:
            if not ok:
                continue
            for raw_lot in pages:
                lot_id = raw_lot.get("id")
                if lot_id is not None:
                    raw_by_id[lot_id] = raw_lot

        logger.info(
            "Bootstrap: %d unique lot(s) retrieved from server-side keyword search, running local match",
            len(raw_by_id),
        )
        return self._apply_local_match(raw_by_id)

    async def fetch_incremental_matches(self, last_update_range: tuple[str, str]) -> dict[int, Lot]:
        """Normal 5-minute cycle: a single date-only query (no server-side
        keyword search at all), then all 92 keywords are matched locally
        against every lot returned. Avoids issuing 92+ API requests per cycle.
        """
        filt = build_date_range_filter(last_update_range)
        assert "nameDescriptionRu" not in filt and "nameDescriptionKz" not in filt

        raw_by_id: dict[int, dict] = {}
        async for raw_lot in self.goszakup.fetch_lots_paginated(filt, limit=PAGE_LIMIT):
            lot_id = raw_lot.get("id")
            if lot_id is not None:
                raw_by_id[lot_id] = raw_lot

        logger.info(
            "Incremental sync: %d lot(s) changed in window, matching locally against %d keyword(s)",
            len(raw_by_id),
            len(self.keyword_filter.keywords),
        )
        return self._apply_local_match(raw_by_id)

    # ------------------------------------------------------------- pipeline
    async def run_incremental_sync(self) -> None:
        now = datetime.now(timezone.utc)
        last_sync_raw = await self.repo.get_app_state(LAST_SYNC_KEY)
        if last_sync_raw:
            last_sync = datetime.fromisoformat(last_sync_raw)
        else:
            last_sync = now - timedelta(days=self.settings.bootstrap_lookback_days)

        window_start = last_sync - timedelta(minutes=self.settings.sync_overlap_minutes)
        from_str = format_api_datetime(window_start, self.settings.app_timezone)
        to_str = format_api_datetime(now, self.settings.app_timezone)

        logger.info("Incremental sync: window %s -> %s", from_str, to_str)
        try:
            matches = await self.fetch_incremental_matches((from_str, to_str))
        except GoszakupError:
            logger.error("Incremental sync aborted due to API error; checkpoint not advanced")
            return

        logger.info("Incremental sync: %d keyword matches found", len(matches))
        for lot in matches.values():
            matched_keyword = getattr(lot, "_matched_keyword", None)
            await self.ingest_lot(lot, matched_keyword)

        await self.repo.set_app_state(LAST_SYNC_KEY, now.isoformat())
        logger.info("Incremental sync completed successfully")

    async def run_pending_check(self) -> None:
        pending_rows = await self.repo.get_pending_tenders()
        if not pending_rows:
            logger.info("Pending check: no pending tenders")
            return

        logger.info("Pending check: refreshing %d pending tender(s)", len(pending_rows))
        pending_ids = [row["lot_id"] for row in pending_rows]
        matched_keywords = {row["lot_id"]: row["matched_keyword"] for row in pending_rows}

        try:
            raw_lots = await self.goszakup.fetch_lots_by_ids(pending_ids)
        except GoszakupError:
            logger.exception("Failed to refresh pending tenders from GosZakup; will retry next cycle")
            return

        refreshed_ids: set[int] = set()
        for raw_lot in raw_lots:
            lot = parse_lot(raw_lot)
            refreshed_ids.add(lot.id)
            await self.ingest_lot(lot, matched_keywords.get(lot.id))

        missing = set(pending_ids) - refreshed_ids
        if missing:
            logger.warning(
                "Pending check: %d tender(s) not returned by API by-id refresh (kept pending): %s",
                len(missing),
                sorted(missing),
            )

    async def run_once(self) -> None:
        await self.run_incremental_sync()
        await self.run_pending_check()
