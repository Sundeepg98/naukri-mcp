# -*- coding: utf-8 -*-
"""Delete the banked BrowserCrashed notification backlog. Proof, not trust.

2342 BrowserCrashed rows, ~93% of his unread notifications, banked by the
watchdog storm fixed in 6641a50. They are noise, but the DELETE that removes
them is irreversible, so every step is verified:

  * back up first, to _backup/ (gitignored; backups/ is NOT)
  * integrity_check the BACKUP, not just the live file
  * match on event_type EXACTLY -- never on body/title. A body match would
    also catch real ProbeStateChanged rows and any legitimate BrowserCrashed
    history we want to keep the shape of.
  * prove by ROW-ID DIFF that the only ids removed are the ones intended, and
    that nothing else moved
  * leave event_log completely alone

Run with --apply to actually delete; default is a dry run.
"""
import argparse
import datetime
import hashlib
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "naukri.db")
BACKUP_DIR = os.path.join(ROOT, "_backup")
TARGET_EVENT = "BrowserCrashed"
TABLES = ["applications", "notifications", "event_log", "reminders", "saved_jobs",
          "interview_rounds", "agent_runs", "agent_decisions", "scheduled_runs",
          "screening_questions", "auto_fix_pending", "endpoint_audit",
          "applications_archive"]


def counts(con):
    out = {}
    for t in TABLES:
        try:
            out[t] = con.execute('select count(*) from "%s"' % t).fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = None
    return out


def notif_ids(con):
    return {r[0] for r in con.execute("select id from notifications")}


def event_log_fingerprint(path):
    """Fingerprint event_log from a FRESH read-only connection at `path`.

    Two ways this cried wolf on the first run, both fixed here:

      * it took the "before" hash from a connection opened BEFORE the WAL
        checkpoint, so it read a stale snapshot and any row the live server
        committed in between looked like a change;
      * it hashed `repr(row)` off a connection carrying `row_factory =
        sqlite3.Row`, whose repr differs from a plain tuple -- so two
        fingerprints taken by different code were never comparable at all.

    Both produced "SOMETHING MOVED" against a table that was byte-identical.
    A verifier that reports a false alarm gets ignored the next time it is
    right, so it is worth more than the delete it guards.
    """
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        rows = con.execute(
            "select id, event_type, timestamp from event_log order by id").fetchall()
    finally:
        con.close()
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(tuple(r)).encode())
    return len(rows), h.hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row

    before_counts = counts(con)
    before_ids = notif_ids(con)
    doomed = {r[0] for r in con.execute(
        "select id from notifications where event_type = ?", (TARGET_EVENT,))}

    print("BEFORE")
    print("  notifications        : %d" % before_counts["notifications"])
    print("  of which %-12s: %d" % (TARGET_EVENT, len(doomed)))
    print("  event_log            : %d rows" % before_counts["event_log"])
    print("  other event_types kept:")
    for r in con.execute("select event_type, count(*) n from notifications "
                         "where event_type != ? group by event_type order by n desc",
                         (TARGET_EVENT,)):
        print("      %-24s %5d" % (r["event_type"], r["n"]))

    if not args.apply:
        print("\nDRY RUN - nothing deleted. Re-run with --apply.")
        con.close()
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BACKUP_DIR, "naukri-pre-crashpurge-%s.db" % stamp)
    con.close()                       # checkpoint WAL before copying
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA wal_checkpoint(FULL)")
    con.close()
    shutil.copy2(DB, backup)
    # Only NOW is the "before" state stable and checkpointed.
    el_before = event_log_fingerprint(DB)
    print("\nBACKUP  -> %s (%.1f MB)" % (backup, os.path.getsize(backup) / 1e6))

    bcon = sqlite3.connect(backup)
    ok = bcon.execute("PRAGMA integrity_check").fetchone()[0]
    bcounts = {t: (bcon.execute('select count(*) from "%s"' % t).fetchone()[0]
                   if before_counts[t] is not None else None) for t in TABLES}
    bcon.close()
    print("  integrity_check      : %s" % ok)
    if ok != "ok":
        print("ABORT: backup failed integrity_check")
        return 1
    mismatch = {t: (before_counts[t], bcounts[t])
                for t in TABLES if before_counts[t] != bcounts[t]}
    if mismatch:
        print("ABORT: backup table counts differ from live: %s" % mismatch)
        return 1
    print("  table counts match   : yes (%d tables)" % len(TABLES))

    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.execute("delete from notifications where event_type = ?", (TARGET_EVENT,))
    deleted = cur.rowcount
    con.commit()

    after_counts = counts(con)
    after_ids = notif_ids(con)
    el_after = event_log_fingerprint(DB)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]

    removed = before_ids - after_ids
    added = after_ids - before_ids          # scheduler may write during the run

    print("\nAFTER")
    print("  deleted rows         : %d" % deleted)
    print("  integrity_check      : %s" % integrity)
    print("  notifications        : %d -> %d" % (
        before_counts["notifications"], after_counts["notifications"]))

    print("\nPROOF")
    print("  removed ids == the %s ids : %s" % (TARGET_EVENT, removed == doomed))
    print("  ids removed that were NOT targeted: %d" % len(removed - doomed))
    print("  targeted ids still present        : %d" % len(doomed & after_ids))
    print("  new ids written during the run    : %d" % len(added))
    print("  event_log untouched               : %s (%s -> %s)" % (
        el_before == el_after, el_before[1], el_after[1]))
    others_ok = all(before_counts[t] == after_counts[t]
                    for t in TABLES if t != "notifications")
    print("  every other table unchanged       : %s" % others_ok)

    con.close()
    good = (removed == doomed and not (removed - doomed)
            and not (doomed & after_ids) and el_before == el_after and others_ok
            and integrity == "ok")
    print("\nVERDICT: %s" % ("clean" if good else "SOMETHING MOVED - restore from backup"))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
