"""Backup naukri.db, then delete ONLY self-identifying test rows.

Deletes exactly three scoped sets:
  reminders    : job_id 'test-tier12' (note 'Tier 12 test reminder')
                 job_id '020426009351' (note 'CLEANUP - ignore')
  notifications: ReminderDue rows whose body is one of those two junk notes
                 SavedJobExpiring rows for the placeholder job 'Test Corp'
  saved_jobs   : job_id '12345678' ('Test Python Developer' @ 'Test Corp')

Deliberately NOT touched: 22 ProbeStateChanged + 10 BrowserCrashed rows (they
match a naive '%probe%' filter only because their bodies read "Probe failed" --
they are real operational history), the 50 real reminders, and the ~1074 real
ReminderDue notifications behind them.
"""
import datetime
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Resolved at run time from this file's own location (scripts/ -> repo root) so
# no machine layout is baked in. Both constants are kept as `str`, NOT `Path`:
# they are passed to `.replace("\\", "/")` below to build the sqlite URI, and
# `Path.replace()` is a filesystem RENAME, not a string substitution.
_REPO_ROOT = Path(__file__).resolve().parents[1]
DB = str(_REPO_ROOT / "naukri.db")
# _backup/ is the repo's established home for copies of live data and is the
# directory .gitignore already excludes. Anything else leaves a 1.3 MB copy of
# the live database sitting untracked-but-committable.
BACKUP_DIR = str(_REPO_ROOT / "_backup")

REMINDER_IDS = ("test-tier12", "020426009351")
JUNK_NOTES = ("Tier 12 test reminder", "CLEANUP - ignore")
TEST_SAVED_JOB = "12345678"
TEST_SAVED_TITLE = "Saved job expiring: Test Python Developer at Test Corp"


def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, "naukri.db.bak-%s" % stamp)
    src = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)          # online backup API: WAL-safe, unlike a file copy
    dst.close()
    src.close()
    return path


def verify(path, live_counts):
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    counts = {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
              for t in live_counts}
    con.close()
    return integrity, counts


def table_counts(con, tables):
    return {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in tables}


def main():
    tables = ["reminders", "notifications", "saved_jobs", "applications"]

    ro = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    before = table_counts(ro, tables)
    ro.close()

    path = backup()
    size = os.path.getsize(path)
    integrity, bcounts = verify(path, before)
    print("BACKUP  %s" % path)
    print("        %d bytes | integrity_check=%s" % (size, integrity))
    for t in tables:
        flag = "OK" if bcounts[t] == before[t] else "MISMATCH"
        print("        %-14s live=%-6d backup=%-6d %s" % (t, before[t], bcounts[t], flag))
    if integrity != "ok" or any(bcounts[t] != before[t] for t in tables):
        print("ABORT: backup did not verify; nothing deleted.")
        return 1

    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    deleted = {}
    with con:
        cur = con.execute(
            "DELETE FROM reminders WHERE job_id IN (?,?)", REMINDER_IDS)
        deleted["reminders"] = cur.rowcount
        cur = con.execute(
            "DELETE FROM notifications WHERE event_type='ReminderDue' AND body IN (?,?)",
            JUNK_NOTES)
        deleted["notifications.ReminderDue"] = cur.rowcount
        cur = con.execute(
            "DELETE FROM notifications WHERE event_type='SavedJobExpiring' AND title=?",
            (TEST_SAVED_TITLE,))
        deleted["notifications.SavedJobExpiring"] = cur.rowcount
        cur = con.execute(
            "DELETE FROM saved_jobs WHERE job_id=?", (TEST_SAVED_JOB,))
        deleted["saved_jobs"] = cur.rowcount

    after = table_counts(con, tables)
    con.close()

    print("\nDELETED")
    for k, v in deleted.items():
        print("        %-34s %d" % (k, v))
    print("\nTABLE COUNTS  before -> after")
    for t in tables:
        print("        %-14s %-6d -> %-6d  (%+d)" % (t, before[t], after[t], after[t] - before[t]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
