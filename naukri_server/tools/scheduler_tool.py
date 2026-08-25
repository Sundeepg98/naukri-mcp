"""MCP tools for controlling the background task scheduler."""

import json
import logging
from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action
from naukri_server.scheduler import salvage_json, summarize_result_blob

logger = logging.getLogger(__name__)

# History payload budget.
#
# `naukri_task_history` used to hand back every column of every row, result
# blobs included. Measured against the live database 2026-08-25, the default
# limit=20 shipped 9,696 bytes and limit=25 shipped 13,687 bytes -- of which
# 5,690 bytes were result blobs, and 4,000 of those were two `reminder_check`
# rows that had been severed mid-string at storage time and could not be
# parsed by anybody. The point of an MCP tool is to spend the caller's context
# on answers, not on bytes; a run's identity, timing, verdict and a derived
# one-line summary answer "what has the scheduler been doing" completely.
HISTORY_DEFAULT_LIMIT = 20
HISTORY_MAX_LIMIT = 100
# Aggregate ceiling for the whole response, not just per row. A per-row cap
# alone is not a bound: 20 rows x 2000 chars is 40,000 chars, which is what
# the old shape could return at its own default.
HISTORY_PAYLOAD_CAP_BYTES = 6000
# How far back a single-run lookup will scan for an id. Bounded on purpose --
# only ever ONE row comes back, but the scan should not walk 1,585 rows.
_RUN_LOOKUP_WINDOW = 500


def _get_scheduler():
    """Get the scheduler singleton, raising if not available."""
    import naukri_server.scheduler as _mod
    if _mod.scheduler is None:
        raise RuntimeError("Scheduler not initialized — server may not be fully started")
    return _mod.scheduler


async def _scheduler_status() -> dict:
    """Get current scheduler status."""
    sched = _get_scheduler()
    status = sched.status

    # Add last run info for each task
    from naukri_server.database import get_last_run
    tasks_with_history = {}
    for name, info in status.get("tasks", {}).items():
        last = await get_last_run(name)
        tasks_with_history[name] = {
            **info,
            "last_run": {
                "started_at": last["started_at"],
                "status": last["status"],
                "duration_ms": last.get("duration_ms"),
            } if last else None,
        }
    status["tasks"] = tasks_with_history
    return {"status": "success", **status}


async def _scheduler_enable(task_name: str) -> dict:
    """Enable a scheduled task."""
    sched = _get_scheduler()
    if sched.enable_task(task_name):
        return {"status": "success", "message": f"Task '{task_name}' enabled"}
    return {"status": "error", "message": f"Unknown task: {task_name}", "error_code": "NOT_FOUND"}


async def _scheduler_disable(task_name: str) -> dict:
    """Disable a scheduled task."""
    sched = _get_scheduler()
    if sched.disable_task(task_name):
        return {"status": "success", "message": f"Task '{task_name}' disabled"}
    return {"status": "error", "message": f"Unknown task: {task_name}", "error_code": "NOT_FOUND"}


async def _scheduler_run_now(task_name: str) -> dict:
    """Run a specific task immediately."""
    sched = _get_scheduler()
    result = await sched.run_now(task_name)
    return result


def _summary_row(row: dict) -> dict:
    """One history row reduced to identity, timing, verdict and a summary.

    Deliberately does NOT carry `result`. The blob is reachable one run at a
    time via `run_id`; it is never included in a listing.
    """
    summarized = {
        "id": row.get("id"),
        "task_name": row.get("task_name"),
        "started_at": row.get("started_at"),
        "duration_ms": round(row["duration_ms"]) if isinstance(row.get("duration_ms"), (int, float)) else None,
        "status": row.get("status"),
    }
    summary = summarize_result_blob(row.get("result"))
    if summary:
        summarized["summary"] = summary
    error = row.get("error")
    if error:
        summarized["error"] = str(error)[:200]
    return summarized


