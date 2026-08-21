"""Clear the banked ReminderDue backlog and backfill reminder title/company.

The auditable record of exactly what was changed in naukri.db on 2026-08-21,
in the same spirit as scripts/purge_test_rows.py.

WHAT IT DOES, in one transaction:
  1. BACKFILL  reminders.title / reminders.company from the local `applications`
     row with the same job_id. All 50 of his reminders had NULL for both, which
     is why every one of them read "Follow up on application to unknown", and
     all 50 have a complete applications row. Pure enrichment of his own local
     records; no API call, nothing deleted.
  2. DELETE    notifications WHERE event_type='ReminderDue'. 1,084 rows holding
     50 distinct facts (~21 duplicates each) - the banked output of the storm
     fixed in reminder_service.py / subscribers.py. Deleting all of them rather
     than keeping one per job is deliberate: every body carries the stale
     "unknown" text from before the backfill, and after the server restarts the
     hourly reminder_check regenerates exactly one correct, deduped notification
     per due reminder.

WHAT IT REFUSES TO DO:
  - No `%test%`/`%probe%`/`%cleanup%` body matching. That filter looks obvious
    and is a trap: on this DB it also catches 12 real ProbeStateChanged and 10
    real BrowserCrashed rows whose bodies merely read "Probe failed" / "Health
    probe failed". Only the event_type column decides.
  - It never touches event_log. Those 1,127 ReminderDue rows are the forensic
    record of the storm, not a work queue.

PROOF: notification row ids are captured per event_type before and after, and
the script asserts that no id of any other event_type disappeared. The live MCP
server may be writing concurrently, so ADDITIONS are expected and reported;
only DISAPPEARANCES would mean this script over-reached.

Usage:
    python scripts/fix_reminder_storm.py            # dry run, changes nothing
    python scripts/fix_reminder_storm.py --apply
"""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "naukri.db"
BACKUP_DIR = ROOT / "_backup"  # gitignored; `backups/` is NOT


def connect(path, read_only=False):
    uri = "file:%s%s" % (path.as_posix(), "?mode=ro" if read_only else "")
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def table_counts(con):
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {n: con.execute("SELECT COUNT(*) FROM [%s]" % n).fetchone()[0] for n in names}


def ids_by_type(con):
    out = {}
    for event_type, row_id in con.execute("SELECT event_type, id FROM notifications"):
        out.setdefault(event_type, set()).add(row_id)
    return out


def make_backup(live):
    """Online backup - safe against the running server, unlike a file copy."""
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / ("naukri.db.bak-%s" % stamp)
    bak = sqlite3.connect(dest)
    try:
        live.backup(bak)
    finally:
        bak.close()

    verify = connect(dest, read_only=True)
    try:
        integrity = verify.execute("PRAGMA integrity_check").fetchone()[0]
        counts = table_counts(verify)
    finally:
        verify.close()
    return dest, integrity, counts


