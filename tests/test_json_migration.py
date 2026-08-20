"""Tests for the guarded JSON -> SQLite migration in naukri_server.database.

Covers the migration ledger (one-shot safety), INSERT-OR-IGNORE duplicate
safety under force=True, accurate per-entity read/inserted/skipped counts, and
the `extra` JSON blob that catches applications.json fields the applications
table has no column for.

Every test runs against a tmp_path database. Nothing here ever touches the
real project-root naukri.db or the real applications.json.
"""

import json
import aiosqlite
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


MIGRATION_NAME = "json_to_sqlite_v1"


# ---------------------------------------------------------------------------
# Helpers -- mirror the fixture style of tests/test_database.py
# ---------------------------------------------------------------------------


@contextmanager
def _migration_env(tmp: Path, apps=None, saved=None, reminders=None, rounds=None):
    """Point DB_PATH and the four JSON file constants at tmp_path.

    Any entity passed as None gets NO file written, exercising the
    missing-file path (load_json_with_backup returns []).
    """
    files = {}
    for name, data in (
        ("applications.json", apps),
        ("saved_jobs.json", saved),
        ("reminders.json", reminders),
        ("interview_rounds.json", rounds),
    ):
        path = tmp / name
        if data is not None:
            path.write_text(json.dumps(data), encoding="utf-8")
        files[name] = path

    with patch("naukri_server.database.DB_PATH", tmp / "naukri.db"), \
         patch("naukri_server.database.DATA_DIR", tmp), \
         patch("naukri_server.config.APPLICATIONS_FILE", files["applications.json"]), \
         patch("naukri_server.config.SAVED_JOBS_FILE", files["saved_jobs.json"]), \
         patch("naukri_server.config.REMINDERS_FILE", files["reminders.json"]), \
         patch("naukri_server.config.INTERVIEW_ROUNDS_FILE", files["interview_rounds.json"]):
        yield files


async def _open_db(db_path: Path) -> aiosqlite.Connection:
    """Open a raw connection to the test database."""
    db = aiosqlite.connect(str(db_path))
    await db
    db.row_factory = aiosqlite.Row
    return db


async def _count(db_path: Path, table: str) -> int:
    db = await _open_db(db_path)
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM " + table)
        row = await cursor.fetchone()
        return row[0]
    finally:
        await db.close()


def _app(job_id, **over):
    """Minimal applications.json record."""
    rec = {
        "job_id": job_id,
        "title": "Backend Dev " + job_id,
        "company": "Acme",
        "status": "applied",
        "applied_at": "2026-04-01T10:00:00",
        "source": "single",
    }
    rec.update(over)
    return rec


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


async def test_migration_inserts_exactly_n_rows_and_reports_n(tmp_path):
    """N JSON records -> exactly N rows, and inserted == N.

    The reported `inserted` is cross-checked against the real COUNT(*) delta,
    which is what proves cursor.rowcount is an accurate insert counter rather
    than a plausible-looking number.
    """
    apps = [_app("J%03d" % i) for i in range(1, 8)]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=[], reminders=[], rounds=[]):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        before = await _count(db_path, "applications")
        result = await migrate_json_to_sqlite()
        after = await _count(db_path, "applications")

    assert result["status"] == "applied"
    assert result["applications"] == {"read": 7, "inserted": 7, "skipped": 0}
    assert after == 7
    # rowcount-derived count must equal the observed table delta
    assert result["applications"]["inserted"] == after - before


