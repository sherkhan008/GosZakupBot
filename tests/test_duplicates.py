from pathlib import Path

import pytest

from app.database.db import init_db
from app.database.repository import (
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_SENT,
    Repository,
    TenderRecord,
)


@pytest.fixture
async def repo():
    conn = await init_db(Path(":memory:"))
    repository = Repository(conn)
    yield repository
    await conn.close()


def _record(lot_id: int = 1, **overrides) -> TenderRecord:
    base = dict(
        lot_id=lot_id,
        lot_number="LOT-1",
        trd_buy_id=100,
        name="Стеллаж металлический",
        amount=100000.0,
        end_date="2026-08-12T14:00:00+05:00",
        delivery_place="г. Астана",
        tender_url="https://goszakup.gov.kz/ru/announce/index/100?tab=lots",
        matched_keyword="стеллаж",
    )
    base.update(overrides)
    return TenderRecord(**base)


async def test_new_candidate_inserted_as_pending(repo: Repository):
    status = await repo.upsert_candidate(_record())
    assert status == STATUS_PENDING
    row = await repo.get_tender(1)
    assert row["status"] == STATUS_PENDING


async def test_sent_lot_is_never_sent_again(repo: Repository):
    await repo.upsert_candidate(_record())
    await repo.mark_sent(1)
    assert await repo.is_sent(1) is True

    # Even if the lot reappears with changed name/amount/deadline, re-ingesting
    # it must NOT flip status away from 'sent' nor mutate protected fields.
    updated = _record(name="ДРУГОЕ НАЗВАНИЕ", amount=999999.0, end_date="2030-01-01T00:00:00+05:00")
    status = await repo.upsert_candidate(updated)
    assert status == STATUS_SENT

    row = await repo.get_tender(1)
    assert row["status"] == STATUS_SENT
    assert row["name"] == "Стеллаж металлический"  # unchanged
    assert row["amount"] == 100000.0  # unchanged
    assert row["sent_at"] is not None


async def test_expired_lot_is_never_sent(repo: Repository):
    await repo.upsert_candidate(_record(lot_id=2))
    await repo.mark_expired(2)
    row = await repo.get_tender(2)
    assert row["status"] == STATUS_EXPIRED
    assert await repo.is_sent(2) is False

    # Re-ingesting an expired lot must not resurrect it into pending/sent.
    status = await repo.upsert_candidate(_record(lot_id=2, name="Обновлено"))
    assert status == STATUS_EXPIRED
    row = await repo.get_tender(2)
    assert row["name"] != "Обновлено"


async def test_pending_row_refreshes_mutable_fields(repo: Repository):
    await repo.upsert_candidate(_record(lot_id=3, amount=1000.0))
    status = await repo.upsert_candidate(_record(lot_id=3, amount=2000.0))
    assert status == STATUS_PENDING
    row = await repo.get_tender(3)
    assert row["amount"] == 2000.0


async def test_get_pending_tenders_lists_only_pending(repo: Repository):
    await repo.upsert_candidate(_record(lot_id=4))
    await repo.upsert_candidate(_record(lot_id=5))
    await repo.mark_sent(5)
    pending = await repo.get_pending_tenders()
    ids = {row["lot_id"] for row in pending}
    assert ids == {4}


async def test_app_state_roundtrip(repo: Repository):
    assert await repo.get_app_state("last_successful_sync") is None
    await repo.set_app_state("last_successful_sync", "2026-08-10T00:00:00+00:00")
    assert await repo.get_app_state("last_successful_sync") == "2026-08-10T00:00:00+00:00"


async def test_settings_roundtrip(repo: Repository):
    assert await repo.get_setting("owner_chat_id") is None
    await repo.set_setting("owner_chat_id", "123456")
    assert await repo.get_setting("owner_chat_id") == "123456"
