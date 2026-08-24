# -*- coding: utf-8 -*-
"""Enumerate the LIVE tools/list and diff it against spec.CALL + spec.SKIP."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpclient import MCP
import spec

m = MCP()
tools = m.list_tools()
live = sorted(t["name"] for t in tools)
speced = set(spec.CALL) | set(spec.SKIP)

print("LIVE tools/list count : %d" % len(live))
print("spec CALL             : %d" % len(spec.CALL))
print("spec SKIP             : %d" % len(spec.SKIP))
print("spec total            : %d" % len(speced))
print()
missing = [n for n in live if n not in speced]
print("REGISTERED BUT NOT IN SPEC (%d):" % len(missing))
for n in missing:
    t = [x for x in tools if x["name"] == n][0]
    props = (t.get("inputSchema") or {}).get("properties") or {}
    req = (t.get("inputSchema") or {}).get("required") or []
    desc = (t.get("description") or "").strip().replace("\n", " ")
    print("  - %s" % n)
    print("      required=%s  params=%s" % (req, sorted(props)))
    print("      desc: %s" % desc[:400])
print()
extra = sorted(n for n in speced if n not in live)
print("IN SPEC BUT NOT REGISTERED (%d): %s" % (len(extra), extra))

json.dump({"live": live, "missing_from_spec": missing,
           "spec_only": extra,
           "tools_raw": tools},
          open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "livelist-2026-08-24.json"), "w"),
          indent=1)
