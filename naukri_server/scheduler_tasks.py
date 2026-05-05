"""Scheduled task definitions — thin wrappers over existing service functions.

Migrated to @scheduled_task decorator (Track 1: unified registry framework).
Wiring happens in __init__.py via wire_scheduled_tasks(scheduler).
"""

import logging

from naukri_server.scheduler import ScheduledTask
from naukri_server.framework.registry import scheduled_task

logger = logging.getLogger(__name__)


# Interval constants (seconds)
INTERVAL_1H = 3600
INTERVAL_2H = 7200
INTERVAL_4H = 14400
INTERVAL_6H = 21600
INTERVAL_DAILY = 86400
INTERVAL_WEEKLY = 604800


# ---------------------------------------------------------------------------
# Task wrapper functions — each calls an existing service helper
# ---------------------------------------------------------------------------

@scheduled_task(
    name="sync_applications",
    interval_seconds=INTERVAL_4H,
    description="Sync applications from Naukri every 4 hours",
    timeout_seconds=120,
)
async def _task_sync_applications() -> dict:
    """Sync applications from Naukri API."""
    from naukri_server.tools.sync import _sync_applications
    return await _sync_applications()


@scheduled_task(
    name="sync_saved_jobs",
    interval_seconds=INTERVAL_4H,
    description="Sync saved jobs from Naukri every 4 hours",
    timeout_seconds=120,
)
async def _task_sync_saved_jobs() -> dict:
    """Sync saved jobs from Naukri API."""
    from naukri_server.tools.sync import _sync_saved_jobs
    return await _sync_saved_jobs()


@scheduled_task(
    name="daily_brief",
    interval_seconds=INTERVAL_DAILY,
    description="Generate morning daily brief at 8am IST",
    run_at_hour=8,
    timeout_seconds=180,
)
async def _task_daily_brief() -> dict:
    """Generate daily brief summary."""
    from naukri_server.tools.daily_brief import naukri_daily_brief
    return await naukri_daily_brief()


@scheduled_task(
    name="stale_check",
    interval_seconds=INTERVAL_6H,
    description="Detect stale applications every 6 hours",
    timeout_seconds=60,
)
async def _task_stale_check() -> dict:
    """Detect stale applications needing follow-up."""
    from naukri_server.services.application_service import get_stale_applications
    return await get_stale_applications(days_threshold=14, min_stale_score=40)


@scheduled_task(
    name="boost_profile",
    interval_seconds=INTERVAL_DAILY,
    description="Boost profile visibility at 9am IST",
    run_at_hour=9,
    timeout_seconds=60,
)
async def _task_boost_profile() -> dict:
    """Boost profile visibility by re-saving headline."""
    from naukri_server.tools.profile_update import _boost_visibility
    return await _boost_visibility(randomize=False)


@scheduled_task(
    name="reminder_check",
    interval_seconds=INTERVAL_1H,
    description="Check for due reminders every hour",
    timeout_seconds=30,
)
async def _task_reminder_check() -> dict:
    """Check for due reminders and emit events."""
    from naukri_server.tools.reminders import _list_reminders
    result = await _list_reminders(include_past=True)
    due_count = result.get("due_count", 0)
    if due_count > 0:
        logger.info("Reminder check: %d due reminders found", due_count)
    return result


@scheduled_task(
    name="notification_digest",
    interval_seconds=INTERVAL_2H,
    description="Fetch notification digest every 2 hours",
    timeout_seconds=60,
)
async def _task_notification_digest() -> dict:
    """Fetch notification summary for digest."""
    from naukri_server.tools.notifications import _get_unified_notify
    return await _get_unified_notify()


@scheduled_task(
    name="api_discovery",
    interval_seconds=INTERVAL_WEEKLY,
    description="Discover new API endpoints weekly",
    timeout_seconds=300,
)
async def _task_discovery() -> dict:
    """Run automated API endpoint discovery."""
    from naukri_server.health.probes.discovery import discover_config_audit
    result = await discover_config_audit()
    return {"status": result.status, "message": result.message, "metadata": result.metadata}


@scheduled_task(
    name="agent_cycle",
    interval_seconds=INTERVAL_2H,
    description="Run autonomous agent cycle every 2 hours",
    timeout_seconds=600,  # 10 minutes — search + apply can take time
)
async def _task_agent_cycle() -> dict:
    """Run autonomous agent observe→decide→act→learn cycle."""
    from naukri_server.agent import run_agent_cycle
    return await run_agent_cycle()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_all(scheduler) -> None:
    """Register all scheduled tasks with the given scheduler.

    Replaced the TASK_DEFINITIONS list and the manual registration loop with
    the unified @scheduled_task decorator framework. This function now wires
    only the production tasks (those defined in this module) to the scheduler,
    so test fixtures registering tasks globally don't leak into production.
    """
    from naukri_server.framework.registry import registry
    count = 0
    for it in registry.by_kind("scheduled_task"):
        if it.fn.__module__ == __name__:
            scheduler.register(ScheduledTask(fn=it.fn, **it.metadata))
            count += 1
    logger.info("Registered %d scheduled tasks", count)


# ---------------------------------------------------------------------------
# Backward-compatibility shim — old tests import TASK_DEFINITIONS directly.
# Built lazily from registry to avoid import-order issues.
# ---------------------------------------------------------------------------

def _build_task_definitions() -> list:
    """Materialize the list of ScheduledTask objects from registry decorators.

    Kept for backward compatibility with test code that imports TASK_DEFINITIONS.
    """
    from naukri_server.framework.registry import registry
    out = []
    for it in registry.by_kind("scheduled_task"):
        # Only include tasks defined in this module (skip test fixtures registering tasks elsewhere)
        if it.fn.__module__ == __name__:
            out.append(ScheduledTask(fn=it.fn, **it.metadata))
    return out


TASK_DEFINITIONS = _build_task_definitions()
