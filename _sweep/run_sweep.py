# -*- coding: utf-8 -*-
"""Call every CALL-listed tool, capturing result + attributed state delta.

Attribution matters: a background scheduler is writing notification/event rows
on its own clock, so a bare count delta manufactures false positives. We record
the actual NEW rows per call and let the analysis stage decide which are ours.
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpclient import MCP          # noqa: E402
import spec                        # noqa: E402
import state                       # noqa: E402

DB = state.DB
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
PACE = 1.5                          # seconds between calls


def rowids():
    out = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True, timeout=10)
        for t in ("notifications", "event_log"):
            r = c.execute('select coalesce(max(id),0) from "%s"' % t).fetchone()
            out[t] = r[0]
        c.close()
    except Exception as e:
        out["ERROR"] = str(e)
    return out


def new_rows(before):
    out = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        for t in ("notifications", "event_log"):
            lo = before.get(t, 0)
            rows = c.execute(
                'select * from "%s" where id > ? order by id' % t, (lo,)).fetchall()
            if rows:
                out[t] = [dict(r) for r in rows]
        c.close()
    except Exception as e:
        out["ERROR"] = str(e)
    return out


def main():
    only = set(sys.argv[1:]) or None
    m = MCP()
    names = [n for n in spec.CALL if not only or n in only]
    results = {}
    if os.path.exists(OUT) and only:
        results = json.load(open(OUT))

    print("calling %d tools" % len(names), flush=True)
    for i, name in enumerate(names, 1):
        args = spec.CALL[name]
        st_b, rid_b = state.snapshot(), rowids()
        t0 = time.time()
        res = m.call(name, args)
        st_a = state.snapshot()
        rec = {
            "args": args,
            "elapsed": res.get("elapsed"),
            "isError": res.get("isError"),
            "rpc_error": res.get("rpc_error"),
            "transport_error": res.get("transport_error"),
            "parsed": res.get("parsed"),
            "text": (res.get("text") or "")[:6000],
            "state_diff": state.diff(st_b, st_a),
            "new_rows": new_rows(rid_b),
        }
        results[name] = rec
        p = rec["parsed"]
        stat = (p.get("status") if isinstance(p, dict) else None) or (
            "RPC_ERR" if rec["rpc_error"] else ("TRANSPORT" if rec["transport_error"] else "?"))
        nn = len(rec["new_rows"].get("notifications", []))
        flag = "  <-- +%d notif" % nn if nn else ""
        print("[%3d/%d] %-34s %-9s %5.1fs%s" % (
            i, len(names), name, str(stat)[:9], time.time() - t0, flag), flush=True)
        json.dump(results, open(OUT, "w"), indent=1, default=str)
        time.sleep(PACE)

    print("done ->", OUT)


if __name__ == "__main__":
    main()