def main():
    apply = "--apply" in sys.argv
    if not DB.exists():
        sys.exit("no such db: %s" % DB)

    live = connect(DB)
    try:
        print("DB: %s (%d bytes)" % (DB, DB.stat().st_size))

        # --- 1. Backup, verified BEFORE any write -----------------------
        dest, integrity, bak_counts = make_backup(live)
        live_counts = table_counts(live)
        print("\nBACKUP  %s (%d bytes)" % (dest, dest.stat().st_size))
        print("  integrity_check = %s" % integrity)
        mismatch = {t: (live_counts.get(t), bak_counts.get(t))
                    for t in set(live_counts) | set(bak_counts)
                    if live_counts.get(t) != bak_counts.get(t)}
        for t in sorted(set(live_counts) | set(bak_counts)):
            print("  %-22s live=%-7s backup=%s" % (t, live_counts.get(t), bak_counts.get(t)))
        if integrity != "ok":
            sys.exit("ABORT: backup failed integrity_check")
        if mismatch:
            # A live insert between backup and re-count is possible; report it
            # rather than pretending the snapshot is frozen.
            print("  NOTE: counts moved during the snapshot (live server is running): %s"
                  % mismatch)

        # --- 2. Pre-state ------------------------------------------------
        pre = ids_by_type(live)
        print("\nPRE-STATE notifications by event_type:")
        for t in sorted(pre, key=lambda k: -len(pre[k])):
            print("  %-24s %d" % (t, len(pre[t])))
        pre_total = sum(len(v) for v in pre.values())
        pre_undelivered = live.execute(
            "SELECT COUNT(*) FROM notifications WHERE delivered_via IS NULL").fetchone()[0]
        print("  TOTAL %d   undelivered (pending_notifications) %d"
              % (pre_total, pre_undelivered))

        backfillable = live.execute("""
            SELECT COUNT(*) FROM reminders r
            JOIN applications a ON a.job_id = r.job_id
            WHERE (r.title IS NULL OR r.title = '' OR r.company IS NULL OR r.company = '')
              AND (a.title IS NOT NULL AND a.title != ''
                   OR a.company IS NOT NULL AND a.company != '')
        """).fetchone()[0]
        print("\nBACKFILL candidates (reminder with a complete applications row): %d"
              % backfillable)

        if not apply:
            print("\nDRY RUN - nothing changed. Re-run with --apply.")
            return

        # --- 3. Mutate, in one transaction -------------------------------
        live.execute("BEGIN IMMEDIATE")

        cur = live.execute("""
            UPDATE reminders
               SET title = COALESCE(NULLIF(title, ''),
                       (SELECT NULLIF(a.title, '') FROM applications a
                         WHERE a.job_id = reminders.job_id)),
                   company = COALESCE(NULLIF(company, ''),
                       (SELECT NULLIF(a.company, '') FROM applications a
                         WHERE a.job_id = reminders.job_id))
             WHERE EXISTS (SELECT 1 FROM applications a WHERE a.job_id = reminders.job_id)
               AND (title IS NULL OR title = '' OR company IS NULL OR company = '')
        """)
        backfilled = cur.rowcount

        cur = live.execute("DELETE FROM notifications WHERE event_type = 'ReminderDue'")
        deleted = cur.rowcount

        live.commit()
        print("\nAPPLIED  reminders backfilled=%d   ReminderDue notifications deleted=%d"
              % (backfilled, deleted))

        # --- 4. Post-state + row-id diff proof ---------------------------
        post = ids_by_type(live)
        print("\nPOST-STATE notifications by event_type:")
        for t in sorted(post, key=lambda k: -len(post[k])):
            print("  %-24s %d" % (t, len(post[t])))
        post_undelivered = live.execute(
            "SELECT COUNT(*) FROM notifications WHERE delivered_via IS NULL").fetchone()[0]
        print("  TOTAL %d   undelivered (pending_notifications) %d"
              % (sum(len(v) for v in post.values()), post_undelivered))

        print("\nROW-ID DIFF (by event_type):")
        clean = True
        for t in sorted(set(pre) | set(post)):
            lost = pre.get(t, set()) - post.get(t, set())
            gained = post.get(t, set()) - pre.get(t, set())
            if t == "ReminderDue":
                print("  %-24s -%d (intended)  +%d" % (t, len(lost), len(gained)))
                if post.get(t):
                    print("     ^ live server re-minted %d while this ran (it is still on "
                          "the OLD code until restart)" % len(post[t]))
            else:
                flag = "" if not lost else "   <-- UNINTENDED LOSS"
                if lost:
                    clean = False
                print("  %-24s -%d  +%d%s" % (t, len(lost), len(gained), flag))
        print("\nVERDICT: %s" % (
            "clean - no non-ReminderDue notification id disappeared" if clean
            else "FAILED - rows of another event_type were deleted"))

        sample = live.execute(
            "SELECT job_id, title, company FROM reminders "
            "WHERE title IS NOT NULL LIMIT 5").fetchall()
        print("\nBackfilled sample:")
        for row in sample:
            print("  %s  %s @ %s" % row)
        still_null = live.execute(
            "SELECT COUNT(*) FROM reminders WHERE title IS NULL OR company IS NULL"
        ).fetchone()[0]
        print("  reminders still NULL title/company: %d" % still_null)
    finally:
        live.close()


if __name__ == "__main__":
    main()
