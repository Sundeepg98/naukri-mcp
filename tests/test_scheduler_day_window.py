"""Fixed-hour daily tasks - do they ever actually run?

Measured on the operator's live database 2026-08-30, over the whole history in
`scheduled_runs` (2,228 rows, 2026-08-20 to 2026-08-30):

    t2_verify_pending   1830 runs
    reminder_check       152
    notification_digest   79
    agent_cycle           66
    sync_saved_jobs       41
    sync_applications     30
    stale_check           26
    api_discovery          2
    retention_sweep        2
    daily_brief            0     <-- run_at_hour=8
    boost_profile          0     <-- run_at_hour=9

Both fixed-hour tasks have NEVER run. Not once in ten days.

The recorded suspicion was `catch_up=False`, and that is not the mechanism.
catch_up only shortens the FIRST sleep after startup; the hour gate in
`_run_interval` rejects the task regardless of it, so flipping the flag on
`daily_brief` would have changed nothing.

The real mechanism needs BOTH of these to be true in the SAME process:

  1. `prime_schedule` seeds every non-catch_up task's `_last_started` to
     `now` at each start, so `is_due` demands 86400s of CONTINUOUS uptime.
  2. `_is_target_hour` is an equality test, so the loop must also be awake
     inside that one specific IST hour.

Uptime measured from the 5-minute t2_verify_pending heartbeat: 26 restarts in
ten days, and only TWO sessions ever reached 24h continuous (25.73h ending
08-24 15:18 IST, 32.58h ending 08-26 23:53 IST). Neither crossed 08:00 or
09:00 IST AFTER passing its 24h mark, so condition 2 never coincided with
condition 1.

The control that pins the mechanism: `retention_sweep` sits in the SAME 86400s
bucket with `run_at_hour=None`, so it needs condition 1 only. It ran exactly
twice - 08-24 14:00 IST and 08-26 16:00 IST - which is the first hourly wake
after the 24h mark of each of those two sessions, to the minute. Same bucket,
same interval, same clock; the only difference is the hour gate, and it is the
difference between two runs and zero.

The fix replaces "exactly this hour, and only after a full interval of uptime"
with a DAY WINDOW: a fixed-hour task is due once per IST calendar day, from its
target hour until `catch_up_window_hours` later, and "already served today" is
read from PERSISTED history so restarts cannot multiply it.

That last clause is the load-bearing one for `boost_profile`, which writes to
the real Naukri profile. `test_a_restart_storm_still_boosts_exactly_once`
below is its guard: 26 restarts in one day must produce ONE run.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server.scheduler import IST, ScheduledTask, TaskScheduler


def _at(hour, minute=0, day=None):
    """Epoch seconds for a given IST wall-clock time (today unless `day` given)."""
    base = datetime.now(IST).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if day is not None:
        base += timedelta(days=day)
    return base.timestamp()


def _task(name, calls, **kw):
    async def fn():
        calls.append(name)
        return {"status": "success"}

    kw.setdefault("interval_seconds", 86400)
    return ScheduledTask(name=name, fn=fn, **kw)


# ---------------------------------------------------------------------------
# The window itself
# ---------------------------------------------------------------------------


def test_fixed_hour_task_is_due_inside_its_hour():
    """08:30 IST, target hour 8, never run. This is the plain case that has
    produced zero runs in ten days."""
    sched = TaskScheduler()
    task = _task("daily_brief", [], run_at_hour=8)
    sched.register(task)
    sched.prime_schedule()

    assert sched.is_due(task, now=_at(8, 30)) is True


def test_fixed_hour_task_catches_up_a_missed_window():
    """The server booted at 10:12 IST today - a real boot time from the live
    history. The 08:00 window was missed while the process was down; a brief
    delivered at 10:12 is still today's brief."""
    sched = TaskScheduler()
    task = _task("daily_brief", [], run_at_hour=8)
    sched.register(task)
    sched.prime_schedule()

    assert sched.is_due(task, now=_at(10, 12)) is True


def test_fixed_hour_task_is_not_due_before_its_hour():
    sched = TaskScheduler()
    task = _task("daily_brief", [], run_at_hour=8)
    sched.register(task)
    sched.prime_schedule()

    assert sched.is_due(task, now=_at(7, 59)) is False


