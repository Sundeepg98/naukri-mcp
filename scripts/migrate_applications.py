"""Standalone runner for the one-shot applications.json -> SQLite migration.

The migration itself lives in naukri_server.database.migrate_json_to_sqlite();
this script is the hand-operated front end for it. It takes a WAL-consistent
backup of the target database FIRST, prints BEFORE / AFTER row counts, and
refuses to migrate if the backup could not be taken.

Usage
-----
    python scripts/migrate_applications.py --dry-run
    python scripts/migrate_applications.py
    python scripts/migrate_applications.py --force
    python scripts/migrate_applications.py --db /path/to/copy/naukri.db --dry-run

Safety notes
------------
* The database is backed up with sqlite3.Connection.backup(), NOT a file copy.
  naukri.db runs in WAL mode, where a plain copy of the .db file can miss
  committed pages that still live in the -wal file.
* The source database is opened READ-ONLY for the backup, so the backup step
  itself can never modify the live database.
* naukri.db / naukri.db-wal / naukri.db-shm are never moved or deleted.
* The server may be running; WAL allows concurrent readers and writers, and
  every connection here sets a busy_timeout.
* --dry-run writes nothing at all -- not even a backup. It prints the paths it
  would have written.

Exit codes: 0 on success, 1 on any failure.
"""

import argparse
import asyncio
import shutil
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENTITIES = ("applications", "saved_jobs", "reminders", "interview_rounds")
BUSY_TIMEOUT_MS = 15000


