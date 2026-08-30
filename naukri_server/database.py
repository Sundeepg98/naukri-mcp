"""SQLite database -- replaces JSON file storage with proper transactions."""

import asyncio
import aiosqlite
import json
import logging
import re
from pathlib import Path
from typing import Optional

from naukri_server.config import DATA_DIR, STALE_THRESHOLD_DAYS

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "naukri.db"
_db_lock = asyncio.Lock()

# Guards the metadata key that has_pending_notification splices into a JSON
# path. Asserted at import rather than trusted: a backslash class silently
# mangled by a shell heredoc has already cost this repo a debugging round, and
# a regex that quietly matches everything would turn the guard into a decoy
# without failing a single test.
_JSON_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
assert _JSON_KEY_RE.match("constant_name") and _JSON_KEY_RE.match("job_id")
assert not _JSON_KEY_RE.match("a.b") and not _JSON_KEY_RE.match("$.job_id")

# Name of the one-shot JSON -> SQLite data migration as recorded in the
# `migrations` ledger table. Bump the suffix only if the import ever has to
# be re-run against every database as a genuinely new migration.
JSON_MIGRATION_NAME = "json_to_sqlite_v1"

# Adds reminders.notified_at to a database created before that column existed.
# Same ledger, same naming convention as above.
REMINDERS_NOTIFIED_AT_MIGRATION = "reminders_notified_at_v1"

# Migration ledger. Kept as its own constant (and appended to _SCHEMA_SQL
# below) so has_migration_run() / record_migration() can create it
# defensively on a database that was initialised before this table existed.
_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    rows_inserted INTEGER DEFAULT 0
);
"""

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
    is_due INTEGER DEFAULT 0,
    -- NULL until a ReminderDue has actually been emitted for this row, and
    -- set back to NULL by set_reminder. It is what makes the hourly
    -- reminder_check notify ONCE per due-transition instead of on every tick.
    -- `is_due` above is dead by contrast: 0 in all 50 of his rows, written by
    -- nothing, computed live from remind_at at read time instead.
    notified_at TEXT
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

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    title TEXT,
    body TEXT,
    priority TEXT DEFAULT 'medium',
    created_at TEXT NOT NULL,
    read_at TEXT,
    delivered_via TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_applied_at ON applications(applied_at);
CREATE INDEX IF NOT EXISTS idx_rounds_job_id ON interview_rounds(job_id);
CREATE INDEX IF NOT EXISTS idx_notif_read ON notifications(read_at);
CREATE INDEX IF NOT EXISTS idx_notif_created ON notifications(created_at);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    data TEXT,
    subscriber_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_event_log_type ON event_log(event_type);
CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(timestamp);

CREATE TABLE IF NOT EXISTS endpoint_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    method TEXT DEFAULT 'GET',
    status TEXT DEFAULT 'NEW',
    first_discovered TEXT,
    last_seen TEXT,
    page_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_endpoint_url ON endpoint_audit(url);

CREATE TABLE IF NOT EXISTS scheduled_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT DEFAULT 'running',  -- running, completed, failed, skipped
    result TEXT,  -- JSON blob with task output summary
    error TEXT,
    duration_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_sched_task ON scheduled_runs(task_name);
CREATE INDEX IF NOT EXISTS idx_sched_started ON scheduled_runs(started_at);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT UNIQUE NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    mode TEXT,
    searches_run INTEGER DEFAULT 0,
    jobs_found INTEGER DEFAULT 0,
    jobs_matched INTEGER DEFAULT 0,
    applied_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,
    duration_ms REAL,
    error TEXT,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_cycle ON agent_runs(cycle_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at);

CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    company TEXT,
    title TEXT,
    search_query TEXT,
    fit_score INTEGER,
    decision TEXT NOT NULL,
    skip_reason TEXT,
    apply_status TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(cycle_id, job_id) ON CONFLICT REPLACE
);
CREATE INDEX IF NOT EXISTS idx_decisions_cycle ON agent_decisions(cycle_id);
CREATE INDEX IF NOT EXISTS idx_decisions_job ON agent_decisions(job_id);

-- T2 healing: every auto-fix on a snapshot+revert-protected endpoint inserts
-- a row here. The t2_verify_pending scheduler task re-validates the endpoint
-- after `verify_at` and either confirms (status="confirmed") or reverts the
-- commit (status="reverted") and rolls the row's status forward.
CREATE TABLE IF NOT EXISTS auto_fix_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha TEXT NOT NULL,
    pre_fix_sha TEXT NOT NULL,
    constant_name TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    verify_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|reverted|revert_failed
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_auto_fix_pending_status ON auto_fix_pending(status);
CREATE INDEX IF NOT EXISTS idx_auto_fix_pending_verify ON auto_fix_pending(verify_at);
""" + _MIGRATIONS_TABLE_SQL


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
    await db.execute("PRAGMA busy_timeout=5000")  # 5 second wait on lock
    return db


async def init_db():
    """Create tables if they don't exist, then apply pending DDL migrations.

    CREATE TABLE IF NOT EXISTS cannot add a column to a table that already
    exists, so every schema change after the first release needs both halves:
    the column in _SCHEMA_SQL for a fresh database, and an ALTER for an
    existing one. init_db is the only place both are guaranteed to run before
    any tool touches the table.
    """
    db = await get_db()
    try:
        await db.executescript(_SCHEMA_SQL)
        await db.commit()
        logger.info("Database initialized at %s", DB_PATH)
    finally:
        await db.close()

    await migrate_reminders_notified_at()


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


