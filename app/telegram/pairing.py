"""Owner chat resolution and /start pairing flow.

Resolution order (per spec section 5):
1. TELEGRAM_CHAT_ID from .env, if set -- always takes priority.
2. Previously paired owner_chat_id stored in SQLite (settings table).
3. Otherwise, wait for a fresh private /start message and pair on it.

Updates that existed before pairing mode started are ignored so an old,
unrelated /start cannot accidentally register the wrong user.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.database.repository import Repository
from app.telegram.client import TelegramClient

logger = logging.getLogger(__name__)

OWNER_CHAT_ID_KEY = "owner_chat_id"
POLL_INTERVAL_SECONDS = 2
LONG_POLL_TIMEOUT_SECONDS = 25


async def resolve_owner_chat_id(
    configured_chat_id: str, repo: Repository
) -> Optional[str]:
    if configured_chat_id:
        return configured_chat_id
    return await repo.get_setting(OWNER_CHAT_ID_KEY)


async def pair_owner(telegram: TelegramClient, repo: Repository) -> str:
    print("Send /start to your Telegram bot.")
    logger.info("Pairing mode: waiting for a private /start message")

    # Establish a baseline so backlog updates from before pairing started
    # cannot accidentally register another user.
    backlog = await telegram.get_updates(offset=None, timeout=0)
    offset = max((u["update_id"] for u in backlog), default=-1) + 1

    while True:
        updates = await telegram.get_updates(offset=offset, timeout=LONG_POLL_TIMEOUT_SECONDS)
        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            text = (message.get("text") or "").strip()
            if chat.get("type") == "private" and text == "/start":
                chat_id = str(chat["id"])
                await repo.set_setting(OWNER_CHAT_ID_KEY, chat_id)
                logger.info("Owner chat paired successfully")
                try:
                    await telegram.send_message(
                        chat_id, "✅ GosZakup monitoring connected."
                    )
                except Exception:
                    logger.exception("Failed to send pairing confirmation message")
                return chat_id
        if not updates:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