def _collapse_runs(rows: list) -> list:
    """Fold CONSECUTIVE runs that say the same thing into one row.

    The operator's actual complaint was not row size, it was that
    `naukri_task_history(limit=25)` reported "24 identical rows of
    confirmed: 0". A scheduled task that runs every five minutes and reports
    the same verdict every time costs one row of information no matter how
    many times it ran, and the thing worth knowing is the COUNT and the SPAN.

    Grouping is on (task_name, status, summary), so a run that failed, or that
    found something the others did not, has a different key and can never be
    absorbed into a group -- an anomaly always surfaces as its own row. Every
    collapsed group keeps `latest_id`, so `run_id=` still reaches the detail.
    """
    collapsed: list = []
    for row in rows:
        key = (row.get("task_name"), row.get("status"), row.get("summary"), row.get("error"))
        if collapsed and collapsed[-1]["_key"] == key:
            group = collapsed[-1]
            group["runs"] += 1
            group["started_from"] = row.get("started_at")
            if isinstance(row.get("duration_ms"), (int, float)):
                prior = group["row"].get("duration_ms")
                group["row"]["duration_ms"] = max(row["duration_ms"], prior or 0)
            continue
        collapsed.append({"_key": key, "row": row, "runs": 1,
                          "started_from": row.get("started_at")})

    out: list = []
    for group in collapsed:
        row = group["row"]
        if group["runs"] == 1:
            out.append(row)
            continue
        folded = {
            "task_name": row.get("task_name"),
            "status": row.get("status"),
            "runs": group["runs"],
            "started_from": group["started_from"],
            "started_to": row.get("started_at"),
            "max_duration_ms": row.get("duration_ms"),
            "latest_id": row.get("id"),
        }
        if row.get("summary"):
            folded["summary"] = row["summary"]
        if row.get("error"):
            folded["error"] = row["error"]
        out.append(folded)
    return out


async def _scheduler_one_run(run_id: int) -> dict:
    """Return exactly ONE run, with its full stored result."""
    from naukri_server.database import list_scheduled_runs
    runs = await list_scheduled_runs(task_name=None, limit=_RUN_LOOKUP_WINDOW)
    match = next((r for r in runs if r.get("id") == run_id), None)
    if match is None:
        return {
            "status": "error",
            "error_code": "NOT_FOUND",
            "message": (f"No run {run_id} in the {_RUN_LOOKUP_WINDOW} most recent runs. "
                        f"Call naukri_task_history() to see current run ids."),
        }

    run = _summary_row(match)
    parsed, state = salvage_json(match.get("result"))
    run["result"] = parsed
    run["result_state"] = state
    if state == "salvaged":
        run["note"] = ("Stored before the storage fix and severed mid-JSON; "
                       "the intact prefix was recovered.")
    elif state == "unreadable" and match.get("result"):
        run["note"] = "Stored blob is not valid JSON and could not be recovered."
        run["raw_result"] = match["result"][:HISTORY_PAYLOAD_CAP_BYTES]
    return {"status": "success", "total": 1, "run": run}


async def _scheduler_history(task_name: Optional[str] = None,
                             limit: int = HISTORY_DEFAULT_LIMIT,
                             run_id: Optional[int] = None,
                             collapse: bool = True) -> dict:
    """Get recent scheduled run history as summaries, or one full run."""
    if run_id is not None:
        return await _scheduler_one_run(run_id)

    from naukri_server.database import list_scheduled_runs

    requested = limit
    limit = max(1, min(int(limit), HISTORY_MAX_LIMIT))
    rows = await list_scheduled_runs(task_name=task_name, limit=limit)

    summarized = [_summary_row(row) for row in rows]
    entries = _collapse_runs(summarized) if collapse else summarized

    # Aggregate bound. Entries go in newest-first until the budget is spent;
    # what does not fit is REPORTED, never dropped in silence. A per-row cap
    # is not a bound -- the old shape's own default could return 20 x 2000.
    kept: list = []
    used = 0
    runs_covered = 0
    for entry in entries:
        cost = len(json.dumps(entry)) + 1
        if kept and used + cost > HISTORY_PAYLOAD_CAP_BYTES:
            break
        kept.append(entry)
        used += cost
        runs_covered += entry.get("runs", 1)

    result = {
        "status": "success",
        "total": len(kept),
        "runs_covered": runs_covered,
        "runs": kept,
        "detail": "Summaries only. For one run's full result: naukri_task_history(run_id=<id>).",
    }
    if collapse and len(kept) != runs_covered:
        result["collapsed"] = (f"{runs_covered} runs folded into {len(kept)} rows; "
                               f"consecutive runs with an identical verdict share a row. "
                               f"Pass collapse=False for one row per run.")
    elided = len(entries) - len(kept)
    if elided:
        result["elided"] = elided
        result["elided_reason"] = f"payload cap {HISTORY_PAYLOAD_CAP_BYTES} bytes reached"
    if requested != limit:
        result["limit_clamped_to"] = limit
    return result