# Columns written explicitly by upsert_application. Anything else on the dict
# goes to the `extra` JSON blob.
_APPLICATION_COLUMNS = frozenset((
    "job_id", "title", "company", "status", "applied_at", "source",
    "ars_score", "star_rating", "job_activity", "company_rating",
    "is_open", "view_count", "follow_up_priority", "last_synced", "extra",
))


def _application_extra(app: dict) -> Optional[str]:
    """JSON for the `extra` column: every field with no column of its own.

    The `extra` column has existed since the first schema and upsert_application
    never wrote it, so every non-column field the sync parser produced was
    silently discarded at persist time. The costly one was `applied_date` --
    Naukri's REAL apply date, parsed out of the history API on every sync and
    thrown away every time, which is why retention had only the insert
    timestamp to work with. See real_applied_date.
    """
    prior = app.get("extra")
    if isinstance(prior, str):
        try:
            prior = json.loads(prior)
        except Exception:
            prior = None
    merged = dict(prior) if isinstance(prior, dict) else {}
    merged.update({k: v for k, v in app.items() if k not in _APPLICATION_COLUMNS})
    return json.dumps(merged) if merged else None


_UPSERT_APPLICATION_SQL = """
            INSERT INTO applications (job_id, title, company, status, applied_at, source,
                                      ars_score, star_rating, job_activity, company_rating,
                                      is_open, view_count, follow_up_priority, last_synced,
                                      extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                title=excluded.title, company=excluded.company, status=excluded.status,
                ars_score=excluded.ars_score, star_rating=excluded.star_rating,
                job_activity=excluded.job_activity, company_rating=excluded.company_rating,
                is_open=excluded.is_open, view_count=excluded.view_count,
                follow_up_priority=excluded.follow_up_priority, last_synced=excluded.last_synced,
                -- Never blank an existing blob with a NULL: a caller that passes
                -- only the column fields must not erase a real applied_date we
                -- already captured on an earlier sync.
                extra=COALESCE(excluded.extra, applications.extra)
"""


def _application_row(app: dict) -> tuple:
    """Bind one application dict to the upsert parameter tuple."""
    rating = app.get("company_rating")
    return (
        app.get("job_id"), app.get("title"), app.get("company"),
        app.get("status", "applied"), app.get("applied_at"),
        app.get("source", "manual"), app.get("ars_score"),
        app.get("star_rating"), app.get("job_activity"),
        json.dumps(rating) if isinstance(rating, dict) else rating,
        app.get("is_open"), app.get("view_count"),
        app.get("follow_up_priority", 50), app.get("last_synced"),
        _application_extra(app),
    )


async def upsert_applications(apps: list) -> int:
    """Upsert many applications over ONE connection. Returns the row count.

    ``upsert_application`` opens a fresh aiosqlite connection per call --
    connect, PRAGMA x3, write, commit, close. A sync persists every application
    it knows about, so on his 162 rows that was 162 connections against a file
    the scheduler, the watchdog and any live tool are also using. It timed out a
    real sync at the `persist` saga step after 30s on 2026-08-22.

    Measured, and worth stating precisely: the per-row cost is NOT the `extra`
    column added by the retention fix (162 rows benchmarked at 1.79s WITH extra
    vs 1.98s without -- free). It is the connection churn. One connection and
    one executemany removes it.
    """
    if not apps:
        return 0
    db = await get_db()
    try:
        await db.executemany(_UPSERT_APPLICATION_SQL,
                             [_application_row(a) for a in apps])
        await db.commit()
        return len(apps)
    finally:
        await db.close()


async def upsert_application(app: dict):
    """Insert or update an application."""
    db = await get_db()
    try:
        await db.execute("""
            INSERT INTO applications (job_id, title, company, status, applied_at, source,
                                      ars_score, star_rating, job_activity, company_rating,
                                      is_open, view_count, follow_up_priority, last_synced,
                                      extra)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                title=excluded.title, company=excluded.company, status=excluded.status,
                ars_score=excluded.ars_score, star_rating=excluded.star_rating,
                job_activity=excluded.job_activity, company_rating=excluded.company_rating,
                is_open=excluded.is_open, view_count=excluded.view_count,
                follow_up_priority=excluded.follow_up_priority, last_synced=excluded.last_synced,
                -- Never blank an existing blob with a NULL: a caller that passes
                -- only the column fields must not erase a real applied_date we
                -- already captured on an earlier sync.
                extra=COALESCE(excluded.extra, applications.extra)
        """, (
            app.get("job_id"), app.get("title"), app.get("company"),
            app.get("status", "applied"), app.get("applied_at"),
            app.get("source", "manual"), app.get("ars_score"),
            app.get("star_rating"), app.get("job_activity"),
            json.dumps(app.get("company_rating")) if isinstance(app.get("company_rating"), dict) else app.get("company_rating"),
            app.get("is_open"), app.get("view_count"),
            app.get("follow_up_priority", 50), app.get("last_synced"),
            _application_extra(app),
        ))
        await db.commit()
    finally:
        await db.close()


