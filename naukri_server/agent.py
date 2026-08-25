"""Autonomous job-hunting agent — observe→decide→act→learn loop.

Runs as a scheduled task (via Option B scheduler). Searches for jobs,
filters by fit score and blocklist, and applies based on configured mode:
  - dry_run: log decisions only, never apply
  - approval: queue jobs for human review via notifications
  - auto: apply automatically within daily budget

All building blocks (search, scoring, apply, events, DB) already exist.
This file is ~250 lines of orchestration glue.
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import uuid4

from mcp.server.fastmcp import Context

from naukri_server.config import APPLY_MIN_FIT_SCORE, DATA_DIR
from naukri_server.domain.agent import (
    AgentBlocklist, QuietHours, AgentCandidate, validate_agent_config,
)

logger = logging.getLogger(__name__)

# IST offset (UTC+5:30) — matches scheduler.py
IST = timezone(timedelta(hours=5, minutes=30))

#: How many results one agent search puts in front of the selector, and the
#: Python ceiling on it. The ceiling is here rather than only in jobcore's
#: KeySpec because jobcore enforces floor/ceiling on the WRITE path; a
#: hand-edited file reaches `_decide` unbounded. Mirrors
#: HARD_LIMITS["agent_per_search_limit_ceiling"].
_PER_SEARCH_LIMIT_DEFAULT = 20
_PER_SEARCH_LIMIT_CEILING = 100

# Default config — used when agent_config.json doesn't exist
DEFAULT_CONFIG = {
    "enabled": False,  # Must be explicitly enabled
    "mode": "approval",  # dry_run | approval | auto
    "max_daily_applications": 15,
    "min_fit_score": APPLY_MIN_FIT_SCORE,
    # Read by `_decide` as the per-search page size. Present here so the
    # config readout shows every key the agent actually uses -- it was absent
    # while `_decide` hardcoded 20, which is how it stayed a decoy unnoticed.
    "per_search_limit": _PER_SEARCH_LIMIT_DEFAULT,
    "quiet_hours": {
        "enabled": True,
        "start_hour": 20,  # 8 PM IST
        "end_hour": 8,     # 8 AM IST
    },
    "searches": [
        {
            "name": "Node.js Bangalore",
            "keywords": "Node.js developer",
            "location": "Bangalore",
            "freshness": 7,
            "min_fit_score": APPLY_MIN_FIT_SCORE,
            "enabled": True,
        },
    ],
    "blocklist": {
        "companies": [],
        "title_keywords": [],
        "enabled": True,
    },
    # "reminder_days": 7 was DELETED (2026-08-21). It shipped in DEFAULT_CONFIG,
    # in both agent_config.json files and in the update allowlist, and NOTHING
    # read it -- the reminder feature is driven entirely by the caller-supplied
    # `set_reminder_days` argument on the apply tools. A knob that validates,
    # persists and changes nothing is worse than a missing feature, because it
    # reads as a working one.
}

CONFIG_PATH = DATA_DIR / "agent_config.json"

#: Where the last FULL policy fingerprint this agent acted under is remembered
#: -- scoring and candidate, not the scoring half alone.
#: One line of state, and it is what turns "policy was quietly widened" into
#: "he sees the list" — see :func:`_effective_mode`.
POLICY_STATE_PATH = DATA_DIR / "agent_policy_state.json"


# ---------------------------------------------------------------------------
# The guards no config write can reach
# ---------------------------------------------------------------------------
#
# THE OLD INVARIANT, RETIRED 2026-08-25: *no sequence of config writes, from
# any server, may grant autonomous apply authority.* The config file CAN now
# arm this agent -- six keys under `servers.naukri.agent` are loadable (see
# `load_agent_config`). Do not re-derive the old sentence from this file's
# shape; it was overruled deliberately, and only the CONCLUSION was overruled.
#
# The traced escalation ran through this module and is still real. `min_fit_score`
# is not a display filter here -- `_decide` enqueues every job at or above it
# with apply_status "pending", and `_act` in "auto" mode then submits them.
# Five writes (enabled -> auto -> min_fit_score 0 -> blocklist off -> arbitrary
# searches) took the system from "disabled, dry_run, threshold 70" to fifteen
# real applications a day at no threshold on his live account.
#
# What changed is WHERE that escalation is stopped. It used to be stopped at
# the config layer, by tier C. It is now stopped HERE, in Python, by four
# guards that no config write of any kind can reach:
#
#   1. MIN_AGENT_FIT_FLOOR — a Python constant the file cannot reach. A bad
#      threshold, from ANY source (this file, agent_config.json, or
#      naukri_agent_update_config), costs display noise instead of
#      applications. Applied in `_decide` as `max(configured, floor)`, and
#      applied AGAIN per search, because a per-search override is the selector
#      one level down and lists never reach jobcore's schema at all.
#
#   2. A forced approval cycle whenever the FULL POLICY fingerprint changed
#      since the last cycle. This covers the two levers that cannot be tier C
#      because they are the feature he asked for by name: inflating
#      `candidate.skills` (|matched|/|job_skills| -> 100 for every job in
#      existence) and reshaping `scoring`. Neither touches the agent block at
#      all, and either can be written FROM A SIBLING SERVER, so no amount of
#      section-scoping catches them. One condition does. That condition
#      compares `policy_hash`, the FULL {scoring, candidate} fingerprint --
#      NOT `scoring_hash`, which covers `scoring` alone and would miss the
#      candidate.skills lever.
#
#      READ THIS BEFORE RELYING ON GUARD 2 FOR THE AGENT BLOCK: `policy_hash`
#      covers {scoring, candidate} and NOT `servers.*`. Writing `mode: "auto"`
#      into the config file therefore does NOT by itself force an approval
#      cycle. Guard 2 was built for the two levers above and still catches
#      them; it is not, and never was, a guard on the agent block. A
#      file-armed agent is bounded by guards 1, 3 and 4 -- plus tier B's
#      confirm_widen on the write path and `validate_agent_config` on load.
#
#   3. The kill switch -- re-checked inside the auto-apply loop on every
#      iteration, so a trip mid-loop halts the rest of the batch.
#
#   4. The daily quota -- `_decide` stops adding candidates at
#      `daily_remaining`, which is upstream of `_act`, and
#      `validate_agent_config` bounds `max_daily_applications` to 1-100
#      whatever the source. `max_daily_applications` is NOT one of the six:
#      the shared file cannot set it (see FILE_DECIDABLE_KEYS).


def _min_agent_fit_floor() -> int:
    """The floor below which no candidate is ever enqueued.

    Read through a function so the import cannot make this module depend on
    jobcore at import time; falls back to the same literal, because a safety
    floor that disappears when an import fails is not a floor.
    """
    try:
        from jobcore.config import MIN_AGENT_FIT_FLOOR

        return int(MIN_AGENT_FIT_FLOOR)
    except Exception:  # pragma: no cover - jobcore is a hard dependency today
        return 60


def _read_policy_state() -> dict:
    try:
        with open(POLICY_STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_policy_state(state: dict) -> None:
    try:
        POLICY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = POLICY_STATE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, POLICY_STATE_PATH)
    except OSError as exc:
        logger.warning("could not persist agent policy state: %s", exc,
                       extra={"step": "config"})


def current_policy_hash() -> Optional[str]:
    """The FULL policy fingerprint -- scoring AND candidate -- or None.

    DO NOT "fix" this to `scoring_hash`. The two are different hashes:
    `policy_hash` covers {scoring, candidate}, `scoring_hash` covers {scoring}
    only. This gate exists to catch an inflated `candidate.skills`, which
    changes no arithmetic and so does NOT move the scoring hash -- it moves
    every score by widening what counts as matched. Narrowing this to the
    scoring hash reopens that hole, silently.
    """
    try:
        from naukri_server import policy as _policy

        return _policy.snapshot().policy_hash
    except Exception:  # pragma: no cover - the loader already swallows
        return None


def _effective_mode(config: dict, cycle_id: str = "") -> tuple[str, Optional[str]]:
    """The mode this cycle actually runs in, and why if it was downgraded.

    "auto" is downgraded to "approval" for exactly one cycle after the full
    policy fingerprint moves. Nothing else is affected: dry_run stays dry_run,
    an unchanged fingerprint runs in the configured mode.
    """
    mode = config.get("mode", "approval")
    if mode != "auto":
        return mode, None

    current = current_policy_hash()
    if current is None:
        return mode, None

    # `last_policy_hash` was called `last_scoring_hash` until the two hashes
    # were told apart. Fall back to the old key so an existing state file on a
    # live box is not read as "never seen", which would burn a spurious
    # approval cycle on the next run.
    _state = _read_policy_state()
    last_seen = _state.get("last_policy_hash", _state.get("last_scoring_hash"))
    try:
        from jobcore.policy import requires_approval_cycle
    except Exception:  # pragma: no cover
        return mode, None

    if not requires_approval_cycle(current, last_seen):
        return mode, None

    # "policy", not "scoring policy": the hash compared here covers the
    # candidate block as well, so an inflated candidate.skills fires this with
    # no scoring key touched at all. Saying "scoring" sent the reader looking
    # in the wrong half of the file for a change that was never there.
    reason = (
        f"policy changed since the last cycle -- scoring, candidate or both "
        f"({last_seen or 'never seen'} -> {current}); this cycle runs in "
        f"approval mode so the list is reviewed before anything is submitted"
    )
    logger.warning("Agent %s: auto -> approval — %s", cycle_id, reason,
                   extra={"cycle_id": cycle_id, "step": "act"})
    return "approval", reason


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------

#: The six keys the shared policy file (`config/jobhunt.json`) may decide,
#: dotted relative to the agent block. THE ONLY SIX. `max_daily_applications`,
#: `cycle_interval_hours` and `quiet_hours.*` are deliberately absent: they are
#: tier B in jobcore's schema and loadable into the jobcore Policy, but this
#: agent does not take them from the shared file. The daily quota in particular
#: is guard 4 above, and a guard whose value the same file can raise is worth
#: less than one it cannot.
FILE_DECIDABLE_KEYS = (
    "enabled",
    "mode",
    "min_fit_score",
    "searches",
    "per_search_limit",
    "blocklist.enabled",
)

_MISSING = object()


def _dig(node, dotted: str):
    """The value at a dotted path inside nested mappings, or ``_MISSING``."""
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _same_value(a, b) -> bool:
    """Equality that does not care about list-vs-tuple or bool-vs-int."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return list(a) == list(b)
    return a == b