def test_fixed_hour_task_is_not_due_once_the_window_has_closed():
    """A 'morning brief' at 22:00 is not a morning brief. The catch-up is
    bounded, not open until midnight."""
    sched = TaskScheduler()
    task = _task("daily_brief", [], run_at_hour=8, catch_up_window_hours=6)
    sched.register(task)
    sched.prime_schedule()

    assert sched.is_due(task, now=_at(14, 1)) is False
    assert sched.is_due(task, now=_at(22, 0)) is False


def test_window_may_cross_midnight():
    """A 22:00 task with a 6h window is still due at 01:00 the next day - the
    window belongs to the day it OPENED on."""
    sched = TaskScheduler()
    task = _task("late", [], run_at_hour=22, catch_up_window_hours=6)
    sched.register(task)
    sched.prime_schedule()

    assert sched.is_due(task, now=_at(1, 0, day=1)) is True
    assert sched.is_due(task, now=_at(5, 0, day=1)) is False


def test_running_inside_the_window_closes_it_for_the_day():
    sched = TaskScheduler()
    task = _task("daily_brief", [], run_at_hour=8)
    sched.register(task)
    sched.prime_schedule(last_started={"daily_brief": _at(8, 5)})

    assert sched.is_due(task, now=_at(8, 30)) is False
    assert sched.is_due(task, now=_at(13, 0)) is False


def test_yesterdays_run_does_not_close_todays_window():
    sched = TaskScheduler()
    task = _task("daily_brief", [], run_at_hour=8)
    sched.register(task)
    sched.prime_schedule(last_started={"daily_brief": _at(8, 5, day=-1)})

    assert sched.is_due(task, now=_at(8, 30)) is True


# ---------------------------------------------------------------------------
# The safety property that matters for boost_profile
# ---------------------------------------------------------------------------


def test_a_restart_storm_still_boosts_exactly_once():
    """26 restarts in one day - the measured restart count over the live
    server's last ten days, compressed into a single day - must produce ONE
    boost, not 26.

    `boost_profile` re-saves the headline on the operator's real Naukri
    profile. The once-per-day bound is therefore not a tidiness property; it
    is the whole reason this task is allowed to catch up at all. It has to
    survive a restart, which means it has to be read from persisted history
    and not from process memory.
    """
    persisted = {}
    runs = []

    for i in range(26):
        now = _at(9, 0) + i * 120  # a restart every two minutes from 09:00
        sched = TaskScheduler()
        task = _task("boost_profile", [], run_at_hour=9)
        sched.register(task)
        # What prime_schedule_from_db does: seed from the persisted last run.
        sched.prime_schedule(last_started=dict(persisted))
        if sched.is_due(task, now=now):
            runs.append(now)
            persisted["boost_profile"] = now

    assert len(runs) == 1, (
        "boost_profile fired %d times across 26 restarts in one day; it "
        "rewrites the live Naukri profile headline" % len(runs)
    )


def test_a_failed_history_read_fails_closed_for_a_fixed_hour_task():
    """If the persisted last run cannot be read, a fixed-hour task must be
    treated as ALREADY SERVED, not as due.

    Failing open here means every restart during the window re-runs the task,
    and for boost_profile that is a write to the live account on each one.
    """
    sched = TaskScheduler()
    task = _task("boost_profile", [], run_at_hour=9)
    sched.register(task)

    async def boom(_name):
        raise RuntimeError("database unavailable")

    with patch("naukri_server.database.get_last_run", new=boom):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            sched.prime_schedule_from_db()
        )

    assert sched.is_due(task, now=_at(9, 30)) is False


async def test_prime_schedule_from_db_seeds_fixed_hour_tasks():
    """Fixed-hour tasks must consult persisted history, not just catch_up
    tasks. Without this a restart inside the window re-opens it."""
    sched = TaskScheduler()
    task = _task("boost_profile", [], run_at_hour=9)
    sched.register(task)

    row = {"started_at": datetime.fromtimestamp(_at(9, 5), IST).isoformat()}
    with patch("naukri_server.database.get_last_run",
               new=AsyncMock(return_value=row)):
        await sched.prime_schedule_from_db()

    assert sched.is_due(task, now=_at(9, 30)) is False


