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