_ARCHIVE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS applications_archive (
    job_id TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    status TEXT,
    applied_at TEXT,
    source TEXT,
    ars_score INTEGER,
    star_rating TEXT,
    job_activity INTEGER,
    company_rating TEXT,
    is_open INTEGER,
    view_count INTEGER,
    follow_up_priority INTEGER,
    last_synced TEXT,
    extra TEXT,
    archived_at TEXT NOT NULL,
    archived_reason TEXT
);
"""

_ARCHIVE_COLUMNS = (
    "job_id", "title", "company", "status", "applied_at", "source",
    "ars_score", "star_rating", "job_activity", "company_rating",
    "is_open", "view_count", "follow_up_priority", "last_synced", "extra",
)


def real_applied_date(row: dict) -> Optional[str]:
    """The date he ACTUALLY applied, as a comparable ISO string, or None.

    ``applications.applied_at`` is NOT that date. ``record_application`` sets it
    to ``datetime.now()`` at INSERT time, so it is an insert timestamp for every
    row -- which is why all 162 rows on the live account carried one of seven
    batch timestamps rather than a spread of real apply dates.

    The real date is Naukri's ``appliedDate``, which the sync parser extracts as
    ``applied_date`` and which now survives into the ``extra`` blob.

    Returns None when no trustworthy date exists or it cannot be parsed. Callers
    MUST treat None as "do not delete": a row whose date provenance is unknown
    has no evidence of age, and guessing costs him history he cannot get back.
    """
    extra = row.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except Exception:
            extra = None
    if not isinstance(extra, dict):
        return None

    raw = extra.get("applied_date")
    if raw is None or raw == "":
        return None

    # Epoch (Naukri's history API sends millis on some routes)
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        try:
            n = float(raw)
        except Exception:
            return None
        if n > 1e11:          # millis
            n /= 1000.0
        if not (0 < n < 4e9):  # outside ~1970..2096 is not a date
            return None
        try:
            from datetime import datetime as _dt, timezone as _tz
            return _dt.fromtimestamp(n, _tz.utc).isoformat()
        except Exception:
            return None

    if not isinstance(raw, str):
        return None
    text = raw.strip()
    from datetime import datetime as _dt, timezone as _tz
    try:
        parsed = _dt.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_tz.utc)
        return parsed.isoformat()
    except Exception:
        pass
    for fmt in ("%d %b %Y", "%d %B %Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d",
                "%b %d, %Y", "%d %b, %Y"):
        try:
            return _dt.strptime(text, fmt).replace(tzinfo=_tz.utc).isoformat()
        except Exception:
            continue
    return None


async def list_purgeable_applications(date: str) -> list:
    """Rows a purge to `date` would actually remove -- the ONE predicate.

    Preview and deletion must never disagree: a dry run that reports N and then
    removes a different N is its own defect. ``purge_applications`` uses this
    for the preview and ``delete_applications_before`` uses it to choose rows.
    """
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM applications")
        rows = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()
    out = []
    for r in rows:
        real = real_applied_date(r)
        if real is not None and real < date:
            out.append(r)
    return out


async def delete_applications_before(date: str, reason: str = "retention") -> int:
    """Archive, then delete, applications whose REAL apply date precedes `date`.

    Two guarantees this function did not previously make:

    * It compares ``real_applied_date(row)``, not ``applied_at``. A row with no
      trustworthy apply date is NEVER deleted -- see real_applied_date. The old
      version compared ``applied_at``, an insert timestamp, so it was set to
      delete 149 of his 162 applications between 2026-08-27 and 2026-09-08 on
      dates that recorded when the rows were IMPORTED.
    * Every row is copied into ``applications_archive`` inside the same
      transaction before it is removed, so a purge is recoverable. Soft-delete
      was rejected as the mechanism: it would require ``WHERE deleted_at IS
      NULL`` on every read path in the codebase, and a missed one silently
      resurrects deleted rows.

    Returns the number of rows archived and deleted.
    """
    doomed = await list_purgeable_applications(date)
    if not doomed:
        return 0

    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).isoformat()
    placeholders = ", ".join("?" for _ in _ARCHIVE_COLUMNS)
    insert_sql = (
        "INSERT OR REPLACE INTO applications_archive (%s, archived_at, archived_reason) "
        "VALUES (%s, ?, ?)" % (", ".join(_ARCHIVE_COLUMNS), placeholders)
    )

    db = await get_db()
    try:
        await db.executescript(_ARCHIVE_TABLE_SQL)
        for row in doomed:
            await db.execute(
                insert_sql,
                tuple(row.get(c) for c in _ARCHIVE_COLUMNS) + (now, reason),
            )
        await db.executemany(
            "DELETE FROM applications WHERE job_id = ?",
            [(row.get("job_id"),) for row in doomed],
        )
        await db.commit()
        logger.info("Archived and deleted %d applications older than %s (%s)",
                    len(doomed), date, reason)
        return len(doomed)
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


async def get_applied_job_ids() -> set:
    """Return set of all job_id strings in applications table. Efficient for duplicate checking."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT job_id FROM applications")
        rows = await cursor.fetchall()
        return {row["job_id"] for row in rows}
    finally:
        await db.close()


async def list_all_applications():
    """Return all applications (no pagination). For analytics/export."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM applications ORDER BY applied_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def list_all_saved_jobs():
    """Return all saved jobs (no pagination). For export."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM saved_jobs ORDER BY saved_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
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
            INSERT INTO reminders (job_id, title, company, remind_at, note, created_at, is_due, notified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                title=excluded.title, company=excluded.company,
                remind_at=excluded.remind_at, note=excluded.note,
                is_due=excluded.is_due, notified_at=excluded.notified_at
        """, (
            rem.get("job_id"), rem.get("title"), rem.get("company"),
            rem.get("remind_at"), rem.get("note"), rem.get("created_at"),
            rem.get("is_due", 0), rem.get("notified_at"),
        ))
        await db.commit()
    finally:
        await db.close()


async def mark_reminder_notified(job_id: str, when: str) -> bool:
    """Stamp notified_at on one reminder. Returns True if a row was stamped.

    A targeted UPDATE rather than an upsert: the caller (list_reminders) holds
    a row it read, and re-upserting that row would write back every other
    column too, silently clobbering a concurrent set_reminder.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE reminders SET notified_at = ? WHERE job_id = ?",
            (when, job_id),
        )
        await db.commit()
        return cursor.rowcount > 0
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


