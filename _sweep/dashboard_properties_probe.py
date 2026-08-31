"""Measure what the dashboard `properties` parameter actually costs and buys.

Read-only. Fires GETs at DASHBOARD_API through the SAME REST path production
takes (auth_bridge headers = API_HEADERS + bearer + cookies), and reports, for
each request shape: wire bytes, latency, and the top-level key set of
`dashBoard`.

THE INSTRUMENT, and why it works (measured 2026-08-30, re-measured here):
an UNRECOGNIZED property name is ignored and the endpoint falls back to the
FULL payload; a RECOGNIZED one narrows the response. So response SIZE answers
"did this name do anything?" independently of whether a key of that name shows
up. Both controls are fired every run and the run ABORTS if either fails --
an absence-based reading off a broken instrument is the failure mode this
guards.

Usage:
    venv\\Scripts\\python.exe _sweep\\dashboard_properties_probe.py
    venv\\Scripts\\python.exe _sweep\\dashboard_properties_probe.py --candidates

Never prints the token or the cookie string.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from naukri_server.config import (  # noqa: E402
    API_HEADERS, CHROME_PROFILE, DASHBOARD_API, DASHBOARD_PROPERTIES, NAUKRI_BASE,
)


def get_auth_headers():
    """Same headers as auth_bridge.get_auth_headers, WITHOUT its expiry gate.

    auth_bridge refuses an export within 120s of the token's `exp` so a long
    sweep does not start on a credential that dies partway through. This probe
    is short and would rather let the WIRE answer than a clock: an expired
    token comes back 401 and the run aborts loudly, which is a better failure
    than a refusal to measure. Reads only; never prints token or cookies.
    """
    state = json.loads(
        (Path(CHROME_PROFILE) / "auth_state.json").read_text(encoding="utf-8")
    )
    return {
        **API_HEADERS,
        "Authorization": f"Bearer {state['token']}",
        "cookie": state["cookies"],
    }

URL = f"{NAUKRI_BASE}{DASHBOARD_API}"

# An unrecognized name. If this does NOT come back full-sized, the instrument's
# premise is broken and every size-based reading below is void.
CTL_UNRECOGNIZED = "notARealPropertyCtl20260831"
# A known-recognized name. If this does NOT narrow, likewise.
CTL_RECOGNIZED = "lookupData"

# The four fields production reads but its own request does not carry.
BROKEN_FIELDS = (
    "unreadPowerNvite", "totalPowerNvite", "unreadMostRelevantMail", "mrt",
)

# Names to test for "does any property carry the four?". Grouped by the guess
# behind them; classification is by size, not by hope.
CANDIDATES = [
    # mail / invite family
    "nvite", "powerNvite", "nviteDetails", "inbox", "inboxDetails",
    "mail", "mails", "mailDetails", "mostRelevantMail", "recruiterMails",
    "communication", "messages", "messageDetails", "jobseekerMail",
    "unreadMail", "mailCount", "notification", "notifications",
    # preferences family (desiredRole / expectedCtc / newLocationPrefId)
    "careerProfile", "desiredProfile", "preferences", "jobPreferences",
    "desiredJobDetails", "cvInfo", "resumeDetails",
]


async def fetch(session, properties):
    """One GET. Returns (bytes, seconds, parsed_dashboard_dict_or_None)."""
    params = {"properties": properties} if properties is not None else None
    t0 = time.perf_counter()
    async with session.get(URL, params=params) as resp:
        raw = await resp.read()
        elapsed = time.perf_counter() - t0
        status = resp.status
    try:
        db = json.loads(raw).get("dashBoard")
    except Exception:
        db = None
    return len(raw), elapsed, db, status


async def repeat(session, properties, n):
    sizes, times, keysets = [], [], []
    for _ in range(n):
        size, elapsed, db, status = await fetch(session, properties)
        if status != 200 or db is None:
            raise SystemExit(f"ABORT: status={status} body-unparsed for properties={properties!r}")
        sizes.append(size)
        times.append(elapsed)
        keysets.append(frozenset(db))
        await asyncio.sleep(0.4)
    return sizes, times, keysets


def summarize(label, sizes, times):
    return (
        f"{label:<12} n={len(sizes)}  bytes={min(sizes)}..{max(sizes)}  "
        f"median_ms={statistics.median(times) * 1000:.0f}  "
        f"min_ms={min(times) * 1000:.0f}  max_ms={max(times) * 1000:.0f}"
    )


async def main(run_candidates):
    n = 5
    headers = get_auth_headers()
    async with aiohttp.ClientSession(headers=headers) as session:
        # ---- CONTROLS FIRST. Nothing below is readable if these fail. -------
        print("=== CONTROLS ===")
        bare_size, _, bare_db, _ = await fetch(session, None)
        await asyncio.sleep(0.4)
        unrec_size, _, unrec_db, _ = await fetch(session, CTL_UNRECOGNIZED)
        await asyncio.sleep(0.4)
        rec_size, _, rec_db, _ = await fetch(session, CTL_RECOGNIZED)
        await asyncio.sleep(0.4)

        print(f"bare                       -> {bare_size:>6} bytes, {len(bare_db):>2} keys")
        print(f"?properties={CTL_UNRECOGNIZED} -> {unrec_size:>6} bytes, {len(unrec_db):>2} keys")
        print(f"?properties={CTL_RECOGNIZED:<26} -> {rec_size:>6} bytes, {len(rec_db):>2} keys")

        neg_ok = frozenset(unrec_db) == frozenset(bare_db)
        pos_ok = len(rec_db) < len(bare_db)
        print(f"NEGATIVE CONTROL (unrecognized falls back to full): {'PASS' if neg_ok else 'FAIL'}")
        print(f"POSITIVE CONTROL (recognized narrows):              {'PASS' if pos_ok else 'FAIL'}")
        if not (neg_ok and pos_ok):
            raise SystemExit("ABORT: instrument premise not reproduced; no size-based reading is valid.")

        # ---- The production comparison, interleaved and repeated ------------
        print("\n=== BARE vs PRODUCTION (interleaved, n=%d each) ===" % n)
        bare_sizes, bare_times, bare_keysets = [], [], []
        prod_sizes, prod_times, prod_keysets = [], [], []
        for _ in range(n):
            s, t, db, st = await fetch(session, None)
            if st != 200:
                raise SystemExit(f"ABORT bare status={st}")
            bare_sizes.append(s); bare_times.append(t); bare_keysets.append(frozenset(db))
            await asyncio.sleep(0.4)
            s, t, db, st = await fetch(session, DASHBOARD_PROPERTIES)
            if st != 200:
                raise SystemExit(f"ABORT prod status={st}")
            prod_sizes.append(s); prod_times.append(t); prod_keysets.append(frozenset(db))
            await asyncio.sleep(0.4)

        print(summarize("bare", bare_sizes, bare_times))
        print(summarize("production", prod_sizes, prod_times))
        print(f"key-set stable across reads: bare={len(set(bare_keysets)) == 1} "
              f"production={len(set(prod_keysets)) == 1}")

        bare_keys = set().union(*bare_keysets)
        prod_keys = set().union(*prod_keysets)
        print(f"\nbare keys       = {len(bare_keys)}")
        print(f"production keys = {len(prod_keys)}")
        print(f"production - bare (LOST by dropping properties) = {sorted(prod_keys - bare_keys) or 'NONE -- bare is a strict superset'}")
        print(f"bare - production (GAINED by dropping properties) = {len(bare_keys - prod_keys)}: {sorted(bare_keys - prod_keys)}")

        print("\n--- the four fields production reads ---")
        for f in BROKEN_FIELDS:
            in_bare = f in bare_keys
            in_prod = f in prod_keys
            val = bare_db.get(f) if in_bare else None
            print(f"  {f:<24} bare={in_bare!s:<5} production={in_prod!s:<5} bare_value={val!r}")

        print("\n--- profilePerformance as a KEY (defect 2) ---")
        print(f"  'profilePerformance' in bare       : {'profilePerformance' in bare_keys}")
        print(f"  'profilePerformance' in production : {'profilePerformance' in prod_keys}")
        pp_fields = ("profileViewCount", "recruiterActionsLatestDate",
                     "totalSearchAppearancesCount", "totalSearchAppearancesLatestDate")
        for f in pp_fields:
            print(f"  flat field {f:<34} in production: {f in prod_keys}")

        if not run_candidates:
            return

        # ---- Candidate property names, classified by size -------------------
        print("\n=== CANDIDATE PROPERTY NAMES (recognized == narrows) ===")
        full = len(bare_db)
        for name in CANDIDATES:
            size, _, db, st = await fetch(session, name)
            if st != 200 or db is None:
                print(f"  {name:<20} status={st} UNREADABLE")
                await asyncio.sleep(0.5)
                continue
            keys = frozenset(db)
            recognized = keys != frozenset(bare_db)
            carries = [f for f in BROKEN_FIELDS if f in keys]
            print(f"  {name:<20} {size:>6}b {len(keys):>2}k  "
                  f"{'RECOGNIZED' if recognized else 'unrecognized(full fallback)':<28} "
                  f"carries={carries}")
            await asyncio.sleep(0.5)
        print(f"\n(full payload = {full} keys; anything reporting {full} keys was ignored)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", action="store_true",
                    help="also sweep candidate property names for the four fields")
    args = ap.parse_args()
    asyncio.run(main(args.candidates))