# ---------------------------------------------------------------------------
# Low-level sqlite helpers (plain sqlite3, no aiosqlite -- this is a script)
# ---------------------------------------------------------------------------


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open the database READ-ONLY. Never mutates the target."""
    con = sqlite3.connect("file:%s?mode=ro" % db_path.as_posix(), uri=True)
    con.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
    return con


def _table_names(con: sqlite3.Connection) -> set:
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in cur.fetchall()}


def _row_counts(db_path: Path) -> dict:
    """Row count per tracked entity. Missing table -> None."""
    con = _connect_ro(db_path)
    try:
        present = _table_names(con)
        counts = {}
        for table in ENTITIES:
            if table not in present:
                counts[table] = None
                continue
            counts[table] = con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
        return counts
    finally:
        con.close()


def _existing_keys(db_path: Path) -> dict:
    """Existing primary keys per entity, for the dry-run delta calculation."""
    con = _connect_ro(db_path)
    try:
        present = _table_names(con)
        keys = {}
        for table in ("applications", "saved_jobs", "reminders"):
            if table not in present:
                keys[table] = set()
                continue
            keys[table] = {
                row[0] for row in con.execute("SELECT job_id FROM " + table).fetchall()
            }
        if "interview_rounds" in present:
            keys["interview_rounds"] = {
                (r[0], r[1], r[2])
                for r in con.execute(
                    "SELECT job_id, round_type, date FROM interview_rounds"
                ).fetchall()
            }
        else:
            keys["interview_rounds"] = set()
        return keys
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def _backup_paths(db_path: Path, stamp: str):
    backup_dir = db_path.parent / "_backup"
    return (
        backup_dir,
        backup_dir / ("%s.premigration-%s" % (db_path.name, stamp)),
        backup_dir / ("applications.json.premigration-%s" % stamp),
    )


def _take_backup(db_path: Path, apps_file: Path, stamp: str):
    """WAL-consistent DB backup + a copy of applications.json.

    Returns (db_backup_path, json_backup_path). Raises on any failure -- the
    caller aborts the migration rather than proceeding unprotected.
    """
    backup_dir, db_backup, json_backup = _backup_paths(db_path, stamp)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # sqlite3 online backup API: reads the live DB *including* the WAL and
    # writes a fully checkpointed, self-consistent copy.
    src = _connect_ro(db_path)
    try:
        dst = sqlite3.connect(str(db_backup))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()

    if not db_backup.exists() or db_backup.stat().st_size == 0:
        raise RuntimeError("backup file missing or empty: %s" % db_backup)

    # Prove the backup is readable and carries the pre-migration state.
    verify = _connect_ro(db_backup)
    try:
        tables = _table_names(verify)
        if "applications" not in tables:
            raise RuntimeError("backup has no applications table: %s" % db_backup)
        verified_rows = verify.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    finally:
        verify.close()

    if apps_file.exists():
        shutil.copy2(str(apps_file), str(json_backup))
    else:
        json_backup = None

    return db_backup, json_backup, verified_rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt(value):
    return "n/a" if value is None else str(value)


def _print_counts(title: str, counts: dict):
    print("")
    print(title)
    print("  %-18s %10s" % ("table", "rows"))
    print("  " + "-" * 29)
    for table in ENTITIES:
        print("  %-18s %10s" % (table, _fmt(counts[table])))


def _print_delta(before: dict, after: dict):
    print("")
    print("ROW COUNT DELTA")
    print("  %-18s %10s %10s %10s" % ("table", "before", "after", "delta"))
    print("  " + "-" * 51)
    for table in ENTITIES:
        b, a = before[table], after[table]
        delta = "n/a" if (b is None or a is None) else "%+d" % (a - b)
        print("  %-18s %10s %10s %10s" % (table, _fmt(b), _fmt(a), delta))


def _print_result(result: dict):
    print("")
    print("MIGRATION RESULT  status=%s%s" % (
        result.get("status"),
        "  reason=%s" % result["reason"] if result.get("reason") else "",
    ))
    print("  %-18s %8s %10s %9s" % ("entity", "read", "inserted", "skipped"))
    print("  " + "-" * 48)
    for table in ENTITIES:
        block = result.get(table) or {}
        print("  %-18s %8s %10s %9s" % (
            table,
            block.get("read", "-"),
            block.get("inserted", "-"),
            block.get("skipped", "-"),
        ))


def _print_ledger(db_path: Path):
    con = _connect_ro(db_path)
    try:
        if "migrations" not in _table_names(con):
            print("")
            print("LEDGER: no migrations table")
            return
        rows = con.execute(
            "SELECT name, applied_at, rows_inserted FROM migrations ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    print("")
    print("LEDGER (migrations table)")
    if not rows:
        print("  (empty)")
    for name, applied_at, rows_inserted in rows:
        print("  %-24s applied_at=%s rows_inserted=%s"
              % (name, applied_at, rows_inserted))


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def _dry_run(db_path: Path, files: dict, stamp: str, force: bool) -> int:
    from naukri_server.utils import load_json_with_backup
    import logging

    logger = logging.getLogger("migrate_applications")

    _, db_backup, json_backup = _backup_paths(db_path, stamp)
    print("")
    print("DRY RUN -- nothing will be written.")
    print("  would back up DB   -> %s" % db_backup)
    print("  would back up JSON -> %s" % json_backup)

    before = _row_counts(db_path)
    _print_counts("BEFORE (current row counts)", before)

    existing = _existing_keys(db_path)
    print("")
    print("WOULD INSERT (records whose key is not already in the DB)")
    print("  %-18s %8s %10s %9s" % ("entity", "read", "would_ins", "would_skip"))
    print("  " + "-" * 48)

    plans = {}
    for table, path, keyfn in (
        ("applications", files["applications"], lambda r: r.get("job_id")),
        ("saved_jobs", files["saved_jobs"], lambda r: r.get("job_id")),
        ("reminders", files["reminders"], lambda r: r.get("job_id")),
        ("interview_rounds", files["interview_rounds"],
         lambda r: (r.get("job_id"), r.get("round_type"), r.get("date"))),
    ):
        records = load_json_with_backup(path, logger)
        seen = set(existing[table])
        would_insert = 0
        for rec in records:
            key = keyfn(rec)
            if key not in seen:
                seen.add(key)
                would_insert += 1
        plans[table] = (len(records), would_insert)
        print("  %-18s %8d %10d %9d"
              % (table, len(records), would_insert, len(records) - would_insert))

    ledger_seen = _ledger_has_migration(db_path)
    print("")
    if ledger_seen and not force:
        print("LEDGER GUARD: json_to_sqlite_v1 is already recorded.")
        print("  A real run WITHOUT --force would report status=skipped and")
        print("  insert nothing. Re-run with --force to apply the plan above.")
    elif ledger_seen and force:
        print("LEDGER GUARD: json_to_sqlite_v1 already recorded, but --force was")
        print("  given, so a real run would apply the plan above.")
    else:
        print("LEDGER GUARD: json_to_sqlite_v1 not yet recorded -- a real run")
        print("  would apply the plan above.")

    print("")
    print("DRY RUN COMPLETE -- database unchanged, no files written.")
    return 0


def _ledger_has_migration(db_path: Path) -> bool:
    con = _connect_ro(db_path)
    try:
        if "migrations" not in _table_names(con):
            return False
        row = con.execute(
            "SELECT 1 FROM migrations WHERE name = ?", ("json_to_sqlite_v1",)
        ).fetchone()
        return row is not None
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Import applications.json (and saved_jobs / reminders / "
                    "interview_rounds) into the SQLite database.",
    )
    parser.add_argument(
        "--db",
        default=str(REPO_ROOT / "naukri.db"),
        help="path to the SQLite database (default: naukri.db at the repo root)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run even if the migrations ledger already records json_to_sqlite_v1",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be inserted and exit; writes nothing",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    from naukri_server.config import (
        APPLICATIONS_FILE,
        SAVED_JOBS_FILE,
        REMINDERS_FILE,
        INTERVIEW_ROUNDS_FILE,
    )

    files = {
        "applications": Path(APPLICATIONS_FILE),
        "saved_jobs": Path(SAVED_JOBS_FILE),
        "reminders": Path(REMINDERS_FILE),
        "interview_rounds": Path(INTERVIEW_ROUNDS_FILE),
    }

    print("naukri JSON -> SQLite migration runner")
    print("  utc            : %s" % datetime.now(timezone.utc).isoformat())
    print("  database       : %s" % db_path)
    print("  force          : %s" % args.force)
    print("  dry-run        : %s" % args.dry_run)
    for name in ENTITIES:
        path = files[name]
        print("  %-16s : %s%s" % (
            name, path, "" if path.exists() else "   (MISSING -- treated as empty)"))

    if not db_path.exists():
        print("")
        print("ERROR: database not found: %s" % db_path)
        print("This script never creates a database. Point --db at an existing one.")
        return 1

    try:
        present = None
        con = _connect_ro(db_path)
        try:
            present = _table_names(con)
        finally:
            con.close()
        missing = [t for t in ENTITIES if t not in present]
        if missing:
            print("")
            print("ERROR: database is missing required tables: %s" % ", ".join(missing))
            print("Start the server once (init_db) before migrating.")
            return 1
    except Exception:
        print("")
        print("ERROR: could not read the database.")
        traceback.print_exc()
        return 1

    if args.dry_run:
        try:
            return _dry_run(db_path, files, stamp, args.force)
        except Exception:
            print("")
            print("ERROR: dry run failed.")
            traceback.print_exc()
            return 1

    # ---- backup FIRST; abort the whole run if it fails --------------------
    try:
        db_backup, json_backup, verified_rows = _take_backup(
            db_path, files["applications"], stamp
        )
    except Exception:
        print("")
        print("ERROR: backup failed -- ABORTING without migrating.")
        traceback.print_exc()
        return 1

    print("")
    print("BACKUP OK (sqlite3 online backup API, WAL-consistent)")
    print("  database -> %s" % db_backup)
    print("  json     -> %s" % (json_backup if json_backup else
                                "(applications.json missing, nothing copied)"))
    print("  verified : backup opens clean, applications rows = %d" % verified_rows)

    before = _row_counts(db_path)
    _print_counts("BEFORE (row counts)", before)

    try:
        result = asyncio.run(_run_migration(db_path, args.force))
    except Exception:
        print("")
        print("ERROR: migration failed. The pre-migration backup is at:")
        print("  %s" % db_backup)
        traceback.print_exc()
        return 1

    after = _row_counts(db_path)
    _print_result(result)
    _print_delta(before, after)
    _print_ledger(db_path)

    print("")
    print("DONE. Backup retained at %s" % db_backup)
    return 0


async def _run_migration(db_path: Path, force: bool) -> dict:
    """Run the migration with database.DB_PATH pointed at the chosen file."""
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import migrate_json_to_sqlite
        return await migrate_json_to_sqlite(force=force)


if __name__ == "__main__":
    sys.exit(main())