async def list_interview_rounds(job_id: Optional[str] = None):
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
# Notifications CRUD
# ---------------------------------------------------------------------------

async def store_notification(notif: dict) -> int:
    """Store a notification. Returns the new row id."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            INSERT INTO notifications (event_type, title, body, priority, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            notif.get("event_type"), notif.get("title"), notif.get("body"),
            notif.get("priority", "medium"), notif.get("created_at"),
            json.dumps(notif.get("metadata")) if notif.get("metadata") else None,
        ))
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def list_undelivered_notifications(limit: int = 20) -> list:
    """Get undelivered notifications ordered by priority then date."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM notifications
            WHERE delivered_via IS NULL
            ORDER BY
                CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                created_at DESC
            LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def mark_notifications_delivered(ids: list, via: str = "brief") -> int:
    """Mark notifications as delivered. Returns count updated."""
    if not ids:
        return 0
    db = await get_db()
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" * len(ids))
        cursor = await db.execute(
            f"UPDATE notifications SET delivered_via = ?, read_at = ? WHERE id IN ({placeholders})",
            [via, now] + list(ids),
        )
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def has_pending_notification(
    event_type: str, key_value: str, metadata_key: str = "job_id",
) -> bool:
    """Is there already an undelivered notification of this type for this key?

    The dedupe predicate for repeating events. `delivered_via IS NULL` is the
    honest reading of "he has not been shown this yet": the daily brief is what
    sets it (mark_notifications_delivered, which stamps read_at too), so a
    delivered row means the notification did its job and the next due check may
    mint a fresh one.

    json_extract, not `metadata LIKE '%key%'`: metadata is a JSON TEXT column,
    and both key spaces nest by construction - Naukri job_ids are numeric
    strings that routinely share prefixes, and config constant names do too
    (DASHBOARD_API is a strict prefix of a plausible DASHBOARD_API_V2). A LIKE
    would suppress a real notification for a different job or endpoint.

    `metadata_key` selects WHICH identity in the metadata blob is the dedupe
    key. It defaults to job_id, which is what ReminderDue and ApplicationStale
    use. EndpointDrift is not about a job at all - it is one fact per drifted
    config constant - so the healing router passes "constant_name".
    """
    if not key_value:
        return False
    if not _JSON_KEY_RE.match(metadata_key):
        # The JSON path is built from this argument, so a non-identifier is a
        # programming error. Raise rather than match nothing forever: every
        # caller wraps this in a fail-open try/except that logs and notifies
        # anyway, so a loud failure degrades to noise, never to silence.
        raise ValueError(
            "metadata_key must be a plain identifier, got %r" % (metadata_key,))
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            SELECT 1 FROM notifications
            WHERE event_type = ?
              AND delivered_via IS NULL
              AND json_extract(metadata, ?) = ?
            LIMIT 1
            """,
            (event_type, "$." + metadata_key, key_value),
        )
        return await cursor.fetchone() is not None
    finally:
        await db.close()


async def count_undelivered_notifications() -> int:
    """Count undelivered notifications."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM notifications WHERE delivered_via IS NULL")
        return (await cursor.fetchone())[0]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Event log persistence
# ---------------------------------------------------------------------------


async def log_event(event_type: str, timestamp: str, data: Optional[str] = None, subscriber_count: int = 0) -> int:
    """Persist a domain event to the event_log table. Returns the new row id."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO event_log (event_type, timestamp, data, subscriber_count) VALUES (?, ?, ?, ?)",
            (event_type, timestamp, data, subscriber_count),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def last_event_value(event_type: str, key: str) -> Optional[str]:
    """The `key` field of the most recently logged event of `event_type`, else None.

    Used by change-detecting emitters: an event named `...Changed` must not fire
    when nothing changed, and the only durable record of what it last reported
    is the row EventBus.emit wrote to event_log. Values come back as STRINGS -
    emit() serialises with ``{k: str(v) for ...}`` - so the caller parses.

    Ordered by `id`, not `timestamp`: id is the autoincrement insert order and
    is immune to clock skew and to two events sharing a timestamp string.

    Returns None when the type has never been logged, when the row has no such
    key, or when `data` will not parse. All three mean "no prior value" and the
    caller should treat them alike. Note that `cleanup_old_records` prunes
    event_log at the retention horizon, so a long-idle detector re-reports once
    after the prune - bounded, and self-correcting on the next call.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT data FROM event_log WHERE event_type = ? ORDER BY id DESC LIMIT 1",
            (event_type,),
        )
        row = await cursor.fetchone()
        if not row or not row["data"]:
            return None
        try:
            payload = json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get(key)
        return None if value is None else str(value)
    finally:
        await db.close()


async def get_event_stats(hours: int = 24) -> dict:
    """Get event counts by type for the last N hours."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT event_type, COUNT(*) as count
            FROM event_log
            WHERE timestamp >= datetime('now', ?)
            GROUP BY event_type
            ORDER BY count DESC
        """, (f"-{hours} hours",))
        rows = await cursor.fetchall()
        return {row["event_type"]: row["count"] for row in rows}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Stale applications query
# ---------------------------------------------------------------------------

