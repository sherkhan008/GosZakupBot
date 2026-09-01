"""Data access layer for the sent_lots/app_state/settings tables.

Minimal storage by design: only lots that were actually sent are ever
persisted, keyed by lot_id, purely to prevent resending. Nothing else about
a tender is stored -- no pending/expired state, no name/amount/deadline
snapshot. Periodic discovery is the source of truth for everything else; a
lot that is too early or too late is simply skipped and re-evaluated fresh
the next time discovery finds it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    # ------------------------------------------------------------- sent_lots
    async def is_sent(self, lot_id: int) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM sent_lots WHERE lot_id = ?", (lot_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def mark_sent(self, lot_id: int) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO sent_lots (lot_id, sent_at) VALUES (?, ?)",
            (lot_id, _now_iso()),
        )
        await self._conn.commit()

    # ------------------------------------------------------------- app_state
    async def get_app_state(self, key: str) -> Optional[str]:
        cursor = await self._conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["value"] if row else None

    async def set_app_state(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    # -------------------------------------------------------------- settings
    async def get_setting(self, key: str) -> Optional[str]:
        cursor = await self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()
