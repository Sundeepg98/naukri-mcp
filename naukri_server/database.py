"""SQLite database -- replaces JSON file storage with proper transactions."""

import asyncio
import aiosqlite
import json
import logging
from pathlib import Path
from typing import Optional

from naukri_server.config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "naukri.db"
_db_lock = asyncio.Lock()

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    status TEXT DEFAULT 'applied',
    applied_at TEXT,
    source TEXT DEFAULT 'manual',
    ars_score INTEGER,
    star_rating TEXT,
    job_activity INTEGER,
    company_rating TEXT,
    is_open INTEGER,
    view_count INTEGER,
    follow_up_priority INTEGER DEFAULT 50,
    last_synced TEXT,
    extra TEXT  -- JSON blob for additional fields
);

CREATE TABLE IF NOT EXISTS saved_jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    notes TEXT,
    saved_at TEXT,
    source TEXT DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS reminders (
    job_id TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    remind_at TEXT,
    note TEXT,
    created_at TEXT,
    is_due INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS interview_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    round_type TEXT,
    date TEXT,
    notes TEXT,
    status TEXT DEFAULT 'scheduled',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS screening_questions (
    question_key TEXT PRIMARY KEY,
    question TEXT,
    answer TEXT,
    cached_at REAL
);

CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_applied_at ON applications(applied_at);
CREATE INDEX IF NOT EXISTS idx_rounds_job_id ON interview_rounds(job_id);
"""


async def get_db() -> aiosqlite.Connection:
    """Get database connection with WAL mode and foreign keys enabled.

    Caller is responsible for closing the connection (use ``async with``
    on the returned object or call ``await db.close()``).
    """
    db = aiosqlite.connect(str(DB_PATH))
    await db  # starts the background thread & opens the connection
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")  # Write-ahead logging for concurrency
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """Create tables if they don't exist."""
    db = await get_db()
    try:
        await db.executescript(_SCHEMA_SQL)
        await db.commit()
        logger.info("Database initialized at %s", DB_PATH)
    finally:
        await db.close()


async def list_applications(status=None, date_from=None, date_to=None, limit=50, offset=0):
    """Query applications with optional filters."""
    db = await get_db()
    try:
        query = "SELECT * FROM applications WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if date_from:
            query += " AND applied_at >= ?"
            params.append(date_from)
        if date_to:
            query += " AND applied_at <= ?"
            params.append(date_to)
        query += " ORDER BY applied_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        # Get total count (respecting same filters except pagination)
        count_query = "SELECT COUNT(*) FROM applications WHERE 1=1"
        count_params = []
        if status:
            count_query += " AND status = ?"
            count_params.append(status)
        if date_from:
            count_query += " AND applied_at >= ?"
            count_params.append(date_from)
        if date_to:
            count_query += " AND applied_at <= ?"
            count_params.append(date_to)
        count_cursor = await db.execute(count_query, count_params)
        total = (await count_cursor.fetchone())[0]

        return [dict(row) for row in rows], total
    finally:
        await db.close()


async def get_application(job_id: str):
    """Get a single application by job_id."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def upsert_application(app: dict):
    """Insert or update an application."""
    db = await get_db()
    try:
        await db.execute("""
            INSERT INTO applications (job_id, title, company, status, applied_at, source,
                                      ars_score, star_rating, job_activity, company_rating,
                                      is_open, view_count, follow_up_priority, last_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                title=excluded.title, company=excluded.company, status=excluded.status,
                ars_score=excluded.ars_score, star_rating=excluded.star_rating,
                job_activity=excluded.job_activity, company_rating=excluded.company_rating,
                is_open=excluded.is_open, view_count=excluded.view_count,
                follow_up_priority=excluded.follow_up_priority, last_synced=excluded.last_synced
        """, (
            app.get("job_id"), app.get("title"), app.get("company"),
            app.get("status", "applied"), app.get("applied_at"),
            app.get("source", "manual"), app.get("ars_score"),
            app.get("star_rating"), app.get("job_activity"),
            json.dumps(app.get("company_rating")) if isinstance(app.get("company_rating"), dict) else app.get("company_rating"),
            app.get("is_open"), app.get("view_count"),
            app.get("follow_up_priority", 50), app.get("last_synced"),
        ))
        await db.commit()
    finally:
        await db.close()


async def delete_applications_before(date: str) -> int:
    """Delete applications older than date. Returns count deleted."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM applications WHERE applied_at < ?", (date,))
        count = (await cursor.fetchone())[0]
        await db.execute("DELETE FROM applications WHERE applied_at < ?", (date,))
        await db.commit()
        return count
    finally:
        await db.close()


async def count_applications_by_status():
    """Get application counts grouped by status."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT status, COUNT(*) as cnt FROM applications GROUP BY status")
        rows = await cursor.fetchall()
        return {row["status"]: row["cnt"] for row in rows}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Saved jobs CRUD
# ---------------------------------------------------------------------------

async def list_saved_jobs(limit=50, offset=0):
    """Query saved jobs ordered by saved_at DESC."""
    db = await get_db()
    try:
        count_cursor = await db.execute("SELECT COUNT(*) FROM saved_jobs")
        total = (await count_cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT * FROM saved_jobs ORDER BY saved_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows], total
    finally:
        await db.close()


async def get_saved_job(job_id: str):
    """Get a single saved job by job_id."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM saved_jobs WHERE job_id = ?", (job_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def upsert_saved_job(sj: dict):
    """Insert or update a saved job."""
    db = await get_db()
    try:
        await db.execute("""
            INSERT INTO saved_jobs (job_id, title, company, notes, saved_at, source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                title=excluded.title, company=excluded.company,
                notes=excluded.notes, saved_at=excluded.saved_at, source=excluded.source
        """, (
            sj.get("job_id"), sj.get("title"), sj.get("company"),
            sj.get("notes"), sj.get("saved_at"), sj.get("source", "manual"),
        ))
        await db.commit()
    finally:
        await db.close()


async def delete_saved_job(job_id: str) -> bool:
    """Delete a saved job. Returns True if deleted, False if not found."""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM saved_jobs WHERE job_id = ?", (job_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def count_saved_jobs() -> int:
    """Count total saved jobs."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM saved_jobs")
        return (await cursor.fetchone())[0]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Reminders CRUD
# ---------------------------------------------------------------------------

async def list_reminders():
    """Get all reminders."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM reminders ORDER BY remind_at ASC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def get_reminder(job_id: str):
    """Get a single reminder by job_id."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM reminders WHERE job_id = ?", (job_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def upsert_reminder(rem: dict):
    """Insert or update a reminder."""
    db = await get_db()
    try:
        await db.execute("""
            INSERT INTO reminders (job_id, title, company, remind_at, note, created_at, is_due)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                title=excluded.title, company=excluded.company,
                remind_at=excluded.remind_at, note=excluded.note, is_due=excluded.is_due
        """, (
            rem.get("job_id"), rem.get("title"), rem.get("company"),
            rem.get("remind_at"), rem.get("note"), rem.get("created_at"),
            rem.get("is_due", 0),
        ))
        await db.commit()
    finally:
        await db.close()


async def delete_reminder(job_id: str) -> bool:
    """Delete a reminder. Returns True if deleted, False if not found."""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM reminders WHERE job_id = ?", (job_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Interview rounds CRUD
# ---------------------------------------------------------------------------

async def add_interview_round(rd: dict) -> int:
    """Insert an interview round. Returns the new row id."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            INSERT INTO interview_rounds (job_id, round_type, date, notes, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            rd.get("job_id"), rd.get("round_type"), rd.get("date"),
            rd.get("notes"), rd.get("status", "scheduled"), rd.get("created_at"),
        ))
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def list_interview_rounds(job_id: str = None):
    """List interview rounds, optionally filtered by job_id."""
    db = await get_db()
    try:
        if job_id:
            cursor = await db.execute(
                "SELECT * FROM interview_rounds WHERE job_id = ? ORDER BY date ASC",
                (job_id,),
            )
        else:
            cursor = await db.execute("SELECT * FROM interview_rounds ORDER BY date ASC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Stale applications query
# ---------------------------------------------------------------------------

async def get_stale_applications_raw(days_threshold: int = 14):
    """Get applications older than days_threshold with their computed days_since_applied.

    Returns raw rows — caller does stale scoring logic.
    """
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT *, julianday('now') - julianday(applied_at) as days_since_applied
            FROM applications
            WHERE applied_at IS NOT NULL
              AND julianday('now') - julianday(applied_at) >= ?
            ORDER BY applied_at ASC
        """, (days_threshold,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Recruiter history (GROUP BY company)
# ---------------------------------------------------------------------------

async def get_recruiter_history():
    """Aggregate applications by company with min/max applied_at and response detection."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT
                company,
                COUNT(*) as applications,
                MIN(applied_at) as first_applied,
                MAX(applied_at) as last_applied,
                GROUP_CONCAT(status) as statuses
            FROM applications
            WHERE company IS NOT NULL
            GROUP BY company
            ORDER BY COUNT(*) DESC
            LIMIT 20
        """)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def migrate_json_to_sqlite():
    """One-time migration: import existing JSON files into SQLite."""
    from naukri_server.utils import load_json_with_backup
    from naukri_server.config import (
        APPLICATIONS_FILE,
        SAVED_JOBS_FILE,
        REMINDERS_FILE,
        INTERVIEW_ROUNDS_FILE,
    )

    db = await get_db()
    try:
        # Migrate applications
        apps = load_json_with_backup(APPLICATIONS_FILE, logger)
        for app in apps:
            await db.execute(
                "INSERT OR IGNORE INTO applications (job_id, title, company, status, applied_at, source, ars_score, star_rating, job_activity, company_rating, is_open, view_count, follow_up_priority, last_synced) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    app.get("job_id"),
                    app.get("title"),
                    app.get("company"),
                    app.get("status", "applied"),
                    app.get("applied_at"),
                    app.get("source", "manual"),
                    app.get("ars_score"),
                    app.get("star_rating"),
                    app.get("job_activity"),
                    json.dumps(app.get("company_rating"))
                    if isinstance(app.get("company_rating"), dict)
                    else app.get("company_rating"),
                    app.get("is_open"),
                    app.get("view_count"),
                    app.get("follow_up_priority", 50),
                    app.get("last_synced"),
                ),
            )

        # Migrate saved jobs
        saved = load_json_with_backup(SAVED_JOBS_FILE, logger)
        for sj in saved:
            await db.execute(
                "INSERT OR IGNORE INTO saved_jobs (job_id, title, company, notes, saved_at, source) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sj.get("job_id"),
                    sj.get("title"),
                    sj.get("company"),
                    sj.get("notes"),
                    sj.get("saved_at"),
                    sj.get("source", "manual"),
                ),
            )

        # Migrate reminders
        reminders = load_json_with_backup(REMINDERS_FILE, logger)
        for r in reminders:
            await db.execute(
                "INSERT OR IGNORE INTO reminders (job_id, title, company, remind_at, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    r.get("job_id"),
                    r.get("title"),
                    r.get("company"),
                    r.get("remind_at"),
                    r.get("note"),
                    r.get("created_at"),
                ),
            )

        # Migrate interview rounds
        rounds = load_json_with_backup(INTERVIEW_ROUNDS_FILE, logger)
        for rd in rounds:
            await db.execute(
                "INSERT OR IGNORE INTO interview_rounds (job_id, round_type, date, notes, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rd.get("job_id"),
                    rd.get("round_type"),
                    rd.get("date"),
                    rd.get("notes"),
                    rd.get("status"),
                    rd.get("created_at"),
                ),
            )

        await db.commit()
        logger.info(
            "Migrated %d apps, %d saved, %d reminders, %d rounds to SQLite",
            len(apps),
            len(saved),
            len(reminders),
            len(rounds),
        )
    finally:
        await db.close()