async def get_stale_applications_raw(days_threshold: int = STALE_THRESHOLD_DAYS):
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
    """Aggregate applications by company with min/max applied_at and response detection.

    Returns EVERY company, not a top-N slice. The caller decides how many rows to
    show; only the caller can also report an honest total. This used to end in
    ``LIMIT 20``, and ``recruiter_history`` then published ``len(rows)`` as
    ``total_companies`` -- so an account with 115 distinct companies reported 20,
    and responsive/unresponsive counts silently described the top 20 rather than
    the portfolio (measured on the live account, 2026-08-22).
    """
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
        """)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def has_migration_run(name: str) -> bool:
    """Return True if the named one-shot migration is recorded in the ledger."""
    db = await get_db()
    try:
        # Defensive: the ledger may not exist yet on a database created by
        # an older init_db(). The DDL is CREATE TABLE IF NOT EXISTS.
        await db.executescript(_MIGRATIONS_TABLE_SQL)
        cursor = await db.execute(
            "SELECT 1 FROM migrations WHERE name = ?", (name,)
        )
        return await cursor.fetchone() is not None
    finally:
        await db.close()


async def record_migration(name: str, rows_inserted: int = 0) -> None:
    """Record that a one-shot migration ran.

    INSERT OR REPLACE, so `applied_at` / `rows_inserted` always describe the
    MOST RECENT run of that migration -- a forced re-run overwrites the row.
    """
    from datetime import datetime, timezone

    db = await get_db()
    try:
        await db.executescript(_MIGRATIONS_TABLE_SQL)
        await db.execute(
            "INSERT OR REPLACE INTO migrations (name, applied_at, rows_inserted) "
            "VALUES (?, ?, ?)",
            (name, datetime.now(timezone.utc).isoformat(), rows_inserted),
        )
        await db.commit()
    finally:
        await db.close()


async def migrate_reminders_notified_at(force: bool = False) -> dict:
    """Add `reminders.notified_at` to a database that predates the column.

    Two independent guards, because either one alone has a hole:

      * the `migrations` ledger short-circuits the whole function, but it is
        only armed once an ALTER has actually run somewhere;
      * PRAGMA table_info is checked regardless, because a database can have
        the column without a ledger row -- every FRESH database does, since
        _SCHEMA_SQL now declares it. Without this check the ALTER would raise
        "duplicate column name: notified_at" and take init_db down with it on
        every server start.

    Returns ``{"status": "applied"|"skipped", "column_added": bool}``.
    """
    if not force and await has_migration_run(REMINDERS_NOTIFIED_AT_MIGRATION):
        return {"status": "skipped", "column_added": False}

    db = await get_db()
    try:
        cursor = await db.execute("PRAGMA table_info(reminders)")
        columns = {row["name"] for row in await cursor.fetchall()}
        column_added = "notified_at" not in columns
        if column_added:
            await db.execute("ALTER TABLE reminders ADD COLUMN notified_at TEXT")
            await db.commit()
            logger.info("Migration %s: added reminders.notified_at",
                        REMINDERS_NOTIFIED_AT_MIGRATION)
    finally:
        await db.close()

    if column_added:
        # Recorded ONLY when the ALTER actually fired. A FRESH database gets
        # the column from _SCHEMA_SQL and has nothing to record; a ledger row
        # there would claim this migration changed something it did not.
        # MEASURED, not assumed: recording unconditionally put a second row in
        # the ledger on every fresh database and turned
        # test_json_migration.test_ledger_records_migration_after_default_run
        # red ("assert 2 == 1") -- that test reads the WHOLE migrations table.
        await record_migration(REMINDERS_NOTIFIED_AT_MIGRATION, 1)

    return {"status": "applied", "column_added": column_added}


def _empty_counts() -> dict:
    """Zeroed per-entity counter block."""
    return {"read": 0, "inserted": 0, "skipped": 0}


async def migrate_json_to_sqlite(force: bool = False) -> dict:
    """One-time migration: import existing JSON files into SQLite.

    Guarded by the `migrations` ledger so that a server restart cannot
    silently resurrect rows the operator deliberately deleted. Pass
    ``force=True`` to run regardless of the ledger; the inserts remain
    INSERT-OR-IGNORE keyed on job_id, so a forced re-run never duplicates a
    row and never overwrites newer DB state with older JSON state.

    Returns::

        {"status": "applied" | "skipped",
         "applications":     {"read": N, "inserted": N, "skipped": N},
         "saved_jobs":       {...},
         "reminders":        {...},
         "interview_rounds": {...}}

    where `inserted` is the summed cursor.rowcount of the INSERT statements
    (1 when the row landed, 0 when it was ignored) and `skipped` is
    `read` - `inserted`. A missing JSON file is a no-op, not an error --
    load_json_with_backup() returns [] for a path that does not exist.
    """
    from naukri_server.utils import load_json_with_backup
    from naukri_server.config import (
        APPLICATIONS_FILE,
        SAVED_JOBS_FILE,
        REMINDERS_FILE,
        INTERVIEW_ROUNDS_FILE,
    )

    if not force and await has_migration_run(JSON_MIGRATION_NAME):
        logger.info(
            "Migration %s already recorded -- skipping JSON import",
            JSON_MIGRATION_NAME,
        )
        return {
            "status": "skipped",
            "reason": "already_applied",
            "applications": _empty_counts(),
            "saved_jobs": _empty_counts(),
            "reminders": _empty_counts(),
            "interview_rounds": _empty_counts(),
        }

    counts = {
        "applications": _empty_counts(),
        "saved_jobs": _empty_counts(),
        "reminders": _empty_counts(),
        "interview_rounds": _empty_counts(),
    }

    db = await get_db()
    try:
        # Derive the mapped column set from the live table rather than from a
        # hardcoded literal, so any future column automatically stops being
        # routed into `extra`.
        cursor = await db.execute("PRAGMA table_info(applications)")
        app_columns = {row[1] for row in await cursor.fetchall()}

        # Migrate applications
        apps = load_json_with_backup(APPLICATIONS_FILE, logger)
        counts["applications"]["read"] = len(apps)
        for app in apps:
            # Everything the applications table has no column for goes into
            # the `extra` JSON blob instead of being silently dropped.
            extra = {k: v for k, v in app.items() if k not in app_columns}
            cursor = await db.execute(
                "INSERT OR IGNORE INTO applications (job_id, title, company, status, applied_at, source, ars_score, star_rating, job_activity, company_rating, is_open, view_count, follow_up_priority, last_synced, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    json.dumps(extra, ensure_ascii=False, sort_keys=True, default=str)
                    if extra
                    else None,
                ),
            )
            counts["applications"]["inserted"] += max(cursor.rowcount, 0)

        # Migrate saved jobs
        saved = load_json_with_backup(SAVED_JOBS_FILE, logger)
        counts["saved_jobs"]["read"] = len(saved)
        for sj in saved:
            cursor = await db.execute(
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
            counts["saved_jobs"]["inserted"] += max(cursor.rowcount, 0)

        # Migrate reminders
        reminders = load_json_with_backup(REMINDERS_FILE, logger)
        counts["reminders"]["read"] = len(reminders)
        for r in reminders:
            cursor = await db.execute(
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
            counts["reminders"]["inserted"] += max(cursor.rowcount, 0)

        # Migrate interview rounds.
        # NOTE: interview_rounds.id is INTEGER PRIMARY KEY AUTOINCREMENT and
        # job_id is NOT unique, so a plain INSERT OR IGNORE here can never
        # conflict and WOULD duplicate every row on a forced re-run. Guard on
        # the natural key (job_id, round_type, date) instead so this function
        # is genuinely idempotent for all four entities.
        rounds = load_json_with_backup(INTERVIEW_ROUNDS_FILE, logger)
        counts["interview_rounds"]["read"] = len(rounds)
        for rd in rounds:
            cursor = await db.execute(
                "INSERT INTO interview_rounds (job_id, round_type, date, notes, status, created_at) "
                "SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS ("
                "  SELECT 1 FROM interview_rounds"
                "  WHERE job_id IS ? AND round_type IS ? AND date IS ?"
                ")",
                (
                    rd.get("job_id"),
                    rd.get("round_type"),
                    rd.get("date"),
                    rd.get("notes"),
                    rd.get("status"),
                    rd.get("created_at"),
                    rd.get("job_id"),
                    rd.get("round_type"),
                    rd.get("date"),
                ),
            )
            counts["interview_rounds"]["inserted"] += max(cursor.rowcount, 0)

        await db.commit()
    finally:
        await db.close()

    for block in counts.values():
        block["skipped"] = block["read"] - block["inserted"]

    total_inserted = sum(block["inserted"] for block in counts.values())
    await record_migration(JSON_MIGRATION_NAME, total_inserted)

    logger.info(
        "Migrated to SQLite (inserted/read): apps %d/%d, saved %d/%d, "
        "reminders %d/%d, rounds %d/%d",
        counts["applications"]["inserted"], counts["applications"]["read"],
        counts["saved_jobs"]["inserted"], counts["saved_jobs"]["read"],
        counts["reminders"]["inserted"], counts["reminders"]["read"],
        counts["interview_rounds"]["inserted"], counts["interview_rounds"]["read"],
    )

    result = {"status": "applied"}
    result.update(counts)
    return result


# ---------------------------------------------------------------------------
# Repository pattern — thin wrappers formalizing aggregate persistence
# ---------------------------------------------------------------------------


class ApplicationRepository:
    """Repository for Application aggregate — delegates to existing CRUD helpers."""

    @staticmethod
    async def list(status=None, date_from=None, date_to=None, limit=50, offset=0):
        return await list_applications(status, date_from, date_to, limit, offset)

    @staticmethod
    async def get(job_id: str):
        return await get_application(job_id)

    @staticmethod
    async def save(app: dict):
        return await upsert_application(app)

    @staticmethod
    async def delete_before(date: str) -> int:
        return await delete_applications_before(date)

    @staticmethod
    async def count_by_status():
        return await count_applications_by_status()

    @staticmethod
    async def get_applied_ids() -> set:
        return await get_applied_job_ids()


class SavedJobRepository:
    """Repository for SavedJob aggregate — delegates to existing CRUD helpers."""

    @staticmethod
    async def list(limit=50, offset=0):
        return await list_saved_jobs(limit, offset)

    @staticmethod
    async def get(job_id: str):
        return await get_saved_job(job_id)

    @staticmethod
    async def save(sj: dict):
        return await upsert_saved_job(sj)

    @staticmethod
    async def delete(job_id: str) -> bool:
        return await delete_saved_job(job_id)


class ReminderRepository:
    """Repository for Reminder aggregate — delegates to existing CRUD helpers."""

    @staticmethod
    async def list():
        return await list_reminders()

    @staticmethod
    async def get(job_id: str):
        return await get_reminder(job_id)

    @staticmethod
    async def save(rem: dict):
        return await upsert_reminder(rem)

    @staticmethod
    async def delete(job_id: str) -> bool:
        return await delete_reminder(job_id)


class ScheduledRunRepository:
    """Repository for scheduled task run records."""

    @staticmethod
    async def insert(task_name: str, started_at: str) -> int:
        return await insert_scheduled_run(task_name, started_at)

    @staticmethod
    async def update(run_id: int, status: str, finished_at: str,
                     duration_ms: float, result: Optional[str] = None, error: Optional[str] = None):
        return await update_scheduled_run(run_id, status, finished_at, duration_ms, result, error)

    @staticmethod
    async def get_last(task_name: str):
        return await get_last_run(task_name)

    @staticmethod
    async def list(task_name: Optional[str] = None, limit: int = 20):
        return await list_scheduled_runs(task_name, limit)


# ---------------------------------------------------------------------------
# Agent runs CRUD
# ---------------------------------------------------------------------------

async def insert_agent_run(run: dict) -> int:
    """Insert an agent cycle run record. Returns row id."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO agent_runs
               (cycle_id, started_at, status, mode, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (run["cycle_id"], run["started_at"], run.get("status", "running"),
             run.get("mode"), run.get("metadata")),
        )
        await db.commit()
        return cursor.lastrowid or 0
    finally:
        await db.close()