async def test_counts_are_reported_for_all_four_entities(tmp_path):
    """Each of the four entities reports its own read/inserted/skipped block."""
    apps = [_app("J001"), _app("J002")]
    saved = [{"job_id": "S1", "title": "Saved One", "company": "Beta"}]
    reminders = [
        {"job_id": "R1", "title": "Rem One", "company": "Gamma",
         "remind_at": "2026-05-01", "note": "ping", "created_at": "2026-04-01"},
        {"job_id": "R2", "title": "Rem Two", "company": "Delta",
         "remind_at": "2026-05-02", "note": "ping", "created_at": "2026-04-02"},
        {"job_id": "R3", "title": "Rem Three", "company": "Delta",
         "remind_at": "2026-05-03", "note": "ping", "created_at": "2026-04-03"},
    ]
    rounds = [
        {"job_id": "J001", "round_type": "screen", "date": "2026-05-05",
         "notes": "n", "status": "scheduled", "created_at": "2026-04-05"},
    ]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=saved,
                        reminders=reminders, rounds=rounds):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        result = await migrate_json_to_sqlite()

    assert result["applications"] == {"read": 2, "inserted": 2, "skipped": 0}
    assert result["saved_jobs"] == {"read": 1, "inserted": 1, "skipped": 0}
    assert result["reminders"] == {"read": 3, "inserted": 3, "skipped": 0}
    assert result["interview_rounds"] == {"read": 1, "inserted": 1, "skipped": 0}

    assert await _count(db_path, "applications") == 2
    assert await _count(db_path, "saved_jobs") == 1
    assert await _count(db_path, "reminders") == 3
    assert await _count(db_path, "interview_rounds") == 1


# ---------------------------------------------------------------------------
# Duplicate safety
# ---------------------------------------------------------------------------


async def test_force_rerun_never_duplicates_and_reports_zero_inserted(tmp_path):
    """The real duplicate-safety proof: force=True twice leaves exactly N rows.

    The ledger is bypassed on both runs, so the second run genuinely re-reads
    the JSON and re-issues every INSERT. Row count staying at N (and inserted
    dropping to 0 / skipped rising to N) is what shows INSERT OR IGNORE is
    doing the work, not the ledger.
    """
    apps = [_app("J%03d" % i) for i in range(1, 6)]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=[], reminders=[], rounds=[]):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        first = await migrate_json_to_sqlite(force=True)
        second = await migrate_json_to_sqlite(force=True)

    assert first["applications"] == {"read": 5, "inserted": 5, "skipped": 0}
    assert second["status"] == "applied"
    assert second["applications"] == {"read": 5, "inserted": 0, "skipped": 5}
    assert await _count(db_path, "applications") == 5


async def test_force_rerun_never_duplicates_interview_rounds(tmp_path):
    """interview_rounds has an AUTOINCREMENT id, so it needs a natural-key guard.

    Without one, INSERT OR IGNORE can never conflict there and a forced re-run
    would double every row. See the DEVIATION note in the slice report.
    """
    rounds = [
        {"job_id": "J001", "round_type": "screen", "date": "2026-05-05",
         "notes": "n1", "status": "scheduled", "created_at": "2026-04-05"},
        {"job_id": "J001", "round_type": "tech", "date": "2026-05-09",
         "notes": "n2", "status": "scheduled", "created_at": "2026-04-06"},
    ]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=[], saved=[], reminders=[], rounds=rounds):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        first = await migrate_json_to_sqlite(force=True)
        second = await migrate_json_to_sqlite(force=True)

    assert first["interview_rounds"] == {"read": 2, "inserted": 2, "skipped": 0}
    assert second["interview_rounds"] == {"read": 2, "inserted": 0, "skipped": 2}
    assert await _count(db_path, "interview_rounds") == 2


async def test_existing_row_with_different_status_is_not_overwritten(tmp_path):
    """INSERT OR IGNORE means newer DB state always wins over older JSON state.

    A job_id already in the DB as 'rejected' must stay 'rejected' even though
    applications.json still calls it 'applied'.
    """
    apps = [_app("J001", status="applied"), _app("J002", status="applied")]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=[], reminders=[], rounds=[]):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()

        db = await _open_db(db_path)
        try:
            await db.execute(
                "INSERT INTO applications (job_id, title, company, status) "
                "VALUES (?, ?, ?, ?)",
                ("J001", "Backend Dev J001", "Acme", "rejected"),
            )
            await db.commit()
        finally:
            await db.close()

        result = await migrate_json_to_sqlite()

    assert result["applications"] == {"read": 2, "inserted": 1, "skipped": 1}

    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT job_id, status FROM applications ORDER BY job_id"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    assert [(r["job_id"], r["status"]) for r in rows] == [
        ("J001", "rejected"),
        ("J002", "applied"),
    ]


