"""Core monitoring service.

BUSINESS RULE: a tender is eligible for exactly three reasons -- keyword in
its title, amount > MIN_AMOUNT_KZT, and 5-72h remaining until its deadline.
lastUpdateDate is NEVER part of that decision. A tender last touched by
GosZakup a year ago must be found and sent just as readily as one updated a
minute ago, as long as it still matches on those three criteria.

Discovery scan (run_discovery_scan) is therefore the PRIMARY, authoritative
detection mechanism: it re-searches every one of the 92 keywords with NO
lastUpdateDate filter at all, every DISCOVERY_SCAN_INTERVAL_MINUTES, so it
finds every currently-matching tender regardless of how long ago it was last
edited. Correctness of the whole system depends only on discovery scan (plus
ingest_lot's amount/deadline/dedup checks) -- everything else is optional:

  - Bootstrap (services/bootstrap.py): one-time historical load on first
    start, tracked by app_state.bootstrap_completed. Superseded within
    minutes by the first discovery scan anyway; kept only to get useful
    results slightly faster on a brand new deployment.
  - Incremental sync (run_incremental_sync): a supplementary fast-path, not
    authoritative. It scans only lots whose lastUpdateDate falls in the delta
    window since the last successful sync, purely as a latency optimization
    (catching freshly-touched tenders within minutes instead of waiting for
    the next discovery cycle). The system is fully correct even with this
    disabled or failing indefinitely -- discovery scan alone guarantees every
    matching tender is eventually found.

Storage is intentionally minimal: only successfully-sent lot ids are ever
persisted (sent_lots), purely to prevent resending. A lot outside the 5-72h
window (too early or too late) is never written to the database at all --
it's simply skipped, and the next discovery scan re-evaluates it from
scratch by keyword, exactly as if it were seen for the first time. There is
no 'pending' or 'expired' storage and therefore no separate pending-refresh
pass: periodic discovery is the sole source of truth for anything not yet
sent.

MEMORY: every fetch path is a streaming pipeline -- API page (bounded by
PAGE_LIMIT) -> parse -> title match -> ingest_lot() -> discard -> next page.
Nothing here ever accumulates raw API payloads or parsed Lot objects across
pages/queries; the only per-run structures held in memory are small,
constant-ish-size counters (a stats dict) and, during a multi-keyword
discovery/bootstrap search, a lightweight set of already-seen lot ids
(ints only, never full payloads) used to dedup the same lot appearing under
multiple keywords. This is what keeps memory roughly constant regardless of
whether a query returns a handful of lots or hundreds of thousands (as
incremental sync historically did after a long catch-up window).

All paths funnel into ingest_lot(), which evaluates the amount filter, the
5-72h deadline window, and duplicate protection, then sends via Telegram
exactly once.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import Settings
from app.database.repository import Repository
from app.filters.amount_filter import passes_amount_filter
from app.filters.deadline_filter import DeadlineStatus, classify_deadline, remaining_timedelta
from app.filters.keyword_filter import KeywordFilter
from app.goszakup.client import GoszakupClient, GoszakupError
from app.goszakup.models import Lot
from app.goszakup.parser import (
    build_tender_url,
    derive_delivery_place,
    derive_display_name,
    derive_tender_number,
    derive_trd_buy_id,
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
from app.memory_utils import get_rss_mb
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.formatter import build_message

logger = logging.getLogger(__name__)

LAST_SYNC_KEY = "last_successful_sync"
BOOTSTRAP_DONE_KEY = "bootstrap_completed"
LAST_DISCOVERY_SCAN_KEY = "last_discovery_scan"

PAGE_LIMIT = 200  # documented GosZakup V3 maximum page size

# How often (in lots streamed) to emit a periodic MEMORY log line during a
# long-running fetch -- frequent enough to catch a real leak developing,
# far too infrequent to spam logs even across a 160k-lot catch-up run.
MEMORY_LOG_EVERY_N_LOTS = 500

# ingest_lot() outcomes, used both for control flow and funnel counting.
OUTCOME_ALREADY_SENT = "already_sent"
OUTCOME_MISSING_END_DATE = "missing_end_date"
OUTCOME_AMOUNT_REJECTED = "amount_rejected"
OUTCOME_EXPIRED = "expired"
OUTCOME_PENDING = "pending"
OUTCOME_SENT = "sent"
OUTCOME_SEND_FAILED = "send_failed"


def _new_stats(keywords_loaded: int) -> dict[str, Any]:
    return {
        "keywords_loaded": keywords_loaded,
        "api_requests": 0,
        "api_failures": 0,
        "lots_received": 0,
        "unique_lots": 0,
        "keyword_matches": 0,
        "rejected_amount": 0,
        "rejected_deadline": 0,
        "pending": 0,
        "already_sent": 0,
        "telegram_sent": 0,
        "missing_end_date": 0,
    }


def _record_outcome(stats: dict[str, Any], outcome: str) -> None:
    if outcome == OUTCOME_AMOUNT_REJECTED:
        stats["rejected_amount"] += 1
    elif outcome == OUTCOME_EXPIRED:
        stats["rejected_deadline"] += 1
    elif outcome in (OUTCOME_PENDING, OUTCOME_SEND_FAILED):
        stats["pending"] += 1
    elif outcome == OUTCOME_ALREADY_SENT:
        stats["already_sent"] += 1
    elif outcome == OUTCOME_SENT:
        stats["telegram_sent"] += 1
    elif outcome == OUTCOME_MISSING_END_DATE:
        stats["missing_end_date"] += 1


def _log_summary(label: str, stats: dict[str, Any]) -> None:
    logger.info("%s SUMMARY: %s", label, " ".join(f"{k}={v}" for k, v in stats.items()))


def _log_memory(label: str, phase: str, stats: dict[str, Any]) -> None:
    rss_mb = get_rss_mb()
    logger.info(
        "MEMORY: rss_mb=%s stage=%s phase=%s lots_received=%d unique_lots=%d",
        f"{rss_mb:.1f}" if rss_mb is not None else "unavailable",
        label,
        phase,
        stats["lots_received"],
        stats["unique_lots"],
    )


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
    async def ingest_lot(self, lot: Lot) -> str:
        """Evaluate one keyword-matched lot: duplicate protection, amount
        filter, deadline classification, and (if eligible) Telegram send.
        Nothing is ever written to the database except sent_lots, and only
        after a successful send -- a lot outside the 5-72h window is simply
        skipped, with no DB row created for it. Returns one of the
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

        now = datetime.now(timezone.utc)
        remaining = remaining_timedelta(end_date, now)
        status = classify_deadline(
            remaining, self.settings.min_hours_remaining, self.settings.max_hours_remaining
        )

        if status == DeadlineStatus.EXPIRED:
            logger.info(
                "Tender %s outside window (remaining=%s < %dh); skipped, nothing stored",
                lot.id,
                remaining,
                self.settings.min_hours_remaining,
            )
            return OUTCOME_EXPIRED

        if status == DeadlineStatus.PENDING:
            logger.info(
                "Tender %s too early (remaining=%s > %dh); skipped, nothing stored -- "
                "next discovery scan will re-evaluate it",
                lot.id,
                remaining,
                self.settings.max_hours_remaining,
            )
            return OUTCOME_PENDING

        # ELIGIBLE: recompute remaining immediately before sending (per spec) and send.
        name = derive_display_name(lot)
        delivery_place = derive_delivery_place(lot)
        tender_number = derive_tender_number(lot)
        trd_buy_id = derive_trd_buy_id(lot)
        url = build_tender_url(trd_buy_id)

        now = datetime.now(timezone.utc)
        remaining = remaining_timedelta(end_date, now)
        message = build_message(
            name=name,
            tender_number=tender_number,
            lot_number=lot.lot_number,
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

    # ------------------------------------------------- streaming ingestion
    async def _ingest_one(
        self,
        raw_lot: dict[str, Any],
        stats: dict[str, Any],
        seen_lot_ids: Optional[set[int]] = None,
    ) -> None:
        """Process exactly one raw lot dict end-to-end -- parse, apply the
        title-only local keyword match, and (if matched) run ingest_lot --
        then let it go out of scope. Callers must stream raw_lot values one
        at a time (e.g. from fetch_lots_paginated) and never collect them
        into a list/dict first: this method is the only place a raw payload
        or parsed Lot exists, and only for the duration of this call, which
        is what keeps memory roughly constant regardless of how many lots
        are streamed through in total.

        seen_lot_ids, if given, is a lightweight cross-query dedup set (lot
        ids only, never full payloads/Lot objects) so the same lot returned
        by two different keyword/language queries in one discovery or
        bootstrap run is ingested only once -- this also prevents a
        duplicate Telegram send that could otherwise race across concurrent
        keyword tasks for the same lot.
        """
        stats["lots_received"] += 1
        lot_id = raw_lot.get("id")
        if lot_id is None:
            return
        if seen_lot_ids is not None:
            if lot_id in seen_lot_ids:
                return
            seen_lot_ids.add(lot_id)
        stats["unique_lots"] += 1

        lot = parse_lot(raw_lot)
        # The local keyword filter is the final authority, and per the
        # title-only matching rule it checks ONLY Lots.nameRu / Lots.nameKz
        # -- NOT descriptions, TrdBuy names, or Plan text -- even though
        # those fields may have been used server-side to find candidates.
        if not self.keyword_filter.match_any(title_fields(lot)):
            return
        stats["keyword_matches"] += 1

        outcome = await self.ingest_lot(lot)
        _record_outcome(stats, outcome)

    async def process_date_range_query(self, last_update_range: tuple[str, str]) -> dict[str, Any]:
        """Normal 5-minute cycle: a single date-only query (no server-side
        keyword search at all), streamed page-by-page -- each raw lot is
        matched locally (title-only, all 92 keywords) and ingested
        immediately, never accumulated into a list/dict first. This is what
        keeps a large catch-up window (historically up to 160k+ lots after
        an outage) from ever holding more than one page's worth of raw
        payloads in memory at a time.
        """
        filt = build_date_range_filter(last_update_range)
        assert "nameDescriptionRu" not in filt and "nameDescriptionKz" not in filt

        stats = _new_stats(len(self.keyword_filter.keywords))
        stats["api_requests"] = 1
        _log_memory("INCREMENTAL SYNC", "start", stats)
        async for raw_lot in self.goszakup.fetch_lots_paginated(filt, limit=PAGE_LIMIT):
            await self._ingest_one(raw_lot, stats)
            if stats["lots_received"] % MEMORY_LOG_EVERY_N_LOTS == 0:
                _log_memory("INCREMENTAL SYNC", "progress", stats)
        _log_memory("INCREMENTAL SYNC", "end", stats)
        return stats

    async def run_keyword_search(
        self, last_update_range: Optional[tuple[str, str]], label: str
    ) -> dict[str, Any]:
        """Search GosZakup one keyword at a time (String, never a list)
        across both language fields, with a bounded worker pool
        (settings.discovery_concurrency) of concurrent keyword/language
        pipelines. Used by BOTH the one-time bootstrap (with a bounded
        lastUpdateDate window, purely to keep the very first run fast) and
        the recurring discovery scan (with last_update_range=None -- no
        date bound at all, since discovery scan is the authoritative,
        lastUpdateDate-independent detection mechanism).

        Each pipeline streams its own pages directly into _ingest_one as
        they arrive -- no query's results are ever collected into a list
        first, and no more than settings.discovery_concurrency pipelines
        run at once, so total memory stays roughly constant regardless of
        how many of the 184 (92 keywords x 2 languages) queries there are
        or how many lots any one of them returns. Cross-query duplicates
        (the same lot matching more than one keyword) are deduped via a
        single shared set of lot ids, never full payloads.

        Returns the aggregated stats dict; raises GoszakupError only if
        every single query failed.
        """
        keywords = self.keyword_filter.keywords
        stats = _new_stats(len(keywords))
        if not keywords:
            _log_summary(label, stats)
            return stats

        semaphore = asyncio.Semaphore(self.settings.discovery_concurrency)
        query_plan = [
            (keyword, field)
            for keyword in keywords
            for field in (NAME_DESCRIPTION_RU, NAME_DESCRIPTION_KZ)
        ]
        seen_lot_ids: set[int] = set()

        async def run_one(keyword: str, field: str) -> None:
            filt = build_single_keyword_filter(
                keyword=keyword, field=field, last_update_date_range=last_update_range
            )
            assert isinstance(filt.get(field), str), "keyword filter must be a single string"
            stats["api_requests"] += 1
            async with semaphore:
                try:
                    async for raw_lot in self.goszakup.fetch_lots_paginated(filt, limit=PAGE_LIMIT):
                        await self._ingest_one(raw_lot, stats, seen_lot_ids)
                        if stats["lots_received"] % MEMORY_LOG_EVERY_N_LOTS == 0:
                            _log_memory(label, "progress", stats)
                except GoszakupError:
                    stats["api_failures"] += 1
                    logger.warning(
                        "Keyword search query failed for field=%s (this keyword/language "
                        "skipped this run, others continue)",
                        field,
                        exc_info=True,
                    )

        logger.info(
            "Keyword search: issuing %d single-keyword queries (%d keywords x 2 languages, concurrency=%d)",
            len(query_plan),
            len(keywords),
            self.settings.discovery_concurrency,
        )
        _log_memory(label, "start", stats)
        await asyncio.gather(*(run_one(kw, field) for kw, field in query_plan))
        _log_memory(label, "end", stats)

        if stats["api_requests"] > 0 and stats["api_failures"] == stats["api_requests"]:
            raise GoszakupError(f"All {stats['api_requests']} keyword search queries failed")
        if stats["api_failures"]:
            logger.warning(
                "Keyword search: %d of %d queries failed and were skipped this run",
                stats["api_failures"],
                stats["api_requests"],
            )

        _log_summary(label, stats)
        return stats

    # ------------------------------------------------------------- pipeline
    async def run_incremental_sync(self) -> Optional[dict[str, Any]]:
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
            stats = await self.process_date_range_query((from_str, to_str))
        except GoszakupError:
            logger.error("Incremental sync aborted due to API error; checkpoint not advanced")
            return None

        _log_summary("INCREMENTAL SYNC", stats)
        await self.repo.set_app_state(LAST_SYNC_KEY, now.isoformat())
        logger.info("Incremental sync completed successfully")
        return stats

    async def run_discovery_scan(self) -> Optional[dict[str, Any]]:
        """Periodic (DISCOVERY_SCAN_INTERVAL_MINUTES) re-scan of ALL keywords,
        with NO lastUpdateDate filter whatsoever -- this is the primary,
        authoritative detection mechanism (see module docstring). A tender
        last touched by GosZakup a year ago is found here exactly the same
        way as one touched a minute ago, as long as it still matches by
        keyword; eligibility is decided afterwards, in ingest_lot(), purely
        by amount and remaining time.

        This is NOT the one-time bootstrap: it runs repeatedly for the life
        of the deployment, tracked by its own last_discovery_scan checkpoint,
        completely independent of bootstrap_completed.
        """
        now = datetime.now(timezone.utc)
        logger.info("Discovery scan started: re-scanning all keywords (no lastUpdateDate filter)")
        try:
            stats = await self.run_keyword_search(None, label="DISCOVERY")
        except GoszakupError:
            logger.error("Discovery scan aborted due to a GosZakup API error; will retry next cycle")
            return None

        await self.repo.set_app_state(LAST_DISCOVERY_SCAN_KEY, now.isoformat())
        logger.info("Discovery scan completed successfully")
        return stats

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

    async def run_once(self) -> None:
        await self.run_incremental_sync()
        await self.maybe_run_discovery_scan()
