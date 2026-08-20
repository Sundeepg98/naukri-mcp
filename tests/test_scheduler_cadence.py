"""Scheduler cadence - does a task actually run on the interval it declares?

Measured on the live server 2026-08-20 (scheduled_runs, all times UTC, process
started 07:32):

    t2_verify_pending   (300s)     31 runs
    api_discovery       (604800s)   2 runs   08:34 and 09:35  <-- ONE HOUR apart
    reminder_check      (3600s)     2 runs   08:34 and 09:34
    agent_cycle         (7200s)     1 run    09:34
    notification_digest (7200s)     1 run    09:34
    sync_applications   (14400s)    0 runs
    sync_saved_jobs     (14400s)    0 runs
    stale_check         (21600s)    0 runs
    daily_brief         (86400s@8)  0 runs
    boost_profile       (86400s@9)  0 runs

Two distinct defects are visible in that table:

A. A WEEKLY task ran HOURLY. Buckets with interval >= 86400 sleep
   min(seconds_until_next_run, 3600) and then execute every task in the bucket
   whose run_at_hour matches. A task with run_at_hour=None matches ALWAYS, so
   it fired on every hourly wake - 168x its declared rate.

B. The loop sleeps BEFORE the first run, so a task never runs until the process
   has been alive for one whole interval. The 4h and 6h tasks have zero rows
   because the server does not live that long. That is why six tasks showed
   last_run = null and the tracking DB stayed empty.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server.scheduler import ScheduledTask, TaskScheduler


def _counting_task(name, interval, calls, **kw):
    async def fn():
        calls.append(name)
        return {"status": "success"}

    return ScheduledTask(name=name, fn=fn, interval_seconds=interval, **kw)


async def _drive(scheduler, tasks, interval, wakes):
    """Run _run_interval for exactly `wakes` wake-ups, with no real sleeping."""
    n = {"i": 0}

    async def fake_sleep(_seconds):
        n["i"] += 1
        if n["i"] >= wakes:
            scheduler._running = False

    scheduler._running = True
    with (
        patch("naukri_server.scheduler.asyncio.sleep", new=fake_sleep),
        patch("naukri_server.database.insert_scheduled_run", new=AsyncMock(return_value=1)),
        patch("naukri_server.database.update_scheduled_run", new=AsyncMock()),
    ):
        await asyncio.wait_for(
            scheduler._run_interval(interval, tasks), timeout=10
        )


# ---------------------------------------------------------------------------
# Defect A - a long-interval task must not fire on every hourly wake
# ---------------------------------------------------------------------------

async def test_weekly_task_does_not_fire_on_every_hourly_wake():
    """RED before the fix: api_discovery (604800s) ran at 08:34 and 09:35."""
    sched = TaskScheduler()
    calls = []
    task = _counting_task("weekly_probe", 604800, calls)
    sched.register(task)
    sched.prime_schedule()

    await _drive(sched, [task], 604800, wakes=5)

    assert calls == [], (
        "a weekly task fired %d time(s) across 5 hourly wakes" % len(calls)
    )


async def test_daily_task_with_run_at_hour_is_unaffected():
    """The hour gate still governs run_at_hour tasks; only the interval gate
    is new. With the hour never matching, the task must not run."""
    from datetime import datetime

    from naukri_server.scheduler import IST

    never_now = (datetime.now(IST).hour + 12) % 24
    sched = TaskScheduler()
    calls = []
    task = _counting_task("brief", 86400, calls, run_at_hour=never_now)
    sched.register(task)
    sched.prime_schedule()

    await _drive(sched, [task], 86400, wakes=4)

    assert calls == []


async def test_interval_loop_sleeps_after_an_unexpected_error():
    """An out-of-range run_at_hour makes _seconds_until_next_run raise BEFORE
    the loop sleeps. Without a backstop the loop spins at 100% CPU forever and
    never runs a task. Verified by driving it: the fake sleep must still be
    reached, which is the only way this test can terminate."""
    sched = TaskScheduler()
    calls = []
    task = _counting_task("broken_hour", 86400, calls, run_at_hour=25)
    sched.register(task)
    sched.prime_schedule()

    await _drive(sched, [task], 86400, wakes=2)  # times out at 10s if it spins

    assert calls == []


# ---------------------------------------------------------------------------
# Defect B - catch-up for tasks that opt in
# ---------------------------------------------------------------------------

async def test_task_without_catch_up_still_waits_a_full_interval():
    """Default behaviour is unchanged: sleep first, then run. This is what
    keeps mutating tasks (agent_cycle applies to jobs, boost_profile writes the
    profile) from firing on every server start."""
    sched = TaskScheduler()
    calls = []
    task = _counting_task("mutating", 7200, calls)
    sched.register(task)
    sched.prime_schedule()

    # One wake at t=0 (fake sleep consumes no real time) - not due yet.
    await _drive(sched, [task], 7200, wakes=2)
    assert calls == []


async def test_catch_up_task_runs_on_the_first_wake_when_overdue():
    """A catch_up task whose last run is older than its interval is DUE now,
    so a freshly started process runs it instead of waiting a whole interval
    it will not survive."""
    sched = TaskScheduler()
    calls = []
    task = _counting_task("readonly_sync", 14400, calls, catch_up=True)
    sched.register(task)

    # Seed as if it last ran 5 hours ago (interval is 4h).
    sched.prime_schedule(last_started={"readonly_sync": _hours_ago(5)})

    # wakes=2: the loop breaks immediately after the sleep that flips _running,
    # so N wakes give N-1 effective task passes. Two wakes therefore means one
    # pass -- which must produce exactly ONE run, not a repeat.
    await _drive(sched, [task], 14400, wakes=2)
    assert calls == ["readonly_sync"]


async def test_catch_up_task_does_not_rerun_when_recently_run():
    sched = TaskScheduler()
    calls = []
    task = _counting_task("readonly_sync", 14400, calls, catch_up=True)
    sched.register(task)
    sched.prime_schedule(last_started={"readonly_sync": _hours_ago(1)})

    await _drive(sched, [task], 14400, wakes=3)
    assert calls == []


async def test_executing_a_task_marks_it_not_due():
    sched = TaskScheduler()
    calls = []
    task = _counting_task("t", 3600, calls)
    sched.register(task)
    sched.prime_schedule(last_started={"t": _hours_ago(9)})

    assert sched.is_due(task) is True
    with (
        patch("naukri_server.database.insert_scheduled_run", new=AsyncMock(return_value=1)),
        patch("naukri_server.database.update_scheduled_run", new=AsyncMock()),
    ):
        await sched._execute_task(task)
    assert sched.is_due(task) is False


async def test_run_now_bypasses_the_interval_gate():
    """naukri_run_task_now must still work on a task that is not due."""
    sched = TaskScheduler()
    calls = []
    task = _counting_task("t", 604800, calls)
    sched.register(task)
    sched.prime_schedule()

    assert sched.is_due(task) is False
    with (
        patch("naukri_server.database.insert_scheduled_run", new=AsyncMock(return_value=1)),
        patch("naukri_server.database.update_scheduled_run", new=AsyncMock()),
    ):
        result = await sched.run_now("t")
    assert calls == ["t"]
    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Production task table - which tasks are allowed to catch up
# ---------------------------------------------------------------------------

def test_only_non_mutating_tasks_opt_into_catch_up():
    """Catch-up makes a task run shortly after every server start. Anything
    that MUTATES the Naukri account or deletes local rows must not be in this
    set - notably agent_cycle (applies to jobs), boost_profile (rewrites the
    profile headline) and sync_applications (its purge step can delete
    applications older than AUTO_PURGE_DAYS)."""
    from naukri_server.scheduler_tasks import TASK_DEFINITIONS

    catching_up = sorted(t.name for t in TASK_DEFINITIONS if t.catch_up)
    assert catching_up == [
        "notification_digest",
        "reminder_check",
        "stale_check",
        "sync_saved_jobs",
    ]

    for forbidden in ("agent_cycle", "boost_profile", "sync_applications", "api_discovery"):
        task = next(t for t in TASK_DEFINITIONS if t.name == forbidden)
        assert task.catch_up is False, "%s must not catch up on startup" % forbidden


def _hours_ago(hours):
    import time

    return time.time() - hours * 3600
