"""`naukri_server_info()` -- which code is this process actually running.

Every other diagnostic on this server is a BEHAVIOURAL fingerprint: does this
field appear, is that count right, does the tool still crash. None of them can
separate "the code is wrong" from "the code is old", and on 2026-08-21 that
cost real time -- a bug was diagnosed as a regression and an agent dispatched to
re-fix it, and the fix was already on disk. The process was stale.

This module reads the frozen constants in :mod:`naukri_server.buildinfo` and
NEVER re-resolves them. That freeze is the entire point; see that module's
header for why a per-call `git rev-parse` is worse than reporting nothing.
"""

from typing import Optional

from naukri_server import mcp
from naukri_server.buildinfo import BUILD, CLOCK, JOBCORE_BUILD


def _surface_count(manager_name: str, registry_name: str) -> Optional[int]:
    """``len()`` of one FastMCP registry, or ``None`` if it was renamed.

    These are FastMCP PRIVATES. An upstream rename must cost one null field,
    not the whole tool -- this is what a reader reaches for when something is
    already broken, so it has to survive being the last thing that works.
    ``None`` also stays honest: it says "not measured", where a ``0`` would say
    "measured, and there are none".
    """
    try:
        return len(getattr(getattr(mcp, manager_name), registry_name))
    except Exception:
        return None


def _config_block() -> dict:
    """Which policy this process is scoring with, and where it came from.

    Both fingerprints, because they are not interchangeable: ``scoring_hash``
    is what a STORED SCORE carries and what says whether an old score is still
    comparable; ``policy_hash`` also covers ``candidate``, which moves every
    score without touching the arithmetic.
    """
    from naukri_server import policy

    try:
        stamp = policy.policy_stamp()
    except Exception:
        stamp = {"policy_rev": None, "policy_hash": None, "scoring_hash": None}
    try:
        source = policy.display_path(policy.snapshot().source)
    except Exception:
        source = None
    return {"source": source, **stamp}


def _database_path() -> Optional[str]:
    """The sqlite file, relativised. A debug tool is the last place a machine
    layout should leak -- it is the payload most likely to be pasted somewhere."""
    try:
        from naukri_server import policy
        from naukri_server.database import DB_PATH

        return policy.display_path(str(DB_PATH))
    except Exception:
        return None


@mcp.tool()
async def naukri_server_info() -> dict:
    """Report which code this running server holds. Check this FIRST, always.

    A fix committed to disk changes nothing for a process that is already up.
    Before debugging any behaviour, confirm the process is not simply stale:

        1. Read `build.code.commit` from this tool.
        2. Run `git rev-parse HEAD` in the naukri checkout.
        3. If they DIFFER, this process is running older code. Nothing further
           you observe about its behaviour is meaningful until it is restarted
           -- re-fixing a bug that is already fixed on disk is the exact hour
           this tool exists to prevent. Restart the MCP server, then re-check.

    Do the same for `build.jobcore.commit` against the jobcore checkout. jobcore
    is installed editable from a sibling repository and moves on its own
    schedule, and the fit-scoring engine, the config loader and the path
    renderer all live there -- so a stale jobcore is exactly as invisible as a
    stale naukri, and produces the same wasted hour.

    `build.code.dirty` is the other half of the answer: a clean commit match
    still leaves uncommitted edits unaccounted for, and an edited working tree
    is running code that no commit describes. `dirty_files` gives the count so
    it can be judged rather than feared.

    The stamps are resolved ONCE, at import, and frozen. That is deliberate: a
    per-call `git rev-parse` would report the commit on DISK, so a stale process
    would confirm its own freshness -- an answer that looks like evidence and is
    false. `build.process.uptime_seconds` is the opposite and is recomputed on
    every call, because a cached uptime is a lie that grows.

    Returns:
        {status, server, build: {code, jobcore, process}, config, surface,
         database}

        - build.code / build.jobcore: {commit (12 chars), commit_full, branch,
          committed_at, dirty, dirty_files, resolved_at, source, detail}.
          `source` is "git" or "unknown"; on "unknown", `commit` is null and
          `detail` says why. There is no fallback that invents a hash.
        - build.process: {pid, started_at, uptime_seconds}. `started_at` is
          when this module was imported, which for a stdio MCP server is
          process start to within milliseconds.
        - config: {source, policy_rev, policy_hash, scoring_hash}. `source` is
          null when no config file was found. `scoring_hash` is the one a
          stored score carries; `policy_hash` also covers `candidate`.
        - surface: {tools, resources, prompts} counts. A null means the FastMCP
          internal was renamed upstream, not that there are none.
        - database: the sqlite file, relativised (never an absolute path).
    """
    return {
        "status": "success",
        "server": "naukri",
        "build": {
            # READ the frozen constants; NEVER call jobcore's `resolve()` here.
            # A per-call resolve makes this whole tool report the commit on
            # DISK, so a stale process confirms its own freshness. Proven by
            # temporarily swapping this line for
            # `_bi.resolve(SERVER_ROOT).as_dict()` and watching
            # test_the_stamp_is_not_re_resolved_per_call go red.
            "code": BUILD.as_dict(),
            "jobcore": JOBCORE_BUILD.as_dict(),
            "process": CLOCK.as_dict(),
        },
        "config": _config_block(),
        "surface": {
            "tools": _surface_count("_tool_manager", "_tools"),
            "resources": _surface_count("_resource_manager", "_resources"),
            "prompts": _surface_count("_prompt_manager", "_prompts"),
        },
        "database": _database_path(),
    }
