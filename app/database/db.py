"""SQLite schema initialization and connection helper."""
from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    lot_id INTEGER PRIMARY KEY,
    lot_number TEXT,
    trd_buy_id INTEGER,
    name TEXT,
    amount REAL,
    end_date TEXT,
    delivery_place TEXT,
    tender_url TEXT,
    matched_keyword TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tenders_status ON tenders (status);
CREATE INDEX IF NOT EXISTS idx_tenders_trd_buy_id ON tenders (trd_buy_id);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


async def init_db(database_path: Path) -> aiosqlite.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(database_path))
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    await conn.commit()
    logger.info("SQLite connected: %s", database_path)
    return conn