# ---------------------------------------------------------------------------
# Ledger (one-shot safety)
# ---------------------------------------------------------------------------


async def test_ledger_records_migration_after_default_run(tmp_path):
    """A default run records json_to_sqlite_v1 with the total rows inserted."""
    apps = [_app("J001"), _app("J002")]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=[], reminders=[], rounds=[]):
        from naukri_server.database import (
            init_db, migrate_json_to_sqlite, has_migration_run,
        )
        await init_db()
        assert await has_migration_run(MIGRATION_NAME) is False
        await migrate_json_to_sqlite()
        assert await has_migration_run(MIGRATION_NAME) is True

    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT name, applied_at, rows_inserted FROM migrations"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    assert len(rows) == 1
    assert rows[0]["name"] == MIGRATION_NAME
    assert rows[0]["rows_inserted"] == 2
    assert rows[0]["applied_at"]


async def test_second_default_run_skips_without_reading_json(tmp_path):
    """The ledger short-circuits before any JSON file is read.

    Proven by appending a brand-new record to applications.json between the
    two runs: if the second run had read the file, J003 would be in the DB.
    """
    apps = [_app("J001"), _app("J002")]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=[], reminders=[], rounds=[]) as files:
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        await migrate_json_to_sqlite()

        files["applications.json"].write_text(
            json.dumps(apps + [_app("J003")]), encoding="utf-8"
        )
        second = await migrate_json_to_sqlite()

    assert second["status"] == "skipped"
    assert second["reason"] == "already_applied"
    assert second["applications"] == {"read": 0, "inserted": 0, "skipped": 0}
    assert await _count(db_path, "applications") == 2

    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT 1 FROM applications WHERE job_id = ?", ("J003",)
        )
        assert await cursor.fetchone() is None
    finally:
        await db.close()


async def test_force_bypasses_the_ledger(tmp_path):
    """force=True runs even when the ledger already has the row."""
    apps = [_app("J001")]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=[], reminders=[], rounds=[]) as files:
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        await migrate_json_to_sqlite()
        files["applications.json"].write_text(
            json.dumps(apps + [_app("J002")]), encoding="utf-8"
        )
        forced = await migrate_json_to_sqlite(force=True)

    assert forced["status"] == "applied"
    assert forced["applications"] == {"read": 2, "inserted": 1, "skipped": 1}
    assert await _count(db_path, "applications") == 2


async def test_record_migration_is_idempotent(tmp_path):
    """record_migration twice leaves one ledger row (INSERT OR REPLACE)."""
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path):
        from naukri_server.database import (
            init_db, record_migration, has_migration_run,
        )
        await init_db()
        await record_migration("some_migration_v1", 5)
        await record_migration("some_migration_v1", 9)
        assert await has_migration_run("some_migration_v1") is True
        assert await has_migration_run("never_ran_v1") is False

    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT name, rows_inserted FROM migrations WHERE name = ?",
            ("some_migration_v1",),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    assert len(rows) == 1
    assert rows[0]["rows_inserted"] == 9  # last run wins, as documented


async def test_ledger_helpers_work_on_a_db_without_the_table(tmp_path):
    """A DB created before the ledger existed must not crash the helpers.

    Simulates an older init_db() by dropping the table after creation.
    """
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=[_app("J001")], saved=[],
                        reminders=[], rounds=[]):
        from naukri_server.database import (
            init_db, migrate_json_to_sqlite, has_migration_run,
        )
        await init_db()

        db = await _open_db(db_path)
        try:
            await db.execute("DROP TABLE migrations")
            await db.commit()
        finally:
            await db.close()

        assert await has_migration_run(MIGRATION_NAME) is False
        result = await migrate_json_to_sqlite()

    assert result["status"] == "applied"
    assert result["applications"]["inserted"] == 1


# ---------------------------------------------------------------------------
# extra column
# ---------------------------------------------------------------------------


