"""Core monitoring service.

Three independent discovery mechanisms feed the same ingestion pipeline:

  - Bootstrap (services/bootstrap.py): one-time historical load, runs once
    per database lifetime, tracked by app_state.bootstrap_completed.
  - Incremental sync (run_incremental_sync): fast, frequent (every
    CHECK_INTERVAL_SECONDS), scans only lots whose lastUpdateDate falls in
    the delta window since the last successful sync.
  - Discovery scan (run_discovery_scan): slower, periodic (every
    DISCOVERY_SCAN_INTERVAL_MINUTES), re-scans ALL 92 keywords independently
    of lastUpdateDate. This exists because a tender's lastUpdateDate reflects
    when GosZakup last *edited* it, not when its deadline enters the 5-72h
    window -- a tender published weeks ago with an untouched lastUpdateDate
    would never surface via incremental sync alone. Discovery scan re-finds
    it the same way bootstrap originally would have.

All three funnel into ingest_lot(), which evaluates title-only keyword
relevance already applied upstream, the amount filter, the 5-72h deadline
window, and duplicate protection, then sends via Telegram exactly once.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import Settings
from app.database.repository import (
    STATUS_EXPIRED,
    STATUS_SENT,
    Repository,
    TenderRecord,
)
from app.filters.amount_filter import passes_amount_filter
from app.filters.deadline_filter import DeadlineStatus, classify_deadline, remaining_timedelta
from app.filters.keyword_filter import KeywordFilter
from app.goszakup.client import GoszakupClient, GoszakupError
from app.goszakup.models import Lot
from app.goszakup.parser import (
    build_tender_url,
    derive_delivery_place,
    derive_display_name,
    format_api_datetime,
    parse_api_datetime,
    parse_lot,
    title_fields,
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
LAST_DISCOVERY_SCAN_KEY = "last_discovery_scan"

PAGE_LIMIT = 200  # documented GosZakup V3 maximum page size
KEYWORD_SEARCH_CONCURRENCY = 4  # conservative: 3-5 simultaneous requests

# ingest_lot() outcomes, used both for control flow and funnel counting.
OUTCOME_ALREADY_SENT = "already_sent"
OUTCOME_MISSING_END_DATE = "missing_end_date"
OUTCOME_AMOUNT_REJECTED = "amount_rejected"
OUTCOME_EXPIRED = "expired"
OUTCOME_PENDING = "pending"
OUTCOME_SENT = "sent"
OUTCOME_SEND_FAILED = "send_failed"


def chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


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
    async def ingest_lot(self, lot: Lot, matched_keyword: Optional[str]) -> str:
        """Evaluate one keyword-matched lot: amount filter, deadline
        classification, duplicate protection, and (if eligible) Telegram
        send. Never touches a lot that is already 'sent'. Returns one of the
        OUTCOME_* constants for funnel logging.
        """
        if await self.repo.is_sent(lot.id):
            return OUTCOME_ALREADY_SENT

        if lot.trd_buy is None or not lot.trd_buy.end_date:
            logger.warning("Lot %s has no TrdBuy.endDate; cannot evaluate deadline, skipping", lot.id)
            return OUTCOME_MISSING_END_DATE

        end_date = parse_api_datetime(lot.trd_buy.end_date, self.settings.app_timezone)
        if end_date is None:
            logger.warning("Lot %s has unparseable endDate %r, skipping", lot.id, lot.trd_buy.end_date)
            return OUTCOME_MISSING_END_DATE

        if not passes_amount_filter(lot.amount, self.settings.min_amount_kzt):
            logger.info(
                "Tender %s rejected by amount filter (amount=%s, must be > %s)",
                lot.id,
                lot.amount,
                self.settings.min_amount_kzt,
            )
            return OUTCOME_AMOUNT_REJECTED

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
        if current_status == STATUS_SENT:
            return OUTCOME_ALREADY_SENT
        if current_status == STATUS_EXPIRED:
            return OUTCOME_EXPIRED

        now = datetime.now(timezone.utc)
        remaining = remaining_timedelta(end_date, now)
        status = classify_deadline(
            remaining, self.settings.min_hours_remaining, self.settings.max_hours_remaining
        )

        if status == DeadlineStatus.EXPIRED:
            await self.repo.mark_expired(lot.id)
            logger.info("Tender %s expired without being sent (remaining=%s)", lot.id, remaining)
            return OUTCOME_EXPIRED

        if status == DeadlineStatus.PENDING:
            logger.info("Tender %s is pending (remaining=%s, waiting for 5-72h window)", lot.id, remaining)
            return OUTCOME_PENDING

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
            return OUTCOME_SEND_FAILED
        await self.repo.mark_sent(lot.id)
        logger.info("Telegram notification sent for tender %s (remaining=%s)", lot.id, remaining)
        return OUTCOME_SENT

    # ------------------------------------------------------------- fetching
    def _apply_local_match(self, raw_by_id: dict[int, dict]) -> dict[int, Lot]:
        """The local keyword filter is the final authority, and per the
        title-only matching rule it checks ONLY Lots.nameRu / Lots.nameKz --
        NOT descriptions, TrdBuy names, or Plan text -- even though those
        fields may have been used server-side to find candidates.
        """
        matches: dict[int, Lot] = {}
        for lot_id, raw_lot in raw_by_id.items():
            lot = parse_lot(raw_lot)
            matched_keyword = self.keyword_filter.match_any(title_fields(lot))
            if matched_keyword:
                matches[lot_id] = lot
                lot._matched_keyword = matched_keyword  # type: ignore[attr-defined]
        return matches

    async def fetch_keyword_search_matches(
        self, last_update_range: Optional[tuple[str, str]]
    ) -> tuple[dict[int, Lot], dict[str, Any]]:
        """Search GosZakup one keyword at a time (String, never a list) across
        both language fields, with pagination per query and concurrency
        limited via a semaphore. Used by BOTH the one-time bootstrap and the
        recurring discovery scan -- the only difference between them is the
        date window and what happens to the checkpoints afterwards.

        Returns (matches, fetch_stats) where fetch_stats carries the raw
        funnel numbers (requests issued/failed, lots received/deduped).
        """
        keywords = self.keyword_filter.keywords
        if not keywords:
            return {}, {
                "keywords_loaded": 0,
                "api_requests": 0,
                "api_failures": 0,
                "lots_received": 0,
                "unique_lots": 0,
            }

        semaphore = asyncio.Semaphore(KEYWORD_SEARCH_CONCURRENCY)
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
                        "Keyword search query failed for field=%s (this keyword/language "
                        "skipped this run, others continue)",
                        field,
                        exc_info=True,
                    )
                    return False, []

        logger.info(
            "Keyword search: issuing %d single-keyword queries (%d keywords x 2 languages, concurrency=%d)",
            len(query_plan),
            len(keywords),
            KEYWORD_SEARCH_CONCURRENCY,
        )
        results = await asyncio.gather(*(fetch_one(kw, field) for kw, field in query_plan))

        successes = sum(1 for ok, _ in results if ok)
        failures = len(results) - successes
        if successes == 0 and len(results) > 0:
            raise GoszakupError(f"All {len(results)} keyword search queries failed")
        if failures:
            logger.warning(
                "Keyword search: %d of %d queries failed and were skipped this run",
                failures,
                len(results),
            )

        raw_by_id: dict[int, dict] = {}
        lots_received = 0
        for ok, pages in results:
            if not ok:
                continue
            lots_received += len(pages)
            for raw_lot in pages:
                lot_id = raw_lot.get("id")
                if lot_id is not None:
                    raw_by_id[lot_id] = raw_lot

        matches = self._apply_local_match(raw_by_id)
        fetch_stats = {
            "keywords_loaded": len(keywords),
            "api_requests": len(query_plan),
            "api_failures": failures,
            "lots_received": lots_received,
            "unique_lots": len(raw_by_id),
        }
        return matches, fetch_stats

    async def fetch_incremental_matches(
        self, last_update_range: tuple[str, str]
    ) -> tuple[dict[int, Lot], dict[str, Any]]:
        """Normal 5-minute cycle: a single date-only query (no server-side
        keyword search at all), then all 92 keywords are matched locally
        (title-only) against every lot returned.
        """
        filt = build_date_range_filter(last_update_range)
        assert "nameDescriptionRu" not in filt and "nameDescriptionKz" not in filt

        raw_by_id: dict[int, dict] = {}
        lots_received = 0
        async for raw_lot in self.goszakup.fetch_lots_paginated(filt, limit=PAGE_LIMIT):
            lots_received += 1
            lot_id = raw_lot.get("id")
            if lot_id is not None:
                raw_by_id[lot_id] = raw_lot

        matches = self._apply_local_match(raw_by_id)
        fetch_stats = {
            "keywords_loaded": len(self.keyword_filter.keywords),
            "api_requests": 1,
            "api_failures": 0,
            "lots_received": lots_received,
            "unique_lots": len(raw_by_id),
        }
        return matches, fetch_stats

    # ------------------------------------------------------------- summary
    async def _ingest_and_summarize(
        self, matches: dict[int, Lot], fetch_stats: dict[str, Any], label: str
    ) -> dict[str, Any]:
        outcomes: list[str] = []
        for lot in matches.values():
            matched_keyword = getattr(lot, "_matched_keyword", None)
            outcome = await self.ingest_lot(lot, matched_keyword)
            outcomes.append(outcome)
        counts = Counter(outcomes)

        summary = dict(fetch_stats)
        summary["keyword_matches"] = len(matches)
        summary["rejected_amount"] = counts[OUTCOME_AMOUNT_REJECTED]
        summary["expired"] = counts[OUTCOME_EXPIRED]
        summary["pending"] = counts[OUTCOME_PENDING] + counts[OUTCOME_SEND_FAILED]
        summary["already_sent"] = counts[OUTCOME_ALREADY_SENT]
        summary["telegram_sent"] = counts[OUTCOME_SENT]
        summary["missing_end_date"] = counts[OUTCOME_MISSING_END_DATE]

        logger.info(
            "%s SUMMARY: %s", label, " ".join(f"{k}={v}" for k, v in summary.items())
        )
        return summary

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
            matches, fetch_stats = await self.fetch_incremental_matches((from_str, to_str))
        except GoszakupError:
            logger.error("Incremental sync aborted due to API error; checkpoint not advanced")
            return

        await self._ingest_and_summarize(matches, fetch_stats, "INCREMENTAL SYNC")

        await self.repo.set_app_state(LAST_SYNC_KEY, now.isoformat())
        logger.info("Incremental sync completed successfully")

    async def run_discovery_scan(self) -> None:
        """Periodic (DISCOVERY_SCAN_INTERVAL_MINUTES) re-scan of ALL keywords
        over the last DISCOVERY_LOOKBACK_DAYS days, independent of
        lastUpdateDate. Re-discovers tenders that existed all along but were
        never touched by GosZakup since they were first published, and whose
        deadline has now entered (or is approaching) the 5-72h window.

        This is NOT the one-time bootstrap: it runs repeatedly for the life
        of the deployment, tracked by its own last_discovery_scan checkpoint,
        completely independent of bootstrap_completed.
        """
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=self.settings.discovery_lookback_days)
        from_str = format_api_datetime(window_start, self.settings.app_timezone)
        to_str = format_api_datetime(now, self.settings.app_timezone)

        logger.info(
            "Discovery scan started: re-scanning last %d day(s) by keyword (%s -> %s)",
            self.settings.discovery_lookback_days,
            from_str,
            to_str,
        )
        try:
            matches, fetch_stats = await self.fetch_keyword_search_matches((from_str, to_str))
        except GoszakupError:
            logger.error("Discovery scan aborted due to a GosZakup API error; will retry next cycle")
            return

        await self._ingest_and_summarize(matches, fetch_stats, "DISCOVERY")

        await self.repo.set_app_state(LAST_DISCOVERY_SCAN_KEY, now.isoformat())
        logger.info("Discovery scan completed successfully")

    async def maybe_run_discovery_scan(self) -> None:
        """Run the discovery scan only if DISCOVERY_SCAN_INTERVAL_MINUTES
        have elapsed since the last one (or it has never run)."""
        last_raw = await self.repo.get_app_state(LAST_DISCOVERY_SCAN_KEY)
        if last_raw:
            last_scan = datetime.fromisoformat(last_raw)
            elapsed_minutes = (datetime.now(timezone.utc) - last_scan).total_seconds() / 60
            if elapsed_minutes < self.settings.discovery_scan_interval_minutes:
                logger.info(
                    "Discovery scan not due yet (%.1f/%d minutes elapsed)",
                    elapsed_minutes,
                    self.settings.discovery_scan_interval_minutes,
                )
                return
        await self.run_discovery_scan()

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

        outcomes: list[str] = []
        refreshed_ids: set[int] = set()
        for raw_lot in raw_lots:
            lot = parse_lot(raw_lot)
            refreshed_ids.add(lot.id)
            outcome = await self.ingest_lot(lot, matched_keywords.get(lot.id))
            outcomes.append(outcome)

        missing = set(pending_ids) - refreshed_ids
        if missing:
            logger.warning(
                "Pending check: %d tender(s) not returned by API by-id refresh (kept pending): %s",
                len(missing),
                sorted(missing),
            )

        counts = Counter(outcomes)
        logger.info(
            "PENDING CHECK SUMMARY: checked=%d refreshed=%d missing=%d expired=%d "
            "pending=%d telegram_sent=%d amount_rejected=%d",
            len(pending_ids),
            len(refreshed_ids),
            len(missing),
            counts[OUTCOME_EXPIRED],
            counts[OUTCOME_PENDING] + counts[OUTCOME_SEND_FAILED],
            counts[OUTCOME_SENT],
            counts[OUTCOME_AMOUNT_REJECTED],
        )

    async def run_once(self) -> None:
        await self.run_incremental_sync()
        await self.maybe_run_discovery_scan()
        await self.run_pending_check()