async def update_agent_run(cycle_id: str, **fields):
    """Update an agent run with completion info."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [cycle_id]
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE agent_runs SET {set_clause} WHERE cycle_id = ?",
            values,
        )
        await db.commit()
    finally:
        await db.close()


async def get_agent_run(cycle_id: str) -> Optional[dict]:
    """Get an agent run by cycle_id."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM agent_runs WHERE cycle_id = ?", (cycle_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_agent_runs(limit: int = 20) -> list[dict]:
    """List recent agent runs."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def insert_agent_decision(cycle_id: str, job_id: str, decision: str,
                                 company: str = "", title: str = "",
                                 search_query: str = "", fit_score: int = 0,
                                 skip_reason: Optional[str] = None,
                                 apply_status: Optional[str] = None) -> int:
    """Insert an agent decision for a specific job."""
    from datetime import datetime, timezone
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO agent_decisions
               (cycle_id, job_id, company, title, search_query, fit_score,
                decision, skip_reason, apply_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cycle_id, job_id, company, title, search_query, fit_score,
             decision, skip_reason, apply_status,
             datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cursor.lastrowid or 0
    finally:
        await db.close()


async def update_agent_decision(cycle_id: str, job_id: str, apply_status: str):
    """Update the apply_status of an agent decision."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE agent_decisions SET apply_status = ? WHERE cycle_id = ? AND job_id = ?",
            (apply_status, cycle_id, job_id),
        )
        await db.commit()
    finally:
        await db.close()