async def test_extra_column_round_trips_unmapped_fields(tmp_path):
    """Fields the applications table has no column for land in `extra` as JSON."""
    apps = [
        _app(
            "J001",
            location="Bengaluru",
            apply_type="normal",
            fit_score=82,
            salary="20-30 Lacs PA",
            url="https://www.naukri.com/job-listings-x",
            pending_questions=["Notice period?"],
            message="applied via api",
            is_crawled=True,
            job_activity_date="2026-04-01",
            updated_at="2026-04-02T09:00:00",
        )
    ]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=[], reminders=[], rounds=[]):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        await migrate_json_to_sqlite()

    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT extra FROM applications WHERE job_id = ?", ("J001",)
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    extra = json.loads(row["extra"])  # must be valid JSON
    assert extra == {
        "apply_type": "normal",
        "fit_score": 82,
        "is_crawled": True,
        "job_activity_date": "2026-04-01",
        "location": "Bengaluru",
        "message": "applied via api",
        "pending_questions": ["Notice period?"],
        "salary": "20-30 Lacs PA",
        "updated_at": "2026-04-02T09:00:00",
        "url": "https://www.naukri.com/job-listings-x",
    }
    # Mapped columns must NOT be duplicated into extra
    for mapped in ("job_id", "title", "company", "status", "applied_at", "source"):
        assert mapped not in extra


async def test_extra_is_null_when_every_field_is_mapped(tmp_path):
    """A record with only mapped fields stores NULL, not an empty JSON object."""
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=[_app("J001")], saved=[],
                        reminders=[], rounds=[]):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        await migrate_json_to_sqlite()

    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT extra FROM applications WHERE job_id = ?", ("J001",)
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    assert row["extra"] is None


async def test_unmapped_key_set_is_derived_from_the_live_table(tmp_path):
    """A key matching a real column stays a column; a novel key goes to `extra`.

    `view_count` is a genuine applications column, so it must be stored there
    and must NOT appear in extra -- which is only true if the mapped-key set
    comes from PRAGMA table_info rather than a hardcoded literal.
    """
    apps = [_app("J001", view_count=7, some_future_field="xyz")]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=[], reminders=[], rounds=[]):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        await migrate_json_to_sqlite()

    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT view_count, extra FROM applications WHERE job_id = ?", ("J001",)
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    assert row["view_count"] == 7
    assert json.loads(row["extra"]) == {"some_future_field": "xyz"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_missing_json_files_are_a_noop(tmp_path):
    """No JSON file on disk -> no exception, zero counts, empty tables."""
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path) as files:
        for path in files.values():
            assert not path.exists()
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        result = await migrate_json_to_sqlite()

    assert result["status"] == "applied"
    for entity in ("applications", "saved_jobs", "reminders", "interview_rounds"):
        assert result[entity] == {"read": 0, "inserted": 0, "skipped": 0}
        assert await _count(db_path, entity) == 0


async def test_empty_json_files_are_a_noop(tmp_path):
    """Empty lists behave the same as missing files."""
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=[], saved=[], reminders=[], rounds=[]):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        result = await migrate_json_to_sqlite()

    assert result["status"] == "applied"
    assert result["applications"] == {"read": 0, "inserted": 0, "skipped": 0}
    assert await _count(db_path, "applications") == 0


async def test_company_rating_dict_is_stored_as_json_string(tmp_path):
    """A dict company_rating is serialised, a plain string passes through."""
    apps = [
        _app("J001", company_rating={"value": 4.1, "count": 320}),
        _app("J002", company_rating="4.5"),
    ]
    db_path = tmp_path / "naukri.db"

    with _migration_env(tmp_path, apps=apps, saved=[], reminders=[], rounds=[]):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        await migrate_json_to_sqlite()

    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT job_id, company_rating FROM applications ORDER BY job_id"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    assert isinstance(rows[0]["company_rating"], str)
    assert json.loads(rows[0]["company_rating"]) == {"value": 4.1, "count": 320}
    assert rows[1]["company_rating"] == "4.5"
    # company_rating is a real column, so it must not leak into extra
    cursor_extra = json.loads(
        (await _fetch_extra(db_path, "J001")) or "{}"
    )
    assert "company_rating" not in cursor_extra


async def _fetch_extra(db_path: Path, job_id: str):
    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT extra FROM applications WHERE job_id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return row["extra"] if row else None
    finally:
        await db.close()
