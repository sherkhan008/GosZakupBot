"""Data access layer for the tenders/app_state/settings tables."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

import aiosqlite

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_EXPIRED = "expired"


@dataclass
class TenderRecord:
    lot_id: int
    lot_number: Optional[str]
    trd_buy_id: Optional[int]
    name: str
    amount: Optional[float]
    end_date: Optional[str]  # ISO 8601 string, timezone-aware
    delivery_place: str
    tender_url: str
    matched_keyword: Optional[str]
    status: str = STATUS_PENDING
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    sent_at: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    # ---------------------------------------------------------------- tenders
    async def get_tender(self, lot_id: int) -> Optional[aiosqlite.Row]:
        cursor = await self._conn.execute(
            "SELECT * FROM tenders WHERE lot_id = ?", (lot_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def upsert_candidate(self, record: TenderRecord) -> str:
        """Insert a new candidate as 'pending', or refresh mutable fields of an
        existing non-final (pending) record. Records already 'sent' or 'expired'
        are left untouched except for last_seen_at, per duplicate-protection rules.

        Returns the resulting status of the row in the DB.
        """
        now = _now_iso()
        existing = await self.get_tender(record.lot_id)
        if existing is None:
            await self._conn.execute(
                """
                INSERT INTO tenders (
                    lot_id, lot_number, trd_buy_id, name, amount, end_date,
                    delivery_place, tender_url, matched_keyword, status,
                    first_seen_at, last_seen_at, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.lot_id,
                    record.lot_number,
                    record.trd_buy_id,
                    record.name,
                    record.amount,
                    record.end_date,
                    record.delivery_place,
                    record.tender_url,
                    record.matched_keyword,
                    STATUS_PENDING,
                    now,
                    now,
                    None,
                ),
            )
            await self._conn.commit()
            return STATUS_PENDING

        if existing["status"] == STATUS_PENDING:
            await self._conn.execute(
                """
                UPDATE tenders
                SET lot_number = ?, trd_buy_id = ?, name = ?, amount = ?,
                    end_date = ?, delivery_place = ?, tender_url = ?,
                    matched_keyword = ?, last_seen_at = ?
                WHERE lot_id = ?
                """,
                (
                    record.lot_number,
                    record.trd_buy_id,
                    record.name,
                    record.amount,
                    record.end_date,
                    record.delivery_place,
                    record.tender_url,
                    record.matched_keyword,
                    now,
                    record.lot_id,
                ),
            )
            await self._conn.commit()
            return STATUS_PENDING

        # sent / expired: never mutate protected fields, just note we saw it again
        await self._conn.execute(
            "UPDATE tenders SET last_seen_at = ? WHERE lot_id = ?",
            (now, record.lot_id),
        )
        await self._conn.commit()
        return existing["status"]

    async def refresh_pending_fields(
        self,
        lot_id: int,
        *,
        name: str,
        amount: Optional[float],
        end_date: Optional[str],
        delivery_place: str,
    ) -> None:
        now = _now_iso()
        await self._conn.execute(
            """
            UPDATE tenders
            SET name = ?, amount = ?, end_date = ?, delivery_place = ?, last_seen_at = ?
            WHERE lot_id = ? AND status = ?
            """,
            (name, amount, end_date, delivery_place, now, lot_id, STATUS_PENDING),
        )
        await self._conn.commit()

    async def get_pending_tenders(self) -> list[aiosqlite.Row]:
        cursor = await self._conn.execute(
            "SELECT * FROM tenders WHERE status = ?", (STATUS_PENDING,)
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)

    async def mark_sent(self, lot_id: int) -> None:
        now = _now_iso()
        await self._conn.execute(
            "UPDATE tenders SET status = ?, sent_at = ?, last_seen_at = ? WHERE lot_id = ?",
            (STATUS_SENT, now, now, lot_id),
        )
        await self._conn.commit()

    async def mark_expired(self, lot_id: int) -> None:
        now = _now_iso()
        await self._conn.execute(
            "UPDATE tenders SET status = ?, last_seen_at = ? WHERE lot_id = ?",
            (STATUS_EXPIRED, now, lot_id),
        )
        await self._conn.commit()

    async def is_sent(self, lot_id: int) -> bool:
        row = await self.get_tender(lot_id)
        return row is not None and row["status"] == STATUS_SENT

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
