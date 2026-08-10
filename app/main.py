"""Entrypoint.

    python -m app.main            # continuous monitoring loop
    python -m app.main --once     # one sync/check cycle, then exit
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.config import Settings, load_settings
from app.database.db import init_db
from app.database.repository import Repository
from app.filters.keyword_filter import KeywordFilter
from app.goszakup.client import GoszakupClient, GoszakupError
from app.goszakup.queries import LOTS_QUERY
from app.logging_utils import configure_logging
from app.services.bootstrap import run_bootstrap
from app.services.monitor import MonitorService
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.pairing import pair_owner, resolve_owner_chat_id

logger = logging.getLogger(__name__)


def _secrets_to_redact(settings: Settings) -> list[str]:
    # Every value here is scrubbed from every log line, including exception
    # tracebacks, for the lifetime of the process. See app/logging_utils.py.
    return [
        settings.goszakup_api_token,
        settings.telegram_bot_token,
        settings.telegram_chat_id,
    ]


async def _verify_goszakup(client: GoszakupClient, token: str) -> None:
    if not token:
        logger.info("GOSZAKUP_API_TOKEN is empty; skipping GosZakup connectivity check")
        return
    try:
        data = await client.execute(LOTS_QUERY, {"filter": {}, "limit": 1, "after": None})
        lots = data.get("Lots") or []
        logger.info("GosZakup API check OK (sample query returned %d lot(s))", len(lots))
    except GoszakupError:
        logger.exception(
            "GosZakup API check FAILED -- verify GOSZAKUP_API_TOKEN and that the schema "
            "still matches app/goszakup/queries.py"
        )


async def _verify_telegram(client: TelegramClient, token: str) -> None:
    if not token:
        logger.info("TELEGRAM_BOT_TOKEN is empty; skipping Telegram connectivity check")
        return
    try:
        await client.get_me()
        logger.info("Telegram API authentication OK")
    except TelegramError:
        logger.exception("Telegram API check FAILED -- verify TELEGRAM_BOT_TOKEN")


async def _run(once: bool) -> None:
    settings = load_settings()
    configure_logging(settings.log_level, _secrets_to_redact(settings))
    logger.info("Application starting (mode=%s)", "once" if once else "continuous")

    conn = await init_db(settings.database_path)
    repo = Repository(conn)
    keyword_filter = KeywordFilter.from_yaml(settings.keywords_path)
    logger.info(
        "Loaded %d keyword(s) from %s", len(keyword_filter.keywords), settings.keywords_path
    )

    goszakup = GoszakupClient(settings.goszakup_graphql_url, settings.goszakup_api_token)
    telegram = TelegramClient(settings.telegram_api_base, settings.telegram_bot_token)

    try:
        await _verify_goszakup(goszakup, settings.goszakup_api_token)
        await _verify_telegram(telegram, settings.telegram_bot_token)

        owner_chat_id = await resolve_owner_chat_id(settings.telegram_chat_id, repo)
        if not owner_chat_id:
            if not settings.telegram_bot_token:
                logger.warning(
                    "TELEGRAM_BOT_TOKEN is empty; cannot pair an owner chat or send "
                    "notifications yet. Sync will still run and store matching candidates "
                    "locally so nothing is lost."
                )
                owner_chat_id = ""
            else:
                owner_chat_id = await pair_owner(telegram, repo)

        monitor = MonitorService(
            settings, repo, goszakup, telegram, keyword_filter, owner_chat_id
        )

        await run_bootstrap(monitor)

        if once:
            await monitor.run_once()
        else:
            logger.info(
                "Entering continuous monitoring loop (interval=%ss)",
                settings.check_interval_seconds,
            )
            while True:
                try:
                    await monitor.run_once()
                except Exception:
                    logger.exception("Unhandled error during monitoring cycle; continuing")
                await asyncio.sleep(settings.check_interval_seconds)
    finally:
        await goszakup.close()
        await telegram.close()
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="GosZakup Telegram monitoring bot")
    parser.add_argument(
        "--once", action="store_true", help="Run a single sync/check cycle and exit"
    )
    args = parser.parse_args()
    asyncio.run(_run(args.once))


if __name__ == "__main__":
    main()
