from pathlib import Path

import pytest

from app.database.db import init_db
from app.database.repository import Repository


@pytest.fixture
async def repo():
    conn = await init_db(Path(":memory:"))
    repository = Repository(conn)
    yield repository
    await conn.close()


async def test_lot_is_not_sent_by_default(repo: Repository):
    assert await repo.is_sent(1) is False


async def test_mark_sent_records_lot_id(repo: Repository):
    await repo.mark_sent(1)
    assert await repo.is_sent(1) is True


async def test_marking_sent_twice_is_idempotent(repo: Repository):
    await repo.mark_sent(1)
    await repo.mark_sent(1)  # must not raise (PRIMARY KEY, INSERT OR IGNORE)
    assert await repo.is_sent(1) is True


async def test_other_lot_ids_are_unaffected(repo: Repository):
    await repo.mark_sent(1)
    assert await repo.is_sent(2) is False


async def test_app_state_roundtrip(repo: Repository):
    assert await repo.get_app_state("last_successful_sync") is None
    await repo.set_app_state("last_successful_sync", "2026-08-10T00:00:00+00:00")
    assert await repo.get_app_state("last_successful_sync") == "2026-08-10T00:00:00+00:00"


async def test_settings_roundtrip(repo: Repository):
    assert await repo.get_setting("owner_chat_id") is None
    await repo.set_setting("owner_chat_id", "123456")
    assert await repo.get_setting("owner_chat_id") == "123456"