async def list_agent_decisions(cycle_id: str) -> list[dict]:
    """List all decisions for a cycle."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM agent_decisions WHERE cycle_id = ? ORDER BY fit_score DESC",
            (cycle_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def count_daily_applied(date_str: str) -> int:
    """Count applications actually submitted on a date (excludes synced imports).

    Only counts source != 'naukri_sync' to avoid inflated counts from
    bulk-synced historical applications that get today's timestamp.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM applications WHERE DATE(applied_at) = ? AND source != 'naukri_sync'",
            (date_str,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
    finally:
        await db.close()


class AgentRepository:
    """Repository for agent cycle run records."""

    @staticmethod
    async def insert_run(run: dict) -> int:
        return await insert_agent_run(run)

    @staticmethod
    async def update_run(cycle_id: str, **fields):
        return await update_agent_run(cycle_id, **fields)

    @staticmethod
    async def get_run(cycle_id: str):
        return await get_agent_run(cycle_id)

    @staticmethod
    async def list_runs(limit: int = 20):
        return await list_agent_runs(limit)

    @staticmethod
    async def insert_decision(cycle_id: str, job_id: str, decision: str, **kw) -> int:
        return await insert_agent_decision(cycle_id, job_id, decision, **kw)

    @staticmethod
    async def update_decision(cycle_id: str, job_id: str, apply_status: str):
        return await update_agent_decision(cycle_id, job_id, apply_status)

    @staticmethod
    async def list_decisions(cycle_id: str):
        return await list_agent_decisions(cycle_id)

    @staticmethod
    async def count_daily(date_str: str) -> int:
        return await count_daily_applied(date_str)


# ---------------------------------------------------------------------------
# Retention / cleanup
# ---------------------------------------------------------------------------

async def cleanup_old_records(days: int = 90) -> dict:
    """Delete rows past the retention horizon from the four append-only tables.

    Covers event_log, scheduled_runs, agent_runs AND agent_decisions - the
    last was in the code but not in this docstring. Called by
    `scheduler_tasks._task_retention_sweep` (daily); before 2026-08-21 it had
    no caller at all.

    The cutoff compares an ISO8601-with-offset column against sqlite's
    `datetime('now', ?)`, which renders `YYYY-MM-DD HH:MM:SS`. The two agree on
    the date part, so the comparison is correct to the day; within the boundary
    day itself 'T' sorts above ' ', so a row from earlier on the cutoff day
    survives one extra day. That errs toward KEEPING forensic rows, which is
    the safe direction, and is why it is documented rather than patched.
    """
    db = await get_db()
    try:
        cutoff = f"-{days} days"

        cursor = await db.execute(
            "DELETE FROM event_log WHERE timestamp < datetime('now', ?)", (cutoff,))
        event_count = cursor.rowcount

        cursor = await db.execute(
            "DELETE FROM scheduled_runs WHERE started_at < datetime('now', ?)", (cutoff,))
        sched_count = cursor.rowcount

        cursor = await db.execute(
            "DELETE FROM agent_runs WHERE started_at < datetime('now', ?)", (cutoff,))
        agent_count = cursor.rowcount

        # agent_decisions cleaned by cascade if FK exists, otherwise manual
        cursor = await db.execute(
            "DELETE FROM agent_decisions WHERE created_at < datetime('now', ?)", (cutoff,))
        decision_count = cursor.rowcount

        await db.commit()
        return {
            "event_log": event_count,
            "scheduled_runs": sched_count,
            "agent_runs": agent_count,
            "agent_decisions": decision_count,
        }
    finally:
        await db.close()


async def get_agent_metrics(days: int = 7) -> dict:
    """Agent performance metrics over time period."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT
                COUNT(*) as cycle_count,
                COALESCE(SUM(jobs_found), 0) as total_found,
                COALESCE(SUM(jobs_matched), 0) as total_matched,
                COALESCE(SUM(applied_count), 0) as total_applied,
                COALESCE(SUM(skipped_count), 0) as total_skipped,
                ROUND(AVG(duration_ms), 0) as avg_duration_ms
            FROM agent_runs
            WHERE started_at >= datetime('now', ?)
              AND status = 'completed'
        """, (f"-{days} days",))
        row = await cursor.fetchone()
        return dict(row) if row else {}
    finally:
        await db.close()