def agent_config_overlay() -> dict:
    """What the shared policy file is currently DECIDING, as dotted keys.

    Empty when there is no config file, when the file cannot be read, or when
    every one of the six still carries the value jobcore ships. That last
    clause is the load-bearing one and deserves its reason:

    jobcore hands back a MERGED block -- schema defaults with the file's values
    laid over them -- so "present in the returned dict" cannot tell a declared
    key from a default. If this function treated every returned key as
    declared, the shipped `config/jobhunt.json` (which contains exactly the six
    schema defaults) would permanently override whatever the operator had set
    through `naukri_agent_update_config`, resetting a deliberately-armed agent
    to `enabled: false, mode: dry_run` on every load. So a value EQUAL to the
    shipped default is read as "the file has no opinion here".

    The cost of that rule, stated plainly rather than discovered later: the
    file cannot use a shipped-default VALUE to override a non-default value in
    `agent_config.json`. Writing `mode: "dry_run"` into the file does not undo
    a tool-set `mode: "auto"`, because "dry_run" is the shipped default and is
    therefore indistinguishable from silence. Use `naukri_agent_update_config`
    for that; it is the surface that owns `agent_config.json`.

    Never raises. A config problem must cost the agent its overlay, not its
    ability to run.
    """
    try:
        from jobcore.policy import schema_defaults

        from naukri_server import policy as _policy

        loaded = _policy.snapshot().policy.server("naukri").get("agent", {})
        shipped = schema_defaults("servers").get("naukri", {}).get("agent", {})
    except Exception as exc:  # pragma: no cover - the loader already swallows
        logger.warning("agent config overlay unavailable (%s); "
                       "agent_config.json decides alone", exc,
                       extra={"step": "config"})
        return {}

    overlay: dict = {}
    for dotted in FILE_DECIDABLE_KEYS:
        value = _dig(loaded, dotted)
        if value is _MISSING:
            continue
        # An EMPTY searches list means "not specified", never "search for
        # nothing". The shipped file ships `searches: []`, and
        # `validate_agent_config` requires a NON-EMPTY list, so a naive merge
        # would fail validation on the very first load and take the agent down.
        # Stated as its own branch rather than left to the default-compare
        # below, so it survives a future change to the shipped default.
        if dotted == "searches" and not value:
            continue
        if _same_value(value, _dig(shipped, dotted)):
            continue
        overlay[dotted] = value
    return overlay


