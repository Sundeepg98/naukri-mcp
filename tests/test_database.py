"""Tests for naukri_server.database -- SQLite storage layer."""

import json
import aiosqlite
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers -- override DB_PATH to use tmp_path for isolation
# ---------------------------------------------------------------------------


async def _init_db_in(tmp: Path):
    """Run init_db with DB_PATH pointed at tmp directory."""
    db_path = tmp / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()
    return db_path


async def _open_db(db_path: Path) -> aiosqlite.Connection:
    """Open a raw connection to the test database."""
    db = aiosqlite.connect(str(db_path))
    await db
    db.row_factory = aiosqlite.Row
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_init_db_creates_tables(tmp_path):
    """init_db should create all 8 tables and 8 indexes."""
    db_path = await _init_db_in(tmp_path)

    db = await _open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]

        # Also verify indexes were created
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
        )
        indexes = [row[0] for row in await cursor.fetchall()]
    finally:
        await db.close()

    expected = [
        "agent_decisions",
        "agent_runs",
        "applications",
        "endpoint_audit",
        "event_log",
        "interview_rounds",
        "notifications",
        "reminders",
        "saved_jobs",
        "scheduled_runs",
        "screening_questions",
    ]
    assert tables == expected

    expected_indexes = [
        "idx_agent_runs_cycle",
        "idx_agent_runs_started",
        "idx_apps_applied_at",
        "idx_apps_status",
        "idx_decisions_cycle",
        "idx_decisions_job",
        "idx_endpoint_url",
        "idx_event_log_ts",
        "idx_event_log_type",
        "idx_notif_created",
        "idx_notif_read",
        "idx_rounds_job_id",
        "idx_sched_started",
        "idx_sched_task",
    ]
    assert indexes == expected_indexes


async def test_migrate_json_imports_applications(tmp_path):
    """migrate_json_to_sqlite should import applications from JSON file."""
    db_path = tmp_path / "naukri.db"

    # Create a fake applications.json
    apps_file = tmp_path / "applications.json"
    apps_data = [
        {
            "job_id": "J001",
            "title": "Backend Dev",
            "company": "Acme",
            "status": "applied",
            "applied_at": "2026-04-01T10:00:00",
            "source": "single",
        },
        {
            "job_id": "J002",
            "title": "Frontend Dev",
            "company": "Beta Inc",
            "status": "viewed",
            "applied_at": "2026-04-02T12:00:00",
            "source": "batch",
        },
    ]
    apps_file.write_text(json.dumps(apps_data), encoding="utf-8")

    # Create empty files for other entities
    for name in ("saved_jobs.json", "reminders.json", "interview_rounds.json"):
        (tmp_path / name).write_text("[]", encoding="utf-8")

    # init_db first, then migrate
    with patch("naukri_server.database.DB_PATH", db_path), \
         patch("naukri_server.database.DATA_DIR", tmp_path), \
         patch("naukri_server.config.APPLICATIONS_FILE", apps_file), \
         patch("naukri_server.config.SAVED_JOBS_FILE", tmp_path / "saved_jobs.json"), \
         patch("naukri_server.config.REMINDERS_FILE", tmp_path / "reminders.json"), \
         patch("naukri_server.config.INTERVIEW_ROUNDS_FILE", tmp_path / "interview_rounds.json"):
        from naukri_server.database import init_db, migrate_json_to_sqlite
        await init_db()
        await migrate_json_to_sqlite()

    # Verify data
    db = await _open_db(db_path)
    try:
        cursor = await db.execute("SELECT job_id, title, company, status FROM applications ORDER BY job_id")
        rows = await cursor.fetchall()
    finally:
        await db.close()

    assert len(rows) == 2
    assert rows[0]["job_id"] == "J001"
    assert rows[0]["title"] == "Backend Dev"
    assert rows[0]["company"] == "Acme"
    assert rows[0]["status"] == "applied"
    assert rows[1]["job_id"] == "J002"
    assert rows[1]["status"] == "viewed"


async def test_applications_table_schema(tmp_path):
    """applications table should have all expected columns."""
    db_path = await _init_db_in(tmp_path)

    db = await _open_db(db_path)
    try:
        cursor = await db.execute("PRAGMA table_info(applications)")
        columns = {row[1]: row[2] for row in await cursor.fetchall()}
    finally:
        await db.close()

    expected_columns = {
        "job_id": "TEXT",
        "title": "TEXT",
        "company": "TEXT",
        "status": "TEXT",
        "applied_at": "TEXT",
        "source": "TEXT",
        "ars_score": "INTEGER",
        "star_rating": "TEXT",
        "job_activity": "INTEGER",
        "company_rating": "TEXT",
        "is_open": "INTEGER",
        "view_count": "INTEGER",
        "follow_up_priority": "INTEGER",
        "last_synced": "TEXT",
        "extra": "TEXT",
    }
    assert columns == expected_columns


async def test_saved_jobs_table_crud(tmp_path):
    """saved_jobs table should support insert and query."""
    db_path = await _init_db_in(tmp_path)

    db = await _open_db(db_path)
    try:
        await db.execute(
            "INSERT INTO saved_jobs (job_id, title, company, notes, saved_at, source) VALUES (?, ?, ?, ?, ?, ?)",
            ("SJ001", "Full Stack Dev", "StartupCo", "Great role", "2026-04-03", "manual"),
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM saved_jobs WHERE job_id = ?", ("SJ001",))
        row = await cursor.fetchone()
    finally:
        await db.close()

    assert row is not None
    assert row["job_id"] == "SJ001"
    assert row["title"] == "Full Stack Dev"
    assert row["company"] == "StartupCo"
    assert row["notes"] == "Great role"
    assert row["source"] == "manual"


async def test_wal_mode_enabled(tmp_path):
    """Database should use WAL journal mode for better concurrency."""
    db_path = await _init_db_in(tmp_path)

    # Open via the production get_db function to verify WAL is set
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import get_db
        db = await get_db()
        try:
            cursor = await db.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            assert row[0] == "wal"
        finally:
            await db.close()
