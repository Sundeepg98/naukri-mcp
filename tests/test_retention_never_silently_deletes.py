"""Retention: a sync never deletes, and a purge never guesses a date.

Context (2026-08-22, measured on the live account). `tools/sync.py` carried a
retention purge that compared `AUTO_PURGE_DAYS=180` against `applied_at`.
`applied_at` is `datetime.now()` at INSERT time -- `record_application` sets it
that way and `upsert_application` never persisted the real `applied_date` the
sync parser extracts -- so it is an insert timestamp for every row in the table.

His 162 applications carried seven batch timestamps, and the purge was set to
delete 149 of them (92%) between 2026-08-27 and 2026-09-08:

    2026-02-28  n= 27  -> 2026-08-27   (cumulative  27)
    2026-03-01  n=  1  -> 2026-08-28   (cumulative  28)
    2026-03-03  n= 67  -> 2026-08-30   (cumulative  95)
    2026-03-12  n= 54  -> 2026-09-08   (cumulative 149)

Silently, with no preview -- and `naukri_status_changes` calls the same sync, so
a read triggered it.

Every test below moves the clock PAST those dates and proves the rows survive.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

import naukri_server.database as database

# Well past 2026-09-08, so every one of his batch timestamps is over the horizon.
WELL_PAST_THE_HORIZON = "2027-01-01T00:00:00+00:00"

# His real shape: seven batch timestamps, no applied_date anywhere.
HIS_DATE_SHAPE = (
    ("2026-02-28T07:18:32.178215+00:00", 27),
    ("2026-03-01T04:21:54.000000+00:00", 1),
    ("2026-03-03T05:17:02.650165+00:00", 67),
    ("2026-03-12T09:02:11.000000+00:00", 54),
    ("2026-04-03T11:39:06.135094+00:00", 2),
    ("2026-08-20T20:58:05.243819+00:00", 10),
    ("2026-08-21T10:39:22.663401+00:00", 1),
)


async def _seed_his_table(tmp_path):
    """Create an isolated DB holding exactly his 162-row date distribution."""
    db_path = tmp_path / "naukri.db"
    with patch.object(database, "DB_PATH", db_path):
        await database.init_db()
        n = 0
        for stamp, count in HIS_DATE_SHAPE:
            for _ in range(count):
                n += 1
                await database.upsert_application({
                    "job_id": "job%04d" % n,
                    "title": "Node Developer %d" % n,
                    "company": "Company %d" % n,
                    "status": "applied",
                    "applied_at": stamp,
                    "source": "naukri_sync",
                })
    return db_path


@pytest.mark.asyncio
async def test_his_162_rows_survive_a_purge_dated_past_every_horizon(tmp_path):
    """THE regression. Clock past 2026-09-08; all 162 rows must remain."""
    db_path = await _seed_his_table(tmp_path)
    with patch.object(database, "DB_PATH", db_path):
        before = await database.list_all_applications()
        assert len(before) == 162, "fixture must reproduce his table"

        deleted = await database.delete_applications_before(WELL_PAST_THE_HORIZON)
        after = await database.list_all_applications()

    assert deleted == 0, "insert timestamps are not apply dates -- nothing is deletable"
    assert len(after) == 162, "he lost %d applications" % (162 - len(after))


@pytest.mark.asyncio
async def test_a_row_with_a_real_apply_date_IS_deletable_and_is_archived(tmp_path):
    """The guard must not be a blanket refusal -- retention still has to work.

    This is the control: same call, same cutoff, one row that carries Naukri's
    real appliedDate. It goes, and it lands in applications_archive.
    """
    db_path = tmp_path / "naukri.db"
    with patch.object(database, "DB_PATH", db_path):
        await database.init_db()
        await database.upsert_application({
            "job_id": "real-1", "title": "Old Role", "company": "Acme",
            "applied_at": "2026-08-01T00:00:00+00:00",     # recent INSERT
            "applied_date": "2025-01-15T00:00:00+00:00",   # genuinely old APPLY
        })
        await database.upsert_application({
            "job_id": "nodate-1", "title": "Unknown", "company": "Beta",
            "applied_at": "2020-01-01T00:00:00+00:00",     # ancient insert stamp
        })

        deleted = await database.delete_applications_before(WELL_PAST_THE_HORIZON)
        remaining = {a["job_id"] for a in await database.list_all_applications()}

        db = await database.get_db()
        try:
            cur = await db.execute("SELECT job_id, archived_reason FROM applications_archive")
            archived = [dict(r) for r in await cur.fetchall()]
        finally:
            await db.close()

    assert deleted == 1
    assert remaining == {"nodate-1"}, "the undated row must be protected"
    assert [a["job_id"] for a in archived] == ["real-1"], "deleted rows must be recoverable"
    assert archived[0]["archived_reason"] == "retention"


@pytest.mark.asyncio
async def test_upsert_persists_the_real_apply_date(tmp_path):
    """`extra` was never written, so applied_date was parsed and thrown away."""
    db_path = tmp_path / "naukri.db"
    with patch.object(database, "DB_PATH", db_path):
        await database.init_db()
        await database.upsert_application({
            "job_id": "j1", "title": "T", "company": "C",
            "applied_at": "2026-08-22T00:00:00+00:00",
            "applied_date": "2026-03-04T00:00:00+00:00",
            "location": "Bengaluru",
        })
        row = (await database.list_all_applications())[0]

    extra = json.loads(row["extra"])
    assert extra["applied_date"] == "2026-03-04T00:00:00+00:00"
    assert extra["location"] == "Bengaluru"
    assert database.real_applied_date(row) == "2026-03-04T00:00:00+00:00"


@pytest.mark.asyncio
async def test_a_later_upsert_without_extra_does_not_erase_the_apply_date(tmp_path):
    """A partial update must not blank a date an earlier sync captured."""
    db_path = tmp_path / "naukri.db"
    with patch.object(database, "DB_PATH", db_path):
        await database.init_db()
        await database.upsert_application({
            "job_id": "j1", "applied_at": "2026-08-22T00:00:00+00:00",
            "applied_date": "2026-03-04T00:00:00+00:00",
        })
        await database.upsert_application({
            "job_id": "j1", "applied_at": "2026-08-22T00:00:00+00:00",
            "status": "viewed_by_recruiter",
        })
        row = (await database.list_all_applications())[0]

    assert database.real_applied_date(row) == "2026-03-04T00:00:00+00:00"
    assert row["status"] == "viewed_by_recruiter"


def test_real_applied_date_returns_none_rather_than_guessing():
    """Every unparseable shape must fail CLOSED (None => never delete)."""
    assert database.real_applied_date({"extra": None}) is None
    assert database.real_applied_date({"extra": "not json"}) is None
    assert database.real_applied_date({"extra": "{}"}) is None
    assert database.real_applied_date({"extra": '{"applied_date": ""}'}) is None
    assert database.real_applied_date({"extra": '{"applied_date": "sometime"}'}) is None
    assert database.real_applied_date({"extra": '{"applied_date": 0}'}) is None
    # ...and the shapes it must UNDERSTAND, or the feature is dead weight.
    assert database.real_applied_date(
        {"extra": '{"applied_date": "12 Mar 2026"}'}).startswith("2026-03-12")
    assert database.real_applied_date(
        {"extra": '{"applied_date": "2026-03-12"}'}).startswith("2026-03-12")
    assert database.real_applied_date(
        {"extra": '{"applied_date": 1772236800000}'}).startswith("2026-02-28")


@pytest.mark.asyncio
async def test_the_sync_saga_has_no_purge_step_and_deletes_nothing(tmp_path):
    """A read must never delete. naukri_status_changes runs this same sync."""
    from naukri_server.tools import sync as sync_mod

    db_path = await _seed_his_table(tmp_path)
    remote = [{"job_id": "job0001", "title": "Node Developer 1",
               "company": "Company 1", "status": "applied"}]

    with patch.object(database, "DB_PATH", db_path), \
         patch.object(sync_mod, "_fetch_applied_jobs_rest",
                      new_callable=AsyncMock, return_value=remote), \
         patch.object(sync_mod, "_save_sync_state_async", new_callable=AsyncMock):
        result = await sync_mod._sync_applications(days_back=365)
        after = await database.list_all_applications()

    assert result["status"] == "success", result
    assert "purge" not in result.get("saga_steps", []), result.get("saga_steps")
    assert result["purged"] == 0
    assert len(after) == 162, "the sync deleted %d rows" % (162 - len(after))


@pytest.mark.asyncio
async def test_purge_preview_and_deletion_cannot_disagree(tmp_path):
    """A dry run that reports N and then removes a different N is its own bug."""
    from naukri_server.services.application_service import purge_applications

    db_path = await _seed_his_table(tmp_path)
    with patch.object(database, "DB_PATH", db_path):
        preview = await purge_applications(WELL_PAST_THE_HORIZON, dry_run=True)
        real = await purge_applications(WELL_PAST_THE_HORIZON, dry_run=False)
        after = await database.list_all_applications()

    assert preview["purged_count"] == real["purged_count"] == 0
    assert preview["protected_no_apply_date"] == 162
    assert len(after) == 162
    assert "insert timestamp" in preview["note"]