def _apply_overlay(config: dict, overlay: dict) -> dict:
    """A deep copy of *config* with each dotted key in *overlay* set."""
    out = json.loads(json.dumps(config, default=str))
    for dotted, value in overlay.items():
        parts = dotted.split(".")
        node = out
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
    return out


def load_agent_config() -> dict:
    """The effective agent config: Python defaults, agent_config.json, then file.

    PRECEDENCE, and why it is this way round::

        DEFAULT_CONFIG  <  agent_config.json  <  config/jobhunt.json (the six)

    THE SHARED FILE WINS for the six keys in :data:`FILE_DECIDABLE_KEYS`.
    The alternative -- letting `agent_config.json` win -- was measured and
    rejected: `naukri_agent_update_config` saves the FULL merged config, so
    `agent_config.json` on any live box already contains all six keys. Under
    that precedence the file layer would be shadowed on every load and
    "loadable" would be a word with no behaviour behind it.

    The file also earns it. It is the reviewed surface: version-controlled,
    revision-stamped, ledgered, tier-checked, with an external-edit detector.
    `agent_config.json` is unversioned runtime state in DATA_DIR.

    The cost is a real one and is made LOUD rather than silent:
    `naukri_agent_update_config` reports a warning naming any key its patch
    wrote that the file then overrode, so a tool write that cannot take effect
    says so instead of appearing to succeed.

    Every file-sourced value is routed through `validate_agent_config`, so the
    mode enum, the 0-100 fit bound and the non-empty-searches rule apply to
    file input exactly as they apply to tool input. If the overlay introduces
    an error the base did not have, THE WHOLE OVERLAY IS DROPPED and the base
    runs: half-applying a policy is worse than ignoring one, which is the same
    call jobcore's loader makes on a malformed document.

    Sync — for use from sync contexts. Async callers should use
    `await asyncio.to_thread(load_agent_config)` to avoid blocking the event loop.
    """
    base = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                stored = json.load(f)
            # Merge with defaults for any missing keys
            base = {**DEFAULT_CONFIG, **stored}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load agent config: %s, using defaults", e,
                           extra={"step": "config"})

    overlay = agent_config_overlay()
    if not overlay:
        return base

    candidate = _apply_overlay(base, overlay)
    base_errors = validate_agent_config(base)
    new_errors = [e for e in validate_agent_config(candidate)
                  if e not in base_errors]
    if new_errors:
        logger.error(
            "agent config from the shared policy file is invalid, so the whole "
            "overlay was DROPPED and agent_config.json is running unchanged. "
            "Keys offered: %s. Errors: %s%s",
            ", ".join(sorted(overlay)), "; ".join(new_errors),
            " (agent_config.json was already invalid too)" if base_errors else "",
            extra={"step": "config"},
        )
        return base

    logger.info("agent config: %s supplied by the shared policy file",
                ", ".join(f"{k}={overlay[k]!r}" for k in sorted(overlay)),
                extra={"step": "config"})
    return candidate


