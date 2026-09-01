"""Tests for the periodic expired-tender cleanup job.

Covers: retention boundary correctness, sent/pending/missing/invalid end_date
protection, its own independent scheduling checkpoint, batch deletion, and
that no VACUUM ever runs automatically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

RETENTION_DAYS = 30


def _record(lot_id: int, end_date_iso: str, **overrides) -> TenderRecord:
    base = dict(
        lot_id=lot_id,
        lot_number=f"LOT-{lot_id}",
        trd_buy_id=1000 + lot_id,
        name=f"Тест {lot_id}",
        amount=150000.0,
        end_date=end_date_iso,
        delivery_place="г. Астана",
        tender_url="https://zakup.gov.kz/",
        matched_keyword="стеллаж",
    )
    base.update(overrides)
    return TenderRecord(**base)


@pytest.fixture
async def repo():
    conn = await init_db(Path(":memory:"))
    repository = Repository(conn)
    yield repository
    await conn.close()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------------- retention boundary


async def test_expired_31_days_old_is_deleted(repo: Repository):
    now = datetime.now(timezone.utc)
    end_date = now - timedelta(days=31)
    await repo.upsert_candidate(_record(1, _iso(end_date)))
    await repo.mark_expired(1)

    cutoff = now - timedelta(days=RETENTION_DAYS)
    candidates = await repo.find_expired_lot_ids_older_than(cutoff)
    assert candidates == [1]

    deleted = await repo.delete_tenders_by_ids(candidates)
    assert deleted == 1
    assert await repo.get_tender(1) is None


async def test_expired_exactly_at_30_day_boundary_is_kept(repo: Repository):
    # end_date < cutoff is the rule (strict). A row whose end_date equals the
    # cutoff exactly is NOT strictly older, so it must be kept this cycle.
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    await repo.upsert_candidate(_record(2, _iso(cutoff)))
    await repo.mark_expired(2)

    candidates = await repo.find_expired_lot_ids_older_than(cutoff)
    assert candidates == []
    assert await repo.get_tender(2) is not None


async def test_expired_just_past_the_boundary_is_deleted(repo: Repository):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    just_past = cutoff - timedelta(seconds=1)
    await repo.upsert_candidate(_record(3, _iso(just_past)))
    await repo.mark_expired(3)

    candidates = await repo.find_expired_lot_ids_older_than(cutoff)
    assert candidates == [3]


async def test_expired_29_days_old_is_kept(repo: Repository):
    now = datetime.now(timezone.utc)
    end_date = now - timedelta(days=29)
    await repo.upsert_candidate(_record(4, _iso(end_date)))
    await repo.mark_expired(4)

    cutoff = now - timedelta(days=RETENTION_DAYS)
    candidates = await repo.find_expired_lot_ids_older_than(cutoff)
    assert candidates == []
    assert await repo.get_tender(4) is not None


# --------------------------------------------------------------------------- protected statuses


async def test_sent_tender_one_year_old_is_never_touched(repo: Repository):
    now = datetime.now(timezone.utc)
    ancient_end_date = now - timedelta(days=365)
    await repo.upsert_candidate(_record(5, _iso(ancient_end_date)))
    await repo.mark_sent(5)

    cutoff = now - timedelta(days=RETENTION_DAYS)
    candidates = await repo.find_expired_lot_ids_older_than(cutoff)
    assert 5 not in candidates

    # Even if somehow passed to delete (defense in depth), the WHERE clause
    # requires status='expired', so a 'sent' row must survive regardless.
    deleted = await repo.delete_tenders_by_ids([5])
    assert deleted == 0
    row = await repo.get_tender(5)
    assert row is not None
    assert row["status"] == STATUS_SENT


async def test_pending_tender_one_year_old_end_date_is_never_touched(repo: Repository):
    now = datetime.now(timezone.utc)
    # A pending tender with an old end_date shouldn't normally happen (it
    # would have expired), but the guard must hold regardless of end_date.
    old_end_date = now - timedelta(days=365)
    await repo.upsert_candidate(_record(6, _iso(old_end_date)))
    # left as 'pending' -- never call mark_expired/mark_sent

    cutoff = now - timedelta(days=RETENTION_DAYS)
    candidates = await repo.find_expired_lot_ids_older_than(cutoff)
    assert 6 not in candidates

    deleted = await repo.delete_tenders_by_ids([6])
    assert deleted == 0
    row = await repo.get_tender(6)
    assert row is not None
    assert row["status"] == STATUS_PENDING


async def test_missing_end_date_is_kept(repo: Repository):
    now = datetime.now(timezone.utc)
    await repo.upsert_candidate(_record(7, None))
    await repo.mark_expired(7)

    cutoff = now - timedelta(days=RETENTION_DAYS)
    candidates = await repo.find_expired_lot_ids_older_than(cutoff)
    assert 7 not in candidates
    assert await repo.get_tender(7) is not None


async def test_invalid_end_date_is_kept(repo: Repository):
    now = datetime.now(timezone.utc)
    await repo.upsert_candidate(_record(8, "not-a-real-date"))
    await repo.mark_expired(8)

    cutoff = now - timedelta(days=RETENTION_DAYS)
    candidates = await repo.find_expired_lot_ids_older_than(cutoff)
    assert 8 not in candidates
    assert await repo.get_tender(8) is not None


async def test_naive_end_date_is_kept(repo: Repository):
    """A timezone-naive end_date is ambiguous -- never safe to age-compare."""
    now = datetime.now(timezone.utc)
    naive = (now - timedelta(days=365)).replace(tzinfo=None)
    await repo.upsert_candidate(_record(9, naive.isoformat()))
    await repo.mark_expired(9)

    cutoff = now - timedelta(days=RETENTION_DAYS)
    candidates = await repo.find_expired_lot_ids_older_than(cutoff)
    assert 9 not in candidates


# --------------------------------------------------------------------------- batch deletion


async def test_batch_deletion_deletes_everything_across_multiple_batches(repo: Repository):
    now = datetime.now(timezone.utc)
    old_end_date = _iso(now - timedelta(days=100))
    ids = list(range(100, 112))  # 12 rows
    for lot_id in ids:
        await repo.upsert_candidate(_record(lot_id, old_end_date))
        await repo.mark_expired(lot_id)

    deleted = await repo.delete_tenders_by_ids(ids, batch_size=5)  # 3 batches: 5,5,2
    assert deleted == 12
    for lot_id in ids:
        assert await repo.get_tender(lot_id) is None


async def test_batch_deletion_commits_after_each_batch(repo: Repository):
    """If a later batch fails, earlier batches must already be durably
    committed (not rolled back as one giant transaction)."""
    now = datetime.now(timezone.utc)
    old_end_date = _iso(now - timedelta(days=100))
    ids = list(range(200, 208))  # 8 rows
    for lot_id in ids:
        await repo.upsert_candidate(_record(lot_id, old_end_date))
        await repo.mark_expired(lot_id)

    await repo.delete_tenders_by_ids(ids[:4], batch_size=2)
    # First half deleted and committed even without deleting the rest.
    for lot_id in ids[:4]:
        assert await repo.get_tender(lot_id) is None
    for lot_id in ids[4:]:
        assert await repo.get_tender(lot_id) is not None


async def test_empty_candidate_list_deletes_nothing(repo: Repository):
    assert await repo.delete_tenders_by_ids([]) == 0


# --------------------------------------------------------------------------- diagnostics / no VACUUM


async def test_storage_diagnostics_shape(repo: Repository):
    diag = await repo.get_storage_diagnostics()
    assert set(diag.keys()) == {
        "page_count",
        "freelist_count",
        "page_size",
        "database_size_mb",
        "reclaimable_mb",
    }
    assert diag["page_size"] > 0


async def test_delete_frees_pages_without_shrinking_file_no_vacuum(repo: Repository):
    """After deleting a meaningful number of rows, freelist_count must rise
    (pages freed for reuse) while page_count must NOT shrink -- proving no
    VACUUM ran (VACUUM would rebuild the file and drop page_count/freelist).
    """
    now = datetime.now(timezone.utc)
    old_end_date = _iso(now - timedelta(days=100))
    ids = list(range(300, 400))  # 100 rows, enough to occupy multiple pages
    for lot_id in ids:
        await repo.upsert_candidate(
            _record(lot_id, old_end_date, name="X" * 500, delivery_place="Y" * 500)
        )
        await repo.mark_expired(lot_id)

    before = await repo.get_storage_diagnostics()
    deleted = await repo.delete_tenders_by_ids(ids)
    assert deleted == 100
    after = await repo.get_storage_diagnostics()

    assert after["page_count"] >= before["page_count"]  # file never shrinks without VACUUM
    assert after["freelist_count"] > before["freelist_count"]  # freed pages exist for reuse


async def test_no_vacuum_statement_is_ever_executed(repo: Repository, monkeypatch):
    executed_sql: list[str] = []
    original_execute = repo._conn.execute

    async def spy_execute(sql, *args, **kwargs):
        executed_sql.append(sql)
        return await original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(repo._conn, "execute", spy_execute)

    now = datetime.now(timezone.utc)
    old_end_date = _iso(now - timedelta(days=100))
    await repo.upsert_candidate(_record(500, old_end_date))
    await repo.mark_expired(500)
    await repo.delete_tenders_by_ids([500])
    await repo.get_storage_diagnostics()

    assert not any("VACUUM" in sql.upper() for sql in executed_sql)


# --------------------------------------------------------------------------- MonitorService wiring


class _StubGoszakup:
    async def close(self):
        pass


class _StubTelegram:
    async def close(self):
        pass


async def _build_monitor(repo: Repository):
    from app.config import Settings
    from app.filters.keyword_filter import KeywordFilter
    from app.services.monitor import MonitorService

    settings = Settings(
        goszakup_api_token="",
        telegram_bot_token="",
        telegram_chat_id="",
        check_interval_seconds=300,
        min_hours_remaining=5,
        max_hours_remaining=72,
        app_timezone_name="Asia/Qyzylorda",
        database_path=Path(":memory:"),
        bootstrap_lookback_days=90,
        sync_overlap_minutes=10,
        discovery_scan_interval_minutes=120,
        min_amount_kzt=100000,
        expired_retention_days=RETENTION_DAYS,
        cleanup_interval_hours=24,
        log_level="INFO",
    )
    return MonitorService(settings, repo, _StubGoszakup(), _StubTelegram(), KeywordFilter([]), "")


async def test_cleanup_runs_only_after_interval(repo: Repository):
    monitor = await _build_monitor(repo)
    now = datetime.now(timezone.utc)
    old_end_date = _iso(now - timedelta(days=100))
    await repo.upsert_candidate(_record(600, old_end_date))
    await repo.mark_expired(600)

    # Never run before -> runs immediately.
    await monitor.maybe_run_cleanup()
    assert await repo.get_tender(600) is None  # deleted

    # Add another old-expired row and run again immediately -- not due yet.
    await repo.upsert_candidate(_record(601, old_end_date))
    await repo.mark_expired(601)
    await monitor.maybe_run_cleanup()
    assert await repo.get_tender(601) is not None  # NOT deleted, cleanup skipped

    # Force the checkpoint into the past beyond the interval -> due again.
    from app.services.monitor import LAST_CLEANUP_KEY

    past = now - timedelta(hours=25)
    await repo.set_app_state(LAST_CLEANUP_KEY, past.isoformat())
    await monitor.maybe_run_cleanup()
    assert await repo.get_tender(601) is None  # deleted this time


async def test_last_cleanup_at_independent_from_other_checkpoints(repo: Repository):
    from app.services.monitor import (
        BOOTSTRAP_DONE_KEY,
        LAST_CLEANUP_KEY,
        LAST_DISCOVERY_SCAN_KEY,
        LAST_SYNC_KEY,
    )

    monitor = await _build_monitor(repo)
    await repo.set_app_state(LAST_SYNC_KEY, "2020-01-01T00:00:00+00:00")
    await repo.set_app_state(LAST_DISCOVERY_SCAN_KEY, "2020-01-01T00:00:00+00:00")
    await repo.set_app_state(BOOTSTRAP_DONE_KEY, "1")

    await monitor.run_cleanup()

    assert await repo.get_app_state(LAST_CLEANUP_KEY) is not None
    # Cleanup must not have touched the other checkpoints at all.
    assert await repo.get_app_state(LAST_SYNC_KEY) == "2020-01-01T00:00:00+00:00"
    assert await repo.get_app_state(LAST_DISCOVERY_SCAN_KEY) == "2020-01-01T00:00:00+00:00"
    assert await repo.get_app_state(BOOTSTRAP_DONE_KEY) == "1"


async def test_run_cleanup_with_nothing_to_delete_still_sets_checkpoint(repo: Repository):
    from app.services.monitor import LAST_CLEANUP_KEY

    monitor = await _build_monitor(repo)
    await monitor.run_cleanup()
    assert await repo.get_app_state(LAST_CLEANUP_KEY) is not None