async def get_agent_skip_stats(days: int = 7) -> dict:
    """Aggregate skip reasons for agent decisions."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT skip_reason, COUNT(*) as count
            FROM agent_decisions
            WHERE decision = 'skip' AND skip_reason IS NOT NULL
              AND created_at >= datetime('now', ?)
            GROUP BY skip_reason ORDER BY count DESC
        """, (f"-{days} days",))
        return {row[0]: row[1] for row in await cursor.fetchall()}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Endpoint audit CRUD
# ---------------------------------------------------------------------------

async def log_endpoint_audit(url: str, method: str = "GET", status: str = "NEW", page_source: str = "") -> int:
    """Log a discovered endpoint. Updates last_seen if URL already exists, else inserts.

    Returns the row id.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        # Check if URL already exists
        cursor = await db.execute("SELECT id FROM endpoint_audit WHERE url = ?", (url,))
        row = await cursor.fetchone()
        if row:
            await db.execute(
                "UPDATE endpoint_audit SET last_seen = ?, status = ? WHERE url = ?",
                (now, status, url),
            )
            await db.commit()
            return row["id"]
        else:
            cursor = await db.execute(
                "INSERT INTO endpoint_audit (url, method, status, first_discovered, last_seen, page_source) VALUES (?, ?, ?, ?, ?, ?)",
                (url, method, status, now, now, page_source),
            )
            await db.commit()
            return cursor.lastrowid
    finally:
        await db.close()


async def get_known_endpoints() -> list[dict]:
    """Return all rows from the endpoint_audit table."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM endpoint_audit ORDER BY first_discovered DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Scheduled runs CRUD
# ---------------------------------------------------------------------------

async def insert_scheduled_run(task_name: str, started_at: str) -> int:
    """Insert a new scheduled run record. Returns the row id."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO scheduled_runs (task_name, started_at) VALUES (?, ?)",
            (task_name, started_at),
        )
        await db.commit()
        return cursor.lastrowid or 0
    finally:
        await db.close()


async def update_scheduled_run(run_id: int, status: str, finished_at: str,
                                duration_ms: float, result: Optional[str] = None,
                                error: Optional[str] = None):
    """Update a scheduled run with completion info."""
    db = await get_db()
    try:
        await db.execute(
            """UPDATE scheduled_runs
               SET status = ?, finished_at = ?, duration_ms = ?, result = ?, error = ?
               WHERE id = ?""",
            (status, finished_at, duration_ms, result, error, run_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_last_run(task_name: str) -> Optional[dict]:
    """Get the most recent run for a task."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM scheduled_runs WHERE task_name = ? ORDER BY started_at DESC LIMIT 1",
            (task_name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_scheduled_runs(task_name: Optional[str] = None, limit: int = 20) -> list[dict]:
    """List recent scheduled runs, optionally filtered by task name."""
    db = await get_db()
    try:
        if task_name:
            cursor = await db.execute(
                "SELECT * FROM scheduled_runs WHERE task_name = ? ORDER BY started_at DESC LIMIT ?",
                (task_name, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM scheduled_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Auto-fix pending CRUD (T2 healing snapshot+revert protection)
# ---------------------------------------------------------------------------

async def insert_auto_fix_pending(
    commit_sha: str,
    pre_fix_sha: str,
    constant_name: str,
    applied_at: str,
    verify_at: str,
) -> int:
    """Record a T2 auto-fix awaiting verification. Returns the new row id."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """
            INSERT INTO auto_fix_pending
              (commit_sha, pre_fix_sha, constant_name, applied_at, verify_at, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (commit_sha, pre_fix_sha, constant_name, applied_at, verify_at),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def list_auto_fix_pending(status: Optional[str] = None,
                                  due_before: Optional[str] = None,
                                  limit: int = 100) -> list[dict]:
    """List auto-fix rows, optionally filtered by status and/or verify_at cutoff."""
    db = await get_db()
    try:
        clauses = []
        params: list = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if due_before is not None:
            clauses.append("verify_at <= ?")
            params.append(due_before)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cursor = await db.execute(
            f"SELECT * FROM auto_fix_pending {where} ORDER BY applied_at ASC LIMIT ?",
            tuple(params),
        )
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()


async def update_auto_fix_status(
    row_id: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    """Update an auto-fix row's status (and optional error message)."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE auto_fix_pending SET status = ?, error = ? WHERE id = ?",
            (status, error, row_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_auto_fix_pending(row_id: int) -> Optional[dict]:
    """Fetch a single auto-fix row by id."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM auto_fix_pending WHERE id = ?", (row_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()