def save_agent_config(config: dict):
    """Save agent config to file.

    Sync — async callers should use `await asyncio.to_thread(save_agent_config, config)`.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def config_hash(config: dict) -> str:
    """Short hash of config for change detection."""
    s = json.dumps(config, sort_keys=True)
    return hashlib.sha256(s.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Saga steps: observe → decide → act → learn
# ---------------------------------------------------------------------------

async def _observe(cycle_id: str, config: dict) -> dict:
    """Step 1: Gather context — applied IDs, daily count, config."""
    from naukri_server.database import get_applied_job_ids, count_daily_applied

    applied_ids = await get_applied_job_ids()
    today = datetime.now(IST).strftime("%Y-%m-%d")
    daily_applied = await count_daily_applied(today)
    daily_remaining = max(0, config["max_daily_applications"] - daily_applied)

    logger.info("Agent %s: observe — %d applied IDs, %d daily applied, %d remaining",
                cycle_id, len(applied_ids), daily_applied, daily_remaining,
                extra={"cycle_id": cycle_id, "step": "observe"})

    try:
        from naukri_server.events import event_bus, AgentObserveCompleted
        await event_bus.emit(AgentObserveCompleted(
            cycle_id=cycle_id, applied_ids_count=len(applied_ids),
            daily_remaining=daily_remaining,
        ))
    except Exception as e:
        logger.warning("Failed to emit AgentObserveCompleted: %s", e,
                       extra={"cycle_id": cycle_id, "step": "observe"})

    return {
        "cycle_id": cycle_id,
        "config": config,
        "applied_ids": applied_ids,
        "daily_applied": daily_applied,
        "daily_remaining": daily_remaining,
    }


async def _decide(observe_result: dict) -> dict:
    """Step 2: Search jobs, filter by score/blocklist/dedup, cap to budget."""
    from naukri_server.tools.auto_hunt import naukri_auto_hunt
    from naukri_server.database import insert_agent_decision
    from naukri_server.events import event_bus, AgentJobSkipped

    config = observe_result["config"]
    applied_ids = observe_result["applied_ids"]
    cycle_id = observe_result["cycle_id"]
    daily_remaining = observe_result["daily_remaining"]
    # The floor the config file cannot reach. `max()`, not `or`: tightening the
    # threshold is always honoured, loosening it past the floor never is.
    floor = _min_agent_fit_floor()
    configured_min_fit = config.get("min_fit_score", APPLY_MIN_FIT_SCORE)
    try:
        configured_min_fit = int(configured_min_fit)
    except (TypeError, ValueError):
        configured_min_fit = APPLY_MIN_FIT_SCORE
    min_fit = max(configured_min_fit, floor)
    if min_fit != configured_min_fit:
        logger.warning(
            "Agent %s: min_fit_score %s raised to the Python floor %d — a "
            "config value below the floor costs display noise, never applications",
            cycle_id, configured_min_fit, floor,
            extra={"cycle_id": cycle_id, "step": "decide"},
        )

    # `per_search_limit` was a DECOY until 2026-08-25: it shipped in the
    # schema, in agent_config.json and in the docs, and this call passed a
    # hardcoded `limit=20`. It is read here now, which is also what lets it
    # leave tier C at all -- jobcore's KeySpec refuses to construct a loadable
    # spec with no declared reader, so "loadable" and "read by something" are
    # the same statement in this system.
    #
    # Clamped in Python because jobcore enforces floor/ceiling on the WRITE
    # path only; a hand-edited file reaches this with any integer at all.
    # The bound costs nothing real: `_decide` stops adding candidates at
    # `daily_remaining` (<= 100), so a larger page only wastes scoring.
    per_search_limit = _PER_SEARCH_LIMIT_DEFAULT
    try:
        per_search_limit = int(config.get("per_search_limit",
                                          _PER_SEARCH_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        pass
    clamped = max(1, min(per_search_limit, _PER_SEARCH_LIMIT_CEILING))
    if clamped != per_search_limit:
        logger.warning(
            "Agent %s: per_search_limit %s clamped to %d",
            cycle_id, per_search_limit, clamped,
            extra={"cycle_id": cycle_id, "step": "decide"},
        )
    per_search_limit = clamped

    blocklist_obj = AgentBlocklist.from_config(config)

    candidates: list[AgentCandidate] = []
    total_found = 0
    total_matched = 0
    searches_run = 0

    for search in config.get("searches", []):
        if not search.get("enabled", True):
            continue
        searches_run += 1

        # The per-search override is the selector again one level down, so the
        # floor applies to it too — otherwise `searches[0].min_fit_score: 0`
        # walks straight past the guard above.
        try:
            search_min_fit = max(int(search.get("min_fit_score", min_fit)), floor)
        except (TypeError, ValueError):
            search_min_fit = min_fit

        result = await naukri_auto_hunt(
            keywords=search["keywords"],
            location=search.get("location"),
            min_fit_score=search_min_fit,
            limit=per_search_limit,
            freshness=search.get("freshness", 7),
            work_mode=search.get("work_mode"),
        )

        if result.get("status") != "success":
            logger.warning("Auto hunt failed for '%s': %s", search["name"], result.get("message"),
                           extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
            continue

        total_found += result.get("jobs_found", 0)
        total_matched += result.get("jobs_matched", 0)
        logger.info("Agent %s: search '%s' — found %d, matched %d",
                    cycle_id, search["name"], result.get("jobs_found", 0), result.get("jobs_matched", 0),
                    extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})

        for job in result.get("ranked_jobs", []):
            job_id = str(job.get("job_id", ""))

            # Dedup
            if job_id in applied_ids or any(c.job_id == job_id for c in candidates):
                await insert_agent_decision(
                    cycle_id, job_id, "skip",
                    company=job.get("company", ""), title=job.get("title", ""),
                    search_query=search["name"], fit_score=job.get("fit_score", 0),
                    skip_reason="already_applied",
                )
                logger.info("Agent %s: skip %s at %s — %s (fit: %d)",
                            cycle_id, job_id, job.get("company", ""), "already_applied", job.get("fit_score", 0),
                            extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
                try:
                    await event_bus.emit(AgentJobSkipped(
                        cycle_id=cycle_id, job_id=job_id,
                        company=job.get("company", ""), reason="already_applied",
                        fit_score=job.get("fit_score", 0),
                    ))
                except Exception as e:
                    logger.warning("Failed to emit AgentJobSkipped: %s", e,
                                   extra={"cycle_id": cycle_id, "step": "decide"})
                continue

            # Blocklist
            block_reason = blocklist_obj.check(job.get("company", ""), job.get("title", ""))
            if block_reason:
                await insert_agent_decision(
                    cycle_id, job_id, "skip",
                    company=job.get("company", ""), title=job.get("title", ""),
                    search_query=search["name"], fit_score=job.get("fit_score", 0),
                    skip_reason=block_reason,
                )
                logger.info("Agent %s: skip %s at %s — %s (fit: %d)",
                            cycle_id, job_id, job.get("company", ""), block_reason, job.get("fit_score", 0),
                            extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
                try:
                    await event_bus.emit(AgentJobSkipped(
                        cycle_id=cycle_id, job_id=job_id,
                        company=job.get("company", ""), reason=block_reason,
                        fit_score=job.get("fit_score", 0),
                    ))
                except Exception as e:
                    logger.warning("Failed to emit %s: %s", "AgentJobSkipped", e,
                                   extra={"cycle_id": cycle_id, "step": "decide"})
                continue

            # Daily cap
            if len(candidates) >= daily_remaining:
                await insert_agent_decision(
                    cycle_id, job_id, "skip",
                    company=job.get("company", ""), title=job.get("title", ""),
                    search_query=search["name"], fit_score=job.get("fit_score", 0),
                    skip_reason="daily_cap",
                )
                logger.info("Agent %s: skip %s at %s — %s (fit: %d)",
                            cycle_id, job_id, job.get("company", ""), "daily_cap", job.get("fit_score", 0),
                            extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
                try:
                    await event_bus.emit(AgentJobSkipped(
                        cycle_id=cycle_id, job_id=job_id,
                        company=job.get("company", ""), reason="daily_cap",
                        fit_score=job.get("fit_score", 0),
                    ))
                except Exception as e:
                    logger.warning("Failed to emit AgentJobSkipped: %s", e,
                                   extra={"cycle_id": cycle_id, "step": "decide"})
                continue

            # THE FLOOR, enforced where the enqueue actually happens.
            # Filtering at the search call is the fast path; this is the one
            # that holds when the search returns something below it anyway —
            # a stale cache, a scorer change, a future refactor of auto_hunt.
            # A guard that only runs on the happy path is not a guard.
            job_fit = job.get("fit_score", 0) or 0
            if job_fit < floor:
                await insert_agent_decision(
                    cycle_id, job_id, "skip",
                    company=job.get("company", ""), title=job.get("title", ""),
                    search_query=search["name"], fit_score=job_fit,
                    skip_reason="below_min_agent_fit_floor",
                )
                logger.warning(
                    "Agent %s: skip %s at %s — below the Python fit floor "
                    "(fit: %s < %d)",
                    cycle_id, job_id, job.get("company", ""), job_fit, floor,
                    extra={"cycle_id": cycle_id, "step": "decide",
                           "search": search["name"]},
                )
                try:
                    await event_bus.emit(AgentJobSkipped(
                        cycle_id=cycle_id, job_id=job_id,
                        company=job.get("company", ""),
                        reason="below_min_agent_fit_floor", fit_score=job_fit,
                    ))
                except Exception as e:
                    logger.warning("Failed to emit AgentJobSkipped: %s", e,
                                   extra={"cycle_id": cycle_id, "step": "decide"})
                continue

            # Accept candidate
            candidate = AgentCandidate.from_hunt_result(job, search["name"])
            logger.info("Agent %s: candidate %s at %s (fit: %d)",
                        cycle_id, candidate.job_id, candidate.company, candidate.fit_score,
                        extra={"cycle_id": cycle_id, "step": "decide", "search": search["name"]})
            candidates.append(candidate)
            await insert_agent_decision(
                cycle_id, candidate.job_id, "apply",
                company=candidate.company, title=candidate.title,
                search_query=candidate.search_name, fit_score=candidate.fit_score,
                apply_status="pending",
            )

    candidates.sort(key=lambda c: c.fit_score, reverse=True)
    logger.info("Agent %s: decide — %d candidates from %d searches, %d total found, %d matched",
                cycle_id, len(candidates), searches_run, total_found, total_matched,
                extra={"cycle_id": cycle_id, "step": "decide"})

    try:
        from naukri_server.events import AgentDecideCompleted
        await event_bus.emit(AgentDecideCompleted(
            cycle_id=cycle_id, searches_run=searches_run,
            total_found=total_found, total_matched=total_matched,
            candidates_count=len(candidates),
        ))
    except Exception as e:
        logger.warning("Failed to emit AgentDecideCompleted: %s", e,
                       extra={"cycle_id": cycle_id, "step": "decide"})

    return {
        **observe_result,
        "candidates": candidates,
        "searches_run": searches_run,
        "total_found": total_found,
        "total_matched": total_matched,
    }


async def _act(decide_result: dict) -> dict:
    """Step 3: Apply based on mode — dry_run, approval, or auto."""
    from naukri_server.database import update_agent_decision, store_notification
    from naukri_server.events import event_bus, AgentJobApplied

    config = decide_result["config"]
    candidates: list[AgentCandidate] = decide_result["candidates"]
    cycle_id = decide_result["cycle_id"]

    # An "auto" cycle is downgraded to "approval" for exactly one cycle after
    # the FULL policy fingerprint moves — the guard against the two levers that
    # reach this selector without touching the agent block at all, and that a
    # SIBLING SERVER can pull (candidate.skills, scoring.*).
    mode, downgrade_reason = _effective_mode(config, cycle_id)
    policy_hash = current_policy_hash()

    logger.info("Agent %s: act — mode=%s, %d candidates", cycle_id, mode, len(candidates),
                extra={"cycle_id": cycle_id, "step": "act"})

    act_result: dict

    if not candidates:
        act_result = {**decide_result, "applied": 0, "pending_approval": 0, "mode": mode, "note": "No candidates to act on"}
    elif mode == "dry_run":
        logger.info("Agent %s: dry_run — %d candidates logged, none applied", cycle_id, len(candidates),
                    extra={"cycle_id": cycle_id, "step": "act"})
        act_result = {
            **decide_result,
            "applied": 0,
            "pending_approval": 0,
            "mode": "dry_run",
            "note": f"Dry run — {len(candidates)} candidates logged, none applied",
        }
    elif mode == "approval":
        # Queue notification for user review
        job_list = "\n".join(
            f"- {c.title} at {c.company} (fit: {c.fit_score})"
            for c in candidates[:10]
        )
        try:
            await store_notification({
                "event_type": "AgentApprovalNeeded",
                "title": f"Agent found {len(candidates)} jobs to apply",
                "body": job_list,
                "priority": "high",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "metadata": json.dumps({
                    "cycle_id": cycle_id,
                    "job_ids": [c.job_id for c in candidates],
                }),
            })
            logger.info("Agent %s: approval notification stored for %d jobs", cycle_id, len(candidates),
                        extra={"cycle_id": cycle_id, "step": "act"})
        except Exception as e:
            logger.error("Agent %s: CRITICAL — failed to store approval notification: %s", cycle_id, e,
                         extra={"cycle_id": cycle_id, "step": "act"})
            act_result = {
                **decide_result,
                "applied": 0,
                "pending_approval": 0,
                "mode": "approval",
                "error": f"Notification storage failed: {e}",
            }
            # Emit act completed even on error
            try:
                from naukri_server.events import AgentActCompleted
                await event_bus.emit(AgentActCompleted(
                    cycle_id=cycle_id, mode=mode,
                    applied_count=0, pending_approval=0,
                ))
            except Exception as emit_err:
                logger.warning("Failed to emit AgentActCompleted: %s", emit_err,
                               extra={"cycle_id": cycle_id, "step": "act"})
            return act_result
        act_result = {
            **decide_result,
            "applied": 0,
            "pending_approval": len(candidates),
            "mode": "approval",
        }
    else:
        # mode == "auto"
        from naukri_server.tools.apply import _apply_single
        from naukri_server import kill_switch
        applied = 0
        errors = []
        halted = False
        for candidate in candidates:
            # FAIL-CLOSED: stop the auto-apply loop the moment the kill-switch
            # trips (e.g. a prior apply in this loop hit a block and tripped it).
            if kill_switch.is_tripped():
                halted = True
                logger.warning("Agent %s: auto-apply HALTED by kill-switch after %d applies",
                               cycle_id, applied, extra={"cycle_id": cycle_id, "step": "act"})
                break
            logger.info("Agent %s: auto-applying to %s at %s (fit: %d)",
                        cycle_id, candidate.job_id, candidate.company, candidate.fit_score,
                        extra={"cycle_id": cycle_id, "step": "act"})
            try:
                result = await _apply_single(
                    job_id=candidate.job_id,
                    title=candidate.title,
                    company=candidate.company,
                    tracking_extra={
                        "source": "agent",
                        "cycle_id": cycle_id,
                        "fit_score": candidate.fit_score,
                    },
                )
                status = result.get("status", "error")
                await update_agent_decision(cycle_id, candidate.job_id, status)
                if status == "applied":
                    applied += 1
                    try:
                        await event_bus.emit(AgentJobApplied(
                            cycle_id=cycle_id, job_id=candidate.job_id,
                            company=candidate.company, title=candidate.title,
                            fit_score=candidate.fit_score,
                        ))
                    except Exception as e:
                        logger.warning("Failed to emit %s: %s", "AgentJobApplied", e,
                                       extra={"cycle_id": cycle_id, "step": "act"})
                elif status == "halted":
                    # This apply hit a block and tripped the kill-switch — stop
                    # the loop now rather than firing more traffic into a block.
                    halted = True
                    logger.warning("Agent %s: auto-apply HALTED by kill-switch (apply returned halted)",
                                   cycle_id, extra={"cycle_id": cycle_id, "step": "act"})
                    break
            except Exception as e:
                errors.append(f"{candidate.job_id}: {e}")
                await update_agent_decision(cycle_id, candidate.job_id, "error")

        act_result = {
            **decide_result,
            "applied": applied,
            "pending_approval": 0,
            "failed": len(errors),
            "errors": errors[:5],
            "mode": "auto",
        }
        if halted:
            act_result["halted"] = True
            act_result["note"] = "Auto-apply halted by kill-switch (block detected)"

    # Record what this cycle actually ran under, so the NEXT cycle can tell
    # whether the policy moved. Written after acting, so a crashed cycle does
    # not consume the one approval cycle a policy change is owed.
    if policy_hash is not None:
        # `policy_hash`, not `scoring_hash`. This key carried the FULL
        # {scoring, candidate} fingerprint under the name of the scoring-only
        # one -- the same defect jobcore fixed, reproduced one layer up. It is
        # a transient tool-result field (nothing persists it; grep
        # database.py), so the rename costs no stored data. It is NOT
        # double-emitted under both names for a deprecation window: two names
        # for one value, where those names mean different things everywhere
        # else, is the ambiguity rather than a migration away from it.
        act_result["policy_hash"] = policy_hash
        state = _read_policy_state()
        state["last_policy_hash"] = policy_hash
        state["last_seen_at"] = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(_write_policy_state, state)
    if downgrade_reason:
        act_result["mode_downgraded_from"] = config.get("mode")
        act_result["mode_downgrade_reason"] = downgrade_reason

    try:
        from naukri_server.events import AgentActCompleted
        await event_bus.emit(AgentActCompleted(
            cycle_id=cycle_id, mode=mode,
            applied_count=act_result.get("applied", 0),
            pending_approval=act_result.get("pending_approval", 0),
        ))
    except Exception as e:
        logger.warning("Failed to emit AgentActCompleted: %s", e,
                       extra={"cycle_id": cycle_id, "step": "act"})

    return act_result


async def _learn(act_result: dict) -> dict:
    """Step 4: Log cycle results to DB and emit completion event."""
    from naukri_server.database import update_agent_run
    from naukri_server.events import event_bus, AgentCycleCompleted

    cycle_id = act_result["cycle_id"]
    applied = act_result.get("applied", 0)
    skipped = len(act_result.get("candidates", [])) - applied
    duration_ms = act_result.get("_duration_ms", 0)

    logger.info("Agent %s: learn — applied=%d, skipped=%d, duration=%.0fms",
                cycle_id, applied, skipped, duration_ms,
                extra={"cycle_id": cycle_id, "step": "learn"})

    await update_agent_run(
        cycle_id,
        finished_at=datetime.now(timezone.utc).isoformat(),
        status="completed",
        searches_run=act_result.get("searches_run", 0),
        jobs_found=act_result.get("total_found", 0),
        jobs_matched=act_result.get("total_matched", 0),
        applied_count=applied,
        skipped_count=skipped,
        duration_ms=duration_ms,
    )

    try:
        await event_bus.emit(AgentCycleCompleted(
            cycle_id=cycle_id, applied_count=applied,
            skipped_count=skipped, duration_ms=duration_ms,
            status="completed",
        ))
    except Exception as e:
        logger.warning("Failed to emit %s: %s", "AgentCycleCompleted", e,
                       extra={"cycle_id": cycle_id, "step": "learn"})

    return {
        "cycle_id": cycle_id,
        "applied": applied,
        "skipped": skipped,
        "mode": act_result.get("mode", "unknown"),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

class _DailyLimitReached(Exception):
    """Raised by observe step when daily application limit is exhausted."""
    def __init__(self, daily_applied: int):
        self.daily_applied = daily_applied
        super().__init__(f"Daily application limit reached ({daily_applied} applied)")


async def run_agent_cycle(ctx: Context | None = None) -> dict:
    """Execute one observe->decide->act->learn cycle using SagaExecutor.

    Each step is a saga step with per-step timeout, timing, and error aggregation.
    On failure, completed steps are reported with their individual timings.

    Called by the scheduler task or manually via naukri_agent(action="run_now").
    """
    # The agent runs from the scheduler, not through handle_tool_action, so it
    # binds its own snapshot. One cycle, one policy: every candidate in a cycle
    # is scored under the same weights, and `_effective_mode` reads the full
    # policy fingerprint of that same snapshot -- the scoring half plus the
    # candidate half, because both move scores.
    try:
        from naukri_server import policy as _policy

        binder = _policy.bind()
    except Exception:  # pragma: no cover
        from contextlib import nullcontext

        binder = nullcontext()

    with binder:
        return await _run_agent_cycle_bound(ctx)


async def _run_agent_cycle_bound(ctx: Context | None = None) -> dict:
    from naukri_server.database import insert_agent_run, update_agent_run
    from naukri_server.events import event_bus, AgentCycleStarted
    from naukri_server.sagas import SagaExecutor

    config = await asyncio.to_thread(load_agent_config)

    # Validate config
    errors = validate_agent_config(config)
    if errors:
        logger.error("Invalid agent config: %s", "; ".join(errors),
                     extra={"step": "config"})
        return {"status": "error", "message": "Config validation failed", "errors": errors}

    # Guard: not enabled
    if not config.get("enabled"):
        return {"status": "skipped", "reason": "Agent is disabled in config"}

    # Guard: quiet hours
    quiet = QuietHours.from_config(config)
    if quiet.is_active:
        return {"status": "skipped", "reason": "Quiet hours active"}

    cycle_id = f"cycle_{uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc).isoformat()

    # Insert run record
    await insert_agent_run({
        "cycle_id": cycle_id,
        "started_at": started_at,
        "mode": config.get("mode", "approval"),
        "metadata": json.dumps({"config_hash": config_hash(config)}),
    })

    enabled_searches = [s for s in config.get("searches", []) if s.get("enabled", True)]

    logger.info("Agent cycle %s starting — mode=%s, %d searches, config_hash=%s",
                cycle_id, config.get("mode"), len(enabled_searches), config_hash(config),
                extra={"cycle_id": cycle_id, "step": "start"})

    try:
        await event_bus.emit(AgentCycleStarted(
            cycle_id=cycle_id,
            mode=config.get("mode", "approval"),
            search_count=len(enabled_searches),
        ))
    except Exception as e:
        logger.warning("Failed to emit %s: %s", "AgentCycleStarted", e,
                       extra={"cycle_id": cycle_id, "step": "start"})

    # Build saga with shared mutable state for inter-step data flow
    saga = SagaExecutor("agent_cycle")
    cycle_state = {"cycle_id": cycle_id, "config": config}

    async def _step_observe():
        if ctx:
            try:
                await ctx.info(f"[{cycle_id}] observe — gathering applied IDs + daily quota")
            except Exception:
                pass
        result = await _observe(cycle_id, config)
        cycle_state.update(result)
        if ctx:
            try:
                await ctx.report_progress(
                    1, 4,
                    message=f"observe done ({result['daily_remaining']} slots)",
                )
            except Exception:
                pass
        # Early termination: raise if daily limit exhausted (except dry_run)
        if result["daily_remaining"] <= 0 and config.get("mode") != "dry_run":
            raise _DailyLimitReached(result["daily_applied"])
        return result

    async def _step_decide():
        if ctx:
            try:
                await ctx.info(f"[{cycle_id}] decide — running searches and scoring")
            except Exception:
                pass
        result = await _decide(cycle_state)
        cycle_state.update(result)
        if ctx:
            try:
                await ctx.report_progress(
                    2, 4,
                    message=f"decide done ({len(result.get('candidates', []))} candidates)",
                )
            except Exception:
                pass
        return result

    async def _step_act():
        if ctx:
            try:
                await ctx.info(
                    f"[{cycle_id}] act — mode={config.get('mode', 'approval')}, "
                    f"{len(cycle_state.get('candidates', []))} candidates"
                )
            except Exception:
                pass
        result = await _act(cycle_state)
        cycle_state.update(result)
        if ctx:
            try:
                await ctx.report_progress(
                    3, 4,
                    message=f"act done (applied={result.get('applied', 0)}, "
                            f"pending={result.get('pending_approval', 0)})",
                )
            except Exception:
                pass
        return result

    async def _step_learn():
        if ctx:
            try:
                await ctx.info(f"[{cycle_id}] learn — recording cycle results")
            except Exception:
                pass
        # Compute total duration from saga step timings so far
        cycle_state["_duration_ms"] = sum(
            ms for _, _, ms in saga.completed
        )
        result = await _learn(cycle_state)
        if ctx:
            try:
                await ctx.report_progress(
                    4, 4,
                    message=f"learn done (applied={result.get('applied', 0)}, "
                            f"skipped={result.get('skipped', 0)})",
                )
            except Exception:
                pass
        return result

    async def _compensate_observe():
        """Compensate observe: mark cycle as failed."""
        try:
            from naukri_server.database import update_agent_run as _update_run
            await _update_run(cycle_id, status="failed",
                              finished_at=datetime.now(timezone.utc).isoformat(),
                              error="saga_compensation_observe")
        except Exception as e:
            logger.warning("Observe compensation failed: %s", e,
                           extra={"cycle_id": cycle_id, "step": "compensate_observe"})

    async def _compensate_decide():
        """Compensate decide: clear candidate state."""
        cycle_state["candidates"] = []
        logger.info("Decide compensation: cleared candidates for %s", cycle_id,
                    extra={"cycle_id": cycle_id, "step": "compensate_decide"})

    saga.add_step("observe", _step_observe, compensate=_compensate_observe, timeout=60)
    saga.add_step("decide", _step_decide, compensate=_compensate_decide, timeout=180)
    saga.add_step("act", _step_act, timeout=300)
    saga.add_step("learn", _step_learn, timeout=30)

    saga_result = await saga.run()

    if saga_result["status"] == "success":
        # Attach step timings to the run metadata
        try:
            await update_agent_run(cycle_id,
                metadata=json.dumps({
                    "config_hash": config_hash(config),
                    "step_timings": saga_result["step_timings"],
                }))
        except Exception:
            pass

        # Return the learn step's result plus saga metadata
        learn_result = saga_result.get("results", {}).get("learn", {})

        if ctx:
            try:
                applied = learn_result.get("applied", 0)
                skipped = learn_result.get("skipped", 0)
                await ctx.info(
                    f"Cycle {cycle_id} complete: {applied} applied, {skipped} skipped"
                )
            except Exception:
                pass

        return {
            **learn_result,
            "status": "success",
            "step_timings": saga_result["step_timings"],
        }

    # Saga failed — check for daily-limit early termination
    if (saga_result.get("failed_step") == "observe"
            and any("_DailyLimitReached" in e for e in saga_result.get("errors", []))):
        await update_agent_run(cycle_id, status="skipped",
                               finished_at=datetime.now(timezone.utc).isoformat())
        return {
            "status": "skipped",
            "reason": "Daily application limit reached",
            "daily_applied": cycle_state.get("daily_applied", 0),
            "step_timings": saga_result.get("step_timings", {}),
        }

    # Generic saga failure
    total_duration_ms = sum(saga_result.get("step_timings", {}).values())
    error_msg = "; ".join(str(e) for e in saga_result.get("errors", []))

    await update_agent_run(
        cycle_id, status="error",
        finished_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=total_duration_ms,
        error=error_msg[:500],
    )
    logger.error("Agent cycle %s saga failed: %s", cycle_id, error_msg,
                 extra={"cycle_id": cycle_id, "step": "saga_failed"})
    return {
        "status": "error",
        "cycle_id": cycle_id,
        "message": error_msg,
        "completed_steps": saga_result.get("completed_steps", []),
        "step_timings": saga_result.get("step_timings", {}),
    }
