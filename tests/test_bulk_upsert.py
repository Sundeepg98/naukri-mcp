"""Persisting a sync opened one DB connection per application.

`upsert_application` calls `get_db()`, which opens a fresh aiosqlite
connection, runs three PRAGMAs, writes, commits and closes. A sync persists
every application it knows about, so on his 162 rows that was 162 connections
against a file the scheduler, the browser watchdog and any live tool are also
using. On 2026-08-22 it timed out a real sync at the `persist` saga step after
30s.

Worth stating precisely, because it was checked before anything was changed:
the per-row cost is NOT the `extra` column the retention fix added. 162 rows
benchmarked at 1.79s WITH extra against 1.98s without -- free. The cost is the
connection churn. Measured after this change: 1.77s -> 0.02s, ~84x.
"""

from unittest.mock import patch

import pytest

import naukri_server.database as database


async def _fresh_db(tmp_path):
    db_path = tmp_path / "naukri.db"
    with patch.object(database, "DB_PATH", db_path):
        await database.init_db()
    return db_path


def _rows(n, **extra):
    return [
        {"job_id": "j%03d" % i, "title": "T%d" % i, "company": "C%d" % i,
         "status": "applied", "applied_at": "2026-08-01T00:00:00+00:00", **extra}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_bulk_upsert_writes_every_row(tmp_path):
    db_path = await _fresh_db(tmp_path)
    with patch.object(database, "DB_PATH", db_path):
        written = await database.upsert_applications(_rows(162))
        rows = await database.list_all_applications()
    assert written == 162
    assert len(rows) == 162


@pytest.mark.asyncio
async def test_it_opens_ONE_connection_for_the_whole_batch(tmp_path):
    """THE regression: 162 rows used to mean 162 connections."""
    db_path = await _fresh_db(tmp_path)
    real_get_db = database.get_db
    calls = []

    async def counting_get_db():
        calls.append(1)
        return await real_get_db()

    with patch.object(database, "DB_PATH", db_path), \
         patch.object(database, "get_db", counting_get_db):
        await database.upsert_applications(_rows(162))

    assert len(calls) == 1, "opened %d connections for one batch" % len(calls)


@pytest.mark.asyncio
async def test_bulk_and_single_produce_identical_rows(tmp_path):
    """The fast path must not be a different path."""
    single_path = tmp_path / "single.db"
    bulk_path = tmp_path / "bulk.db"
    payload = _rows(5, applied_date="2026-03-04", location="Bengaluru")

    with patch.object(database, "DB_PATH", single_path):
        await database.init_db()
        for r in payload:
            await database.upsert_application(r)
        single = await database.list_all_applications()

    with patch.object(database, "DB_PATH", bulk_path):
        await database.init_db()
        await database.upsert_applications(payload)
        bulk = await database.list_all_applications()

    assert [dict(r) for r in single] == [dict(r) for r in bulk]


@pytest.mark.asyncio
async def test_bulk_upsert_updates_in_place_and_keeps_the_apply_date(tmp_path):
    """The COALESCE guard must survive the batch path too."""
    db_path = await _fresh_db(tmp_path)
    with patch.object(database, "DB_PATH", db_path):
        await database.upsert_applications(
            [{"job_id": "j1", "applied_at": "2026-08-01T00:00:00+00:00",
              "applied_date": "2026-03-04"}])
        # a later sync carries no extra fields
        await database.upsert_applications(
            [{"job_id": "j1", "applied_at": "2026-08-01T00:00:00+00:00",
              "status": "viewed_by_recruiter"}])
        rows = await database.list_all_applications()

    assert len(rows) == 1, "upsert must update, not duplicate"
    assert rows[0]["status"] == "viewed_by_recruiter"
    assert database.real_applied_date(rows[0]) == "2026-03-04T00:00:00+00:00"


@pytest.mark.asyncio
async def test_empty_batch_is_a_no_op_and_opens_nothing(tmp_path):
    db_path = await _fresh_db(tmp_path)
    calls = []

    async def counting_get_db():
        calls.append(1)
        raise AssertionError("should not connect for an empty batch")

    with patch.object(database, "DB_PATH", db_path), \
         patch.object(database, "get_db", counting_get_db):
        assert await database.upsert_applications([]) == 0
    assert not calls


@pytest.mark.asyncio
async def test_the_sync_persist_step_uses_the_batch_path(tmp_path):
    """Pin the call site, not just the helper -- persist is where it timed out."""
    from unittest.mock import AsyncMock

    from naukri_server.tools import sync as sync_mod

    db_path = await _fresh_db(tmp_path)
    remote = [{"job_id": "j1", "title": "T", "company": "C", "status": "applied"}]

    with patch.object(database, "DB_PATH", db_path), \
         patch.object(sync_mod, "_fetch_applied_jobs_rest",
                      new_callable=AsyncMock, return_value=remote), \
         patch.object(sync_mod, "_save_sync_state_async", new_callable=AsyncMock), \
         patch.object(database, "upsert_applications",
                      new_callable=AsyncMock, return_value=1) as bulk, \
         patch.object(database, "upsert_application",
                      new_callable=AsyncMock) as single:
        result = await sync_mod._sync_applications(days_back=365)

    assert result["status"] == "success", result
    bulk.assert_awaited_once()
    single.assert_not_awaited(), "persist fell back to the row-at-a-time path"
