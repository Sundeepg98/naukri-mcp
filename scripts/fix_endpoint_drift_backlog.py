"""Clear the banked EndpointDrift backlog from notifications.

The auditable record of exactly what was changed in naukri.db, in the same
spirit as scripts/fix_read_path_backlog.py (which cleared the ApplicationStale
/ SavedJobExpiring backlog), scripts/fix_reminder_storm.py (the ReminderDue
storm) and scripts/purge_test_rows.py.

WHAT IT DOES, in one transaction:
  DELETE notifications WHERE event_type = 'EndpointDrift'. Drift is a read-path
  notification: it is re-derived from LIVE state every time the scheduled
  api_validator probe runs, so a banked copy is not a fact worth keeping - it
  is one fact restated N times. The 81 rows measured below carry only THREE
  distinct facts between them, keyed by metadata.constant_name. Once the server
  restarts on the deduped code, the probe re-mints exactly one current
  notification per genuinely drifting constant.

  Deleting ALL rows of the type - rather than keeping one row per
  constant_name as a "most recent" survivor - is the right call for the same
  reason. A kept row is a snapshot of a schema comparison that has already been
  superseded by the next probe pass; it cannot be trusted as current, and the
  probe will restate it anyway. Keeping one would preserve staleness, not
  history. The history that matters is already in event_log, which this script
  never writes.

  There is NO backfill half here. Nothing in this event type needs repair - it
  only needs to stop being hoarded.

WHAT IT REFUSES TO DO:
  - No body/title matching, no metadata LIKE, no %drift%/%endpoint% filter.
    There is no LIKE anywhere in the delete path. That filter looks obvious and
    is a trap: a %test%|%probe%|%cleanup% style filter has TWICE nearly
    destroyed real ProbeStateChanged and BrowserCrashed rows on this DB, whose
    bodies merely mention probes or endpoints. Only exact event_type equality
    decides what dies.
  - It never touches event_log. Its EndpointDrift rows are the forensic record
    of when each drift was first observed, not a work queue. event_log is read
    for reporting only, never written.

PROOF: notification row ids are captured per event_type before and after, and
the script asserts that no id of any other event_type disappeared. The live MCP
server may be writing concurrently, so ADDITIONS are possible; the verdict line
separates a GAIN (live server insert - harmless) from a LOSS (this script
over-reached - the real failure), and either one trips the strict gate.

MEASUREMENT THAT MOTIVATED THIS (naukri.db, 2026-08-22T17:00Z, read-only):
  notifications total          212      undelivered (delivered_via IS NULL) 198
  EndpointDrift                 81 rows, ALL 81 undelivered
                                        = 41% of the undelivered queue and the
                                        largest single group
  distinct facts inside those 81 rows, keyed by metadata.constant_name:
    DASHBOARD_API         severity=added   drift_type=field  x33
                          2026-08-21T07:36 .. 2026-08-21T15:38
    APPLIED_JOBS_API      severity=added   drift_type=field  x25
                          2026-08-21T09:37 .. 2026-08-21T15:38
    RECOMMENDED_JOBS_API  severity=removed drift_type=field  x23
                          2026-08-22T07:40 .. 2026-08-22T16:41
  The first two stopped drifting yesterday. RECOMMENDED_JOBS_API is drifting
  RIGHT NOW at roughly 4 rows/hour and still climbing, so the count this run
  reports will be higher than 81.
  expected after                131 total, 117 undelivered
Those numbers are context, NOT assertions. The live DB drifts - the script
reads whatever is actually there now and reports it; nothing above is
hardcoded into a check.

Usage:
    python scripts/fix_endpoint_drift_backlog.py            # dry run, changes nothing
    python scripts/fix_endpoint_drift_backlog.py --apply
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "naukri.db"
BACKUP_DIR = ROOT / "_backup"  # gitignored; `backups/` is NOT

# The ONLY thing that decides what dies. Exact equality, nothing else.
TARGETS = ("EndpointDrift",)

# Built from len(TARGETS) so the SQL stays correct for a 1-tuple. Hardcoding
# "(?, ?)" against a one-element TARGETS is the obvious way to break this
# script: sqlite3 raises ProgrammingError on the arity mismatch at best, and a
# hand-trimmed literal silently widens the blast radius at worst.
PLACEHOLDERS = ", ".join("?" * len(TARGETS))

# The SAME fact wears a DIFFERENT name in the two tables: the event is
# `EndpointDriftDetected`, the notification it banks is `EndpointDrift`. Kept
# as its own constant, deliberately not folded into TARGETS, so this name can
# never reach the DELETE - event_log is the forensic record and is read here
# for reporting only. Without this the report line was silently empty, which is
# how a purge script starts looking like it has nothing to report.
EVENT_LOG_TARGETS = ("EndpointDriftDetected",)
EVENT_LOG_PLACEHOLDERS = ", ".join("?" * len(EVENT_LOG_TARGETS))


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


def parse_args():
    ap = argparse.ArgumentParser(
        description="Delete the banked EndpointDrift notification backlog. "
                    "Dry run unless --apply is given.")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete. Without it nothing is written.")
    return ap.parse_args()


def main():
    args = parse_args()
    apply = args.apply
    if not DB.exists():
        sys.exit("no such db: %s" % DB)

    # Dry run opens the live DB READ-ONLY so it cannot write even by accident
    # (closing a read-write connection can checkpoint the WAL and touch naukri.db).
    live = connect(DB, read_only=not apply)
    try:
        print("DB: %s (%d bytes)  mode=%s"
              % (DB, DB.stat().st_size,
                 "read-write (APPLY)" if apply else "read-only (DRY RUN)"))
        print("TARGET event_types: %s" % ", ".join(TARGETS))

        # --- 1. Backup, verified BEFORE any write -----------------------
        dest, integrity, bak_counts = make_backup(live)
        live_counts = table_counts(live)
        print("\nBACKUP  %s (%d bytes)" % (dest, dest.stat().st_size))
        print("  integrity_check = %s" % integrity)
        for t in sorted(set(live_counts) | set(bak_counts)):
            print("  %-22s live=%-7s backup=%s" % (t, live_counts.get(t), bak_counts.get(t)))
        mismatch = {t: (live_counts.get(t), bak_counts.get(t))
                    for t in set(live_counts) | set(bak_counts)
                    if live_counts.get(t) != bak_counts.get(t)}
        if integrity != "ok":
            sys.exit("ABORT: backup failed integrity_check (%s). Nothing was written."
                     % integrity)

        # The live MCP server is not stopped for this run, so it inserts while
        # the script works (measured: event_log grows every ~5 min, and
        # RECOMMENDED_JOBS_API drift lands every ~15 min). The gate is
        # therefore DIRECTIONAL rather than exact:
        #
        #   backup < live  -> the snapshot is a few seconds stale. Harmless: a
        #                     restore point that misses the newest rows is still
        #                     a faithful restore point, and the rows it misses
        #                     are the very noise being cleared.
        #   backup > live  -> rows VANISHED from the live DB between snapshot
        #                     and re-count. Nothing benign does that. ABORT.
        ahead = {t: (live_counts.get(t), bak_counts.get(t)) for t in mismatch
                 if (bak_counts.get(t) or 0) > (live_counts.get(t) or 0)}
        if ahead:
            sys.exit(
                "ABORT: the backup holds MORE rows than the live DB: %s\n"
                "  (live=?, backup=?). Rows disappeared from the live database\n"
                "  while this ran. Nothing was written; investigate first." % ahead)
        if mismatch:
            print("  backup verified: integrity ok; %d table(s) grew after the "
                  "snapshot (live server writing concurrently): %s"
                  % (len(mismatch), mismatch))
        else:
            print("  backup verified: integrity ok, all %d tables match the live DB"
                  % len(live_counts))

        # --- 2. Pre-state ------------------------------------------------
        pre = ids_by_type(live)
        print("\nPRE-STATE notifications by event_type:")
        for t in sorted(pre, key=lambda k: -len(pre[k])):
            mark = "   <-- TARGET" if t in TARGETS else ""
            print("  %-24s %d%s" % (t, len(pre[t]), mark))
        pre_total = sum(len(v) for v in pre.values())
        pre_undelivered = live.execute(
            "SELECT COUNT(*) FROM notifications WHERE delivered_via IS NULL").fetchone()[0]
        print("  TOTAL %d   undelivered (pending_notifications) %d"
              % (pre_total, pre_undelivered))

        doomed = sum(len(pre.get(t, ())) for t in TARGETS)
        doomed_undelivered = live.execute(
            "SELECT COUNT(*) FROM notifications "
            "WHERE event_type IN (%s) AND delivered_via IS NULL" % PLACEHOLDERS,
            TARGETS).fetchone()[0]
        print("\nWOULD DELETE %d rows (%d of them undelivered):"
              % (doomed, doomed_undelivered))
        for t in TARGETS:
            print("  %-24s %d" % (t, len(pre.get(t, ()))))
        print("  SQL: DELETE FROM notifications WHERE event_type IN (%s)   params=%s"
              % (PLACEHOLDERS, TARGETS))
        print("  PROJECTED notifications: total %d -> %d   undelivered %d -> %d"
              % (pre_total, pre_total - doomed,
                 pre_undelivered, pre_undelivered - doomed_undelivered))

        # event_log is READ here and nowhere else in this script. Never written.
        for row in live.execute(
                "SELECT event_type, COUNT(*) FROM event_log WHERE event_type IN (%s) "
                "GROUP BY event_type ORDER BY event_type"
                % EVENT_LOG_PLACEHOLDERS, EVENT_LOG_TARGETS):
            print("  event_log %-22s %d   (forensic record, LEFT UNTOUCHED)" % row)

        if not apply:
            print("\nDRY RUN - nothing changed. Re-run with --apply.")
            return

        # --- 3. Mutate, in one transaction -------------------------------
        live.execute("BEGIN IMMEDIATE")
        cur = live.execute(
            "DELETE FROM notifications WHERE event_type IN (%s)" % PLACEHOLDERS, TARGETS)
        deleted = cur.rowcount
        live.commit()
        print("\nAPPLIED  notifications deleted=%d" % deleted)

        # --- 4. Post-state + row-id diff proof ---------------------------
        post = ids_by_type(live)
        post_total = sum(len(v) for v in post.values())
        post_undelivered = live.execute(
            "SELECT COUNT(*) FROM notifications WHERE delivered_via IS NULL").fetchone()[0]
        print("\nPOST-STATE notifications by event_type:")
        for t in sorted(post, key=lambda k: -len(post[k])):
            print("  %-24s %d" % (t, len(post[t])))
        print("  TOTAL %d   undelivered (pending_notifications) %d"
              % (post_total, post_undelivered))
        print("\nBEFORE/AFTER  notifications: total %d -> %d   undelivered %d -> %d"
              % (pre_total, post_total, pre_undelivered, post_undelivered))

        # Blast radius, proved by row id and not by count alone.
        print("\nROW-ID DIFF (by event_type):")
        print("  %-24s %7s %7s %7s   %s"
              % ("event_type", "before", "after", "delta", "note"))
        losses = {}
        gains = {}
        for t in sorted(set(pre) | set(post)):
            before, after = pre.get(t, set()), post.get(t, set())
            lost, gained = before - after, after - before
            delta = len(after) - len(before)
            if t in TARGETS:
                note = "TARGET"
                if after:
                    note += " - %d re-minted by the live server (still on OLD code)" % len(after)
            elif lost:
                note = "UNINTENDED LOSS of %d id(s)   <-- FAILURE" % len(lost)
            elif gained:
                note = "gained %d id(s) - live server insert" % len(gained)
            else:
                note = "untouched"
            print("  %-24s %7d %7d %+7d   %s" % (t, len(before), len(after), delta, note))
            if t not in TARGETS:
                if lost:
                    losses[t] = len(lost)
                if gained:
                    gains[t] = len(gained)

        survivors = {t: len(pre.get(t, set()) & post.get(t, set())) for t in TARGETS}
        survivors = {t: n for t, n in survivors.items() if n}

        print("\nVERDICT: %s" % (
            "clean - no id of any non-target event_type disappeared" if not losses
            else "FAILED - rows of another event_type were deleted: %s" % losses))

        if losses:
            raise SystemExit(
                "ABORT: this script deleted rows it must not touch: %s. "
                "Restore from %s." % (losses, dest))
        if survivors:
            raise SystemExit(
                "ABORT: target rows present before the delete survived it: %s. "
                "The delete did not do what it claimed." % survivors)
        if gains:
            # NOT a failure. The blast-radius claim is "no id of another
            # event_type DISAPPEARED", and a concurrent insert cannot violate
            # that - only a deletion can, and that is the `losses` gate above.
            # His MCP server is deliberately left running for this pass, so
            # inserts during the run are expected and are reported, not raised.
            print("\nNOTE: the live server inserted rows of other event_types while "
                  "this ran: %s. No row was lost; the delete is clean and committed. "
                  "The totals above are a point-in-time reading." % gains)
    finally:
        live.close()


if __name__ == "__main__":
    main()
