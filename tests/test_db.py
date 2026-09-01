"""Schema/clean-start tests for the minimal storage architecture.

Covers the two requirements a Railway clean reset depends on: (1) init_db
creates a brand new database from nothing, with no dependency on any prior
schema/file, and (2) the only per-tender state ever persisted (sent_lots)
carries exactly lot_id + sent_at -- nothing else.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.database.db import init_db
from app.database.repository import Repository


async def test_fresh_database_is_created_from_a_nonexistent_file(tmp_path):
    """Simulates the Railway clean-reset scenario: no tenders.db file (and no
    old schema) exists at all before start -- init_db must create one from
    scratch without error.
    """
    db_path = tmp_path / "data" / "tenders.db"
    assert not db_path.exists()

    conn = await init_db(db_path)
    try:
        assert db_path.exists()
    finally:
        await conn.close()


async def test_new_database_contains_only_the_three_minimal_tables(tmp_path):
    conn = await init_db(tmp_path / "tenders.db")
    try:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        table_names = {row["name"] for row in rows}
        assert table_names == {"sent_lots", "app_state", "settings"}
    finally:
        await conn.close()


async def test_sent_lots_table_has_only_lot_id_and_sent_at_columns(tmp_path):
    conn = await init_db(tmp_path / "tenders.db")
    try:
        cursor = await conn.execute("PRAGMA table_info(sent_lots)")
        columns_info = await cursor.fetchall()
        await cursor.close()
        column_names = {row["name"] for row in columns_info}
        assert column_names == {"lot_id", "sent_at"}
    finally:
        await conn.close()


async def test_sent_lots_row_stores_only_lot_id_and_sent_at(tmp_path):
    conn = await init_db(tmp_path / "tenders.db")
    try:
        repo = Repository(conn)
        await repo.mark_sent(42)

        cursor = await conn.execute("SELECT * FROM sent_lots WHERE lot_id = 42")
        row = await cursor.fetchone()
        await cursor.close()

        assert set(row.keys()) == {"lot_id", "sent_at"}
        assert row["lot_id"] == 42
        assert row["sent_at"] is not None
    finally:
        await conn.close()


async def test_reinitializing_an_existing_fresh_database_is_idempotent(tmp_path):
    """init_db must be safe to call again against a database it already
    created (e.g. on process restart) without requiring any migration."""
    db_path = tmp_path / "tenders.db"
    conn1 = await init_db(db_path)
    repo1 = Repository(conn1)
    await repo1.mark_sent(1)
    await conn1.close()

    conn2 = await init_db(db_path)
    try:
        repo2 = Repository(conn2)
        assert await repo2.is_sent(1) is True
    finally:
        await conn2.close()
