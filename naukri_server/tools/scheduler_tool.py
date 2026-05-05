"""MCP tools for controlling the background task scheduler."""

import logging
from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action

logger = logging.getLogger(__name__)


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


async def _scheduler_history(task_name: Optional[str] = None, limit: int = 20) -> dict:
    """Get recent scheduled run history."""
    from naukri_server.database import list_scheduled_runs
    runs = await list_scheduled_runs(task_name=task_name, limit=limit)
    return {
        "status": "success",
        "total": len(runs),
        "runs": runs,
    }


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
async def naukri_task_history(task_name: Optional[str] = None, limit: int = 20) -> dict:
    """Show recent scheduled run history, optionally filtered by task name.

    Args:
        task_name: Optional task name filter (None = all tasks)
        limit: Max records (default 20)

    Returns:
        {status, total, runs: [{task_name, started_at, finished_at, status, duration_ms}]}
    """
    return await handle_tool_action(
        lambda: _scheduler_history(task_name=task_name or None, limit=limit),
        "scheduler.history",
    )
