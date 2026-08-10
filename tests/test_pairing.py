from pathlib import Path

import pytest

from app.database.db import init_db
from app.database.repository import Repository
from app.telegram import pairing
from app.telegram.pairing import pair_owner, resolve_owner_chat_id


class FakeTelegramClient:
    """Stands in for TelegramClient: no real network access."""

    def __init__(self, update_batches: list[list[dict]]):
        self._batches = update_batches
        self.sent: list[tuple[str, str]] = []

    async def get_updates(self, offset=None, timeout=0):
        if self._batches:
            return self._batches.pop(0)
        return []

    async def send_message(self, chat_id, text, parse_mode="HTML"):
        self.sent.append((chat_id, text))
        return {"message_id": 1}


@pytest.fixture
async def repo():
    conn = await init_db(Path(":memory:"))
    repository = Repository(conn)
    yield repository
    await conn.close()


async def test_configured_chat_id_takes_priority(repo: Repository):
    chat_id = await resolve_owner_chat_id("999999", repo)
    assert chat_id == "999999"


async def test_stored_owner_chat_id_used_when_no_env_override(repo: Repository):
    await repo.set_setting("owner_chat_id", "42")
    chat_id = await resolve_owner_chat_id("", repo)
    assert chat_id == "42"


async def test_resolve_returns_none_when_nothing_available(repo: Repository):
    assert await resolve_owner_chat_id("", repo) is None


async def test_pairing_ignores_backlog_and_registers_first_new_start(repo: Repository):
    backlog = [{"update_id": 100, "message": {"chat": {"id": 1, "type": "private"}, "text": "/start"}}]
    fresh = [
        {"update_id": 102, "message": {"chat": {"id": 2, "type": "group"}, "text": "/start"}},
        {"update_id": 103, "message": {"chat": {"id": 3, "type": "private"}, "text": "hello"}},
        {"update_id": 104, "message": {"chat": {"id": 4, "type": "private"}, "text": "/start"}},
    ]
    fake = FakeTelegramClient([backlog, fresh])

    chat_id = await pair_owner(fake, repo)

    assert chat_id == "4"
    assert await repo.get_setting("owner_chat_id") == "4"
    assert fake.sent == [("4", "✅ GosZakup monitoring connected.")]


async def test_pairing_polls_again_when_no_start_found_yet(repo: Repository, monkeypatch):
    monkeypatch.setattr(pairing, "POLL_INTERVAL_SECONDS", 0)
    backlog: list[dict] = []
    empty_round: list[dict] = []
    fresh = [{"update_id": 5, "message": {"chat": {"id": 7, "type": "private"}, "text": "/start"}}]
    fake = FakeTelegramClient([backlog, empty_round, fresh])

    chat_id = await pair_owner(fake, repo)
    assert chat_id == "7"