# ---------------------------------------------------------------------------
# Atomic single-purpose MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_scheduler_status() -> dict:
    """Show scheduler state + all task statuses + last run times.

    Returns:
        {status, running, total_tasks, enabled_tasks,
         tasks: {name: {enabled, interval, last_run}}}
    """
    return await handle_tool_action(_scheduler_status, "scheduler.status")


@mcp.tool()
async def naukri_enable_task(task_name: str) -> dict:
    """Enable a previously disabled scheduled task.

    Args:
        task_name: One of sync_applications, sync_saved_jobs, daily_brief, stale_check,
            boost_profile, reminder_check, notification_digest, api_discovery

    Returns:
        {status, message} or {status: error, error_code: NOT_FOUND}
    """
    if not task_name:
        return {"status": "error", "message": "task_name is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _scheduler_enable(task_name=task_name),
        "scheduler.enable",
    )


@mcp.tool()
async def naukri_disable_task(task_name: str) -> dict:
    """Disable a scheduled task from running.

    Args:
        task_name: One of sync_applications, sync_saved_jobs, daily_brief, stale_check,
            boost_profile, reminder_check, notification_digest, api_discovery

    Returns:
        {status, message} or {status: error, error_code: NOT_FOUND}
    """
    if not task_name:
        return {"status": "error", "message": "task_name is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _scheduler_disable(task_name=task_name),
        "scheduler.disable",
    )


@mcp.tool()
async def naukri_run_task_now(task_name: str) -> dict:
    """Execute a scheduled task immediately, regardless of schedule.

    Args:
        task_name: One of sync_applications, sync_saved_jobs, daily_brief, stale_check,
            boost_profile, reminder_check, notification_digest, api_discovery

    Returns:
        Task execution result (depends on the task)
    """
    if not task_name:
        return {"status": "error", "message": "task_name is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _scheduler_run_now(task_name=task_name),
        "scheduler.run_now",
    )


@mcp.tool()
async def naukri_task_history(task_name: Optional[str] = None,
                              limit: int = HISTORY_DEFAULT_LIMIT,
                              run_id: Optional[int] = None,
                              collapse: bool = True) -> dict:
    """Show recent scheduled run history as one-line summaries per run.

    Summaries only: no result blobs are ever returned by a listing, and the
    whole response is capped at 6,000 bytes regardless of `limit`. Each row
    carries a derived summary instead -- e.g.
    `total=50, due_count=50, reminders=50, oldest overdue 161d`.

    Consecutive runs with an identical verdict share one row carrying the run
    count and the time span, so a five-minute task does not report the same
    line twenty times. A failed or differing run never joins a group.

    To read one run's full stored result, pass that run's `run_id` (the `id`
    of a single row, or `latest_id` of a collapsed group). One id returns one
    run; there is no value of `run_id` that returns more than one.

    Args:
        task_name: Optional task name filter (None = all tasks)
        limit: Max runs to consider, clamped to 100 (default 20)
        run_id: Return exactly this one run WITH its full result, instead of a
            listing. Takes precedence over limit/task_name/collapse.
        collapse: Fold consecutive identical runs (default True). False gives
            strictly one row per run.

    Returns:
        Listing: {status, total, runs_covered, runs: [...], detail,
                  collapsed?, elided?}
        Single:  {status, total: 1, run: {..., result, result_state}}
    """
    return await handle_tool_action(
        lambda: _scheduler_history(task_name=task_name or None, limit=limit,
                                   run_id=run_id, collapse=collapse),
        "scheduler.history",
    )
