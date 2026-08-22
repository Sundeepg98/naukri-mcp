"""Per-call state snapshot: DB table counts + local JSON state file hashes."""
import hashlib, os, sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "naukri.db")
FILES = ["applications.json", "saved_jobs.json", "reminders.json", "questions.json",
         "early_access_tracking.json", "sync_state.json", "agent_config.json",
         "healing_state.json", "debug.png"]
TABLES = ["applications", "notifications", "event_log", "reminders", "saved_jobs",
          "interview_rounds", "agent_runs", "agent_decisions", "scheduled_runs",
          "screening_questions", "auto_fix_pending", "endpoint_audit"]


def snapshot():
    s = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True, timeout=10)
        for t in TABLES:
            try:
                s["db." + t] = c.execute('select count(*) from "%s"' % t).fetchone()[0]
            except Exception:
                s["db." + t] = None
        c.close()
    except Exception as e:
        s["db.ERROR"] = str(e)
    for f in FILES:
        p = os.path.join(ROOT, f)
        try:
            with open(p, "rb") as fh:
                s["file." + f] = hashlib.sha1(fh.read()).hexdigest()[:12]
        except Exception:
            s["file." + f] = None
    return s


def diff(before, after):
    out = {}
    for k in set(before) | set(after):
        b, a = before.get(k), after.get(k)
        if b != a:
            out[k] = [b, a]
    return out