# ---------------------------------------------------------------------------
# End to end through the real interval loop
# ---------------------------------------------------------------------------


async def _drive(scheduler, tasks, interval, wakes):
    """Run _run_interval for exactly `wakes` wake-ups with no real sleeping."""
    n = {"i": 0}
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        n["i"] += 1
        if n["i"] >= wakes:
            scheduler._running = False

    scheduler._running = True
    with (
        patch("naukri_server.scheduler.asyncio.sleep", new=fake_sleep),
        patch("naukri_server.database.insert_scheduled_run",
              new=AsyncMock(return_value=1)),
        patch("naukri_server.database.update_scheduled_run", new=AsyncMock()),
    ):
        await asyncio.wait_for(scheduler._run_interval(interval, tasks), timeout=10)
    return slept


async def test_the_loop_actually_runs_a_due_fixed_hour_task():
    """The whole defect, end to end: a fixed-hour task whose window is open
    must be executed by the real interval loop, on a freshly started process
    that has been alive for seconds rather than a day."""
    sched = TaskScheduler()
    calls = []
    now_hour = datetime.now(IST).hour
    task = _task("daily_brief", calls, run_at_hour=now_hour)
    sched.register(task)
    sched.prime_schedule()

    await _drive(sched, [task], 86400, wakes=2)

    assert calls == ["daily_brief"], (
        "the interval loop did not run a fixed-hour task inside its own "
        "window on a freshly started process"
    )


async def test_the_loop_wakes_quickly_when_a_window_is_already_open():
    """Sleeping up to an hour before looking is the same defect wearing a
    smaller number: the measured uptime sessions include several under 90
    minutes."""
    sched = TaskScheduler()
    calls = []
    now_hour = datetime.now(IST).hour
    task = _task("daily_brief", calls, run_at_hour=now_hour)
    sched.register(task)
    sched.prime_schedule()

    slept = await _drive(sched, [task], 86400, wakes=1)

    assert slept[0] == TaskScheduler._STARTUP_GRACE_SECONDS, (
        "first wake was %ss with an open window" % slept[0]
    )


async def test_the_loop_does_not_rerun_a_task_already_served_today():
    sched = TaskScheduler()
    calls = []
    now_hour = datetime.now(IST).hour
    task = _task("daily_brief", calls, run_at_hour=now_hour)
    sched.register(task)
    sched.prime_schedule()

    await _drive(sched, [task], 86400, wakes=6)

    assert calls == ["daily_brief"]


# ---------------------------------------------------------------------------
# The production table
# ---------------------------------------------------------------------------


def test_the_two_dead_tasks_declare_a_catch_up_window():
    """daily_brief and boost_profile are the two tasks with zero runs. Both
    must now carry a bounded window; a fixed-hour task without one is the
    old behaviour under a new name."""
    from naukri_server.scheduler_tasks import TASK_DEFINITIONS

    by_name = {t.name: t for t in TASK_DEFINITIONS}
    for name, hour in (("daily_brief", 8), ("boost_profile", 9)):
        task = by_name[name]
        assert task.run_at_hour == hour
        assert task.catch_up_window_hours > 0
        assert task.catch_up_window_hours <= 12, (
            "%s would catch up %sh after its window opened, which is no "
            "longer the scheduled hour in any useful sense"
            % (name, task.catch_up_window_hours)
        )


def test_fixed_hour_tasks_still_do_not_opt_into_startup_catch_up():
    """The day window is NOT catch_up. catch_up means 'runs on every server
    start', which is exactly what boost_profile must never do -- 26 starts in
    a day would be 26 writes to the live profile. The window is bounded to
    once per IST day instead, so the two mechanisms must stay separate."""
    from naukri_server.scheduler_tasks import TASK_DEFINITIONS

    for t in TASK_DEFINITIONS:
        if t.run_at_hour is not None:
            assert t.catch_up is False, (
                "%s is fixed-hour AND catch_up; that is once per start, not "
                "once per day" % t.name
            )
