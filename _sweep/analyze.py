# -*- coding: utf-8 -*-
"""Classify sweep results.

The account is LOGGED OUT, which is a free oracle: every account-backed tool MUST
say so. A tool that instead reports success with an empty payload cannot tell
"nothing" from "broken" - the exact defect that let seven LinkedIn tools pass.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RES = json.load(open(os.path.join(HERE, "results.json")))

AUTH_PAT = re.compile(
    r"not logged in|NotLoggedInError|no_token|login first|session expired|"
    r"unauthor|AUTH_ERROR|LOGIN_REQUIRED", re.I)

# payload keys that carry the actual answer
LIST_KEYS = ("jobs", "applications", "saved_jobs", "notifications", "alerts",
             "messages", "companies", "reminders", "rounds", "results", "items",
             "history", "topics", "recruiters", "decisions", "changes", "answers",
             "templates", "opportunities", "roles", "tasks", "entries", "data")


def payload_size(p):
    """Largest list length found in the response, plus count of scalar fields."""
    if not isinstance(p, dict):
        return None, None
    best, key = 0, None
    for k, v in p.items():
        if isinstance(v, list):
            if len(v) > best:
                best, key = len(v), k
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, list) and len(v2) > best:
                    best, key = len(v2), k + "." + k2
    return best, key


def classify(name, rec):
    p = rec.get("parsed")
    txt = (rec.get("text") or "")
    if rec.get("transport_error"):
        return "errored", "transport: " + str(rec["transport_error"])[:90]
    if rec.get("rpc_error"):
        return "errored", "rpc: " + str(rec["rpc_error"])[:90]
    if not isinstance(p, dict):
        return "errored", "non-JSON response: " + txt[:90]

    status = str(p.get("status", "")).lower()
    msg = str(p.get("message", "") or p.get("error", ""))
    code = str(p.get("error_code", ""))
    is_auth = bool(AUTH_PAT.search(msg + " " + code + " " + txt[:600]))
    n, key = payload_size(p)

    if status in ("error", "failed"):
        if is_auth:
            return "auth-blocked-honest", "%s: %s" % (code or "error", msg[:80])
        return "errored", "%s: %s" % (code or "error", msg[:90])

    # status success/partial_success from here
    if is_auth:
        return "empty-unclear", "reports success but body admits auth failure: " + msg[:70]
    if n == 0 or n is None:
        # success with no list payload - is it a scalar answer or a silent empty?
        scalars = {k: v for k, v in p.items()
                   if k not in ("status", "message", "error_code")
                   and not isinstance(v, (list, dict))}
        if scalars:
            return "success-scalar", "scalars: " + json.dumps(scalars)[:110]
        return "empty-unclear", "success with no payload at all"
    return "success-data", "%s=%d" % (key, n)


def main():
    out = {}
    for name, rec in sorted(RES.items()):
        v, ev = classify(name, rec)
        out[name] = {"verdict": v, "evidence": ev,
                     "elapsed": rec.get("elapsed"),
                     "state_diff": rec.get("state_diff"),
                     "new_notifs": [r.get("event_type") for r in
                                    rec.get("new_rows", {}).get("notifications", [])]}
    json.dump(out, open(os.path.join(HERE, "verdicts.json"), "w"), indent=1, default=str)
    counts = {}
    for v in out.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    print("=== COUNTS (of %d called) ===" % len(out))
    for k in sorted(counts, key=lambda x: -counts[x]):
        print("  %-22s %3d" % (k, counts[k]))
    if len(sys.argv) > 1:
        want = sys.argv[1]
        print("\n=== %s ===" % want)
        for n, v in sorted(out.items()):
            if v["verdict"] == want:
                print("  %-34s %s" % (n, v["evidence"]))


if __name__ == "__main__":
    main()
