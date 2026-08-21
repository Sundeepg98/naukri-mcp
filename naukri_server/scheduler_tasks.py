"""Scheduled task definitions — thin wrappers over existing service functions.

Migrated to @scheduled_task decorator (Track 1: unified registry framework).
Wiring happens in __init__.py via wire_scheduled_tasks(scheduler).
"""

import logging

from naukri_server.scheduler import ScheduledTask
from naukri_server.framework.registry import scheduled_task

logger = logging.getLogger(__name__)


# CATCH-UP POLICY
# ---------------
# The scheduler sleeps a full interval before a task's FIRST run, so on a server
# whose process lifetime is shorter than the interval a task never runs at all.
# Measured 2026-08-20: sync_applications / sync_saved_jobs (4h), stale_check
# (6h), daily_brief and boost_profile (daily) had ZERO rows in scheduled_runs.
#
# catch_up=True makes a task run shortly after startup when its persisted last
# run is older than its interval. It is granted ONLY to tasks that neither
# mutate the Naukri account nor delete local rows, because it means "runs on
# every server start". Deliberately excluded:
#   agent_cycle       - searches AND APPLIES to jobs (consumes the daily quota)
#   boost_profile     - rewrites the profile headline on naukri.com
#   sync_applications - its purge step calls delete_applications_before(), which
#                       drops rows older than AUTO_PURGE_DAYS (config.py)
#   api_discovery     - probes many endpoints; pacing matters with 7 captcha
#                       blocks on the account record
#   daily_brief       - hour-gated (08:00 IST), not interval-gated

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
    catch_up=True,  # read + upsert only; no purge/delete step
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
    catch_up=True,  # pure read over the local DB
)
async def _task_stale_check() -> dict:
    """Detect stale applications needing follow-up."""
    from naukri_server import policy
    from naukri_server.config import STALE_THRESHOLD_DAYS, STALE_MIN_SCORE
    from naukri_server.services.application_service import get_stale_applications
    return await get_stale_applications(
        days_threshold=policy.setting("staleness.days", STALE_THRESHOLD_DAYS),
        min_stale_score=policy.setting("staleness.min_stale_score",
                                       STALE_MIN_SCORE),
    )


@scheduled_task(
    name="boost_profile",
    interval_seconds=INTERVAL_DAILY,
    description="Boost profile visibility at 9am IST",
    run_at_hour=9,
    timeout_seconds=60,
)
async def _task_boost_profile() -> dict:
    """Boost profile visibility by re-saving headline.

    `randomize` defaults to False — today's literal, pinned by
    test_scheduler.py and test_profile_deep.py. The design proposed
    flipping it to True; a default flip is a behaviour change and ships
    in its own commit, never inside a mechanism change. He can flip it
    himself now, which is the point of the file.
    """
    from naukri_server import policy
    from naukri_server.tools.profile_update import _boost_visibility

    if not policy.setting("boost_profile.enabled", True):
        return {"status": "skipped",
                "reason": "servers.naukri.boost_profile.enabled is false"}
    return await _boost_visibility(
        randomize=policy.setting("boost_profile.randomize", False))


@scheduled_task(
    name="reminder_check",
    interval_seconds=INTERVAL_1H,
    description="Check for due reminders every hour",
    timeout_seconds=30,
    catch_up=True,  # pure read; emits ReminderDue events
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
    catch_up=True,  # read-only fetch of the notification summary
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


# Interval for T2 verification. Must be <= VERIFY_DELAY_SECONDS (600s) so a fix
# that becomes due is picked up promptly rather than sitting an extra cycle.
INTERVAL_5MIN = 300


async def _revalidate_endpoint(constant_name: str) -> bool:
    """Return True if `constant_name`'s endpoint is currently healthy (no drift).

    Used by the t2_verify_pending task to decide whether a provisional T2
    auto-fix should be confirmed or reverted. Conservative on every failure
    path: if we can't positively confirm health (config missing, fetch fails,
    no snapshot to compare, or any exception) we return False so the fix is
    REVERTED rather than trusted. A bad fix being reverted is always safer than
    a bad fix being kept.
    """
    try:
        from naukri_server import config as _cfg
        from naukri_server.interfaces import api_client
        from naukri_server.probes.drift_detector import detector as _detector

        try:
            path = getattr(_cfg, constant_name)
        except AttributeError:
            logger.warning("revalidate: config constant %s missing", constant_name)
            return False

        if not _detector.has_snapshot(path):
            # No baseline to compare against — we can't prove the fix worked.
            logger.info("revalidate: no snapshot for %s — cannot confirm, reverting", path)
            return False

        response = await api_client.get(path)
        report = _detector.check_drift(path, response)
        return report is None  # None == schemas match == healthy
    except Exception as exc:  # noqa: BLE001 — any failure ⇒ treat as unhealthy
        logger.warning("revalidate of %s raised: %s — treating as unhealthy", constant_name, exc)
        return False


@scheduled_task(
    name="t2_verify_pending",
    interval_seconds=INTERVAL_5MIN,
    description="Verify pending T2 auto-fixes; revert any that still drift",
    timeout_seconds=120,
)
async def _task_t2_verify_pending() -> dict:
    """Verify due T2 auto-fixes and revert the ones that didn't fix the drift.

    This is the scheduler half of the T2 snapshot+revert safety contract:
    apply_t2_fix records a pending row, and this task (running every 5 min)
    re-validates each endpoint once its verify_at has passed — confirming the
    fix if the endpoint is healthy, or git-reverting it (and disabling the
    healer on revert failure) otherwise. Without this task a bad T2 fix would
    sit `pending` and unverified forever.

    Repo root is the package root (the dir containing naukri_server/), which is
    the live git checkout the healer commits into.
    """
    from naukri_server.config import _PACKAGE_ROOT
    from naukri_server.healing import t2_autofix
    counts = await t2_autofix.verify_due_pending_rows(_PACKAGE_ROOT, _revalidate_endpoint)
    return {"status": "completed", **counts}


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
