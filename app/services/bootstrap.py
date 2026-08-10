"""First-run bootstrap: pull BOOTSTRAP_LOOKBACK_DAYS worth of keyword-matching
lots, evaluate each against the deadline window, and mark the app as
bootstrapped so subsequent runs use incremental sync instead.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.goszakup.parser import format_api_datetime
from app.services.monitor import BOOTSTRAP_DONE_KEY, LAST_SYNC_KEY, MonitorService
from app.goszakup.client import GoszakupError

logger = logging.getLogger(__name__)


async def is_bootstrapped(monitor: MonitorService) -> bool:
    return (await monitor.repo.get_app_state(BOOTSTRAP_DONE_KEY)) == "1"


async def run_bootstrap(monitor: MonitorService) -> None:
    if await is_bootstrapped(monitor):
        logger.info("Bootstrap already completed previously, skipping")
        return

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=monitor.settings.bootstrap_lookback_days)
    from_str = format_api_datetime(window_start, monitor.settings.app_timezone)
    to_str = format_api_datetime(now, monitor.settings.app_timezone)

    logger.info(
        "Bootstrap started: scanning last %d day(s) (%s -> %s)",
        monitor.settings.bootstrap_lookback_days,
        from_str,
        to_str,
    )

    try:
        matches = await monitor.fetch_bootstrap_matches((from_str, to_str))
    except GoszakupError:
        logger.error("Bootstrap aborted due to a GosZakup API error; will retry on next start")
        return

    logger.info("Bootstrap: %d keyword match(es) found across the lookback window", len(matches))
    for lot in matches.values():
        matched_keyword = getattr(lot, "_matched_keyword", None)
        await monitor.ingest_lot(lot, matched_keyword)

    await monitor.repo.set_app_state(LAST_SYNC_KEY, now.isoformat())
    await monitor.repo.set_app_state(BOOTSTRAP_DONE_KEY, "1")
    logger.info("Bootstrap finished successfully")
