"""Background task scheduler — autonomous operations without Claude."""

import asyncio
import json
import logging
import time
from naukri_server._compat import timeout as _timeout
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

# IST offset (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Result storage: cap the size WITHOUT ever severing the JSON
# ---------------------------------------------------------------------------
#
# The old code stored `json.dumps(result)[:2000]`. A slice of a JSON document
# is not a JSON document. Measured on the live database 2026-08-25: every one
# of the 137 rows that hit the 2000-char cap is unparseable -- 137 of 137, a
# perfect correlation, because the cut lands mid-string far more often than on
# a boundary. A reminder_check row ended `..."remind_at": "2026-03-17T` and
# raised `Unterminated string`. The row was simultaneously the largest thing in
# the table and the only thing in it that could not be read: 2000 bytes that
# decode to nothing.
#
# The replacement never cuts the document. It cuts the DATA -- dropping the
# containers, which is where the bulk always is, and keeping the scalars, which
# is where the meaning always is. The live shapes make that trade lopsided in
# our favour: every task puts its counts first and its payload last, so
# `{"status": "success", "total": 50, "due_count": 50, "reminders": [...50...]}`
# reduces to the same three numbers plus `{"omitted": "list", "items": 50}` --
# ~160 bytes that parse, instead of 2000 bytes that do not.

RESULT_STORAGE_CAP_BYTES = 2000
_SCALAR_STRING_CAP = 300


def _dumps(obj) -> str:
    """json.dumps that cannot raise -- falls back to a repr envelope."""
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return json.dumps({"status": "unknown", "repr": str(obj)[:_SCALAR_STRING_CAP]})


# A dropped list of records is worth one derived fact about it. These are the
# numeric fields the live tasks actually carry (measured off naukri.db
# 2026-08-25: reminder_check, stale_check); anything absent is skipped in
# silence. Table-driven so a tenth task costs a line, not a branch.
_DIGEST_NUMERIC_FIELDS = ("days_until_due", "stale_score", "follow_up_priority")


def _digest_records(items: list) -> dict:
    """min/max of the known numeric fields across a list of record dicts."""
    digest: dict = {}
    for field in _DIGEST_NUMERIC_FIELDS:
        values = [it[field] for it in items
                  if isinstance(it, dict)
                  and isinstance(it.get(field), (int, float))
                  and not isinstance(it.get(field), bool)]
        if values:
            digest[f"{field}_min"] = min(values)
            digest[f"{field}_max"] = max(values)
    return digest


def _describe_container(value):
    """Replace a container with a count of what was dropped."""
    if isinstance(value, list):
        described = {"omitted": "list", "items": len(value)}
        described.update(_digest_records(value))
        return described
    return {"omitted": "dict", "keys": len(value)}


def salvage_json(raw: Optional[str]) -> tuple:
    """Read a stored result blob, repairing a legacy severed document.

    Returns ``(obj, state)`` where state is ``ok``, ``salvaged`` or
    ``unreadable``. Rows written before the storage fix were cut with
    ``[:2000]``; the cut always lands at the END, so the PREFIX is intact and
    every task writes its scalars first. Walking back to the last comma that
    sits outside a string and closing what is still open recovers those
    leading counts -- which is the whole of what the row was ever worth.
    """
    if not raw:
        return None, "unreadable"
    try:
        return json.loads(raw), "ok"
    except (ValueError, TypeError):
        pass

    stack: list = []
    in_string = False
    escaped = False
    cut_at = -1
    cut_stack: list = []
    for i, ch in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "[{":
            stack.append(ch)
        elif ch in "]}":
            if stack:
                stack.pop()
        elif ch == "," and stack:
            # A comma outside a string always follows a COMPLETE value, so
            # this index is a safe place to end the document.
            cut_at = i
            cut_stack = list(stack)

    if cut_at < 0:
        return None, "unreadable"
    closers = "".join("]" if opener == "[" else "}" for opener in reversed(cut_stack))
    try:
        return json.loads(raw[:cut_at] + closers), "salvaged"
    except (ValueError, TypeError):
        return None, "unreadable"


# Fields that carry no signal in a one-line summary: `status` has its own
# column, and the rest are plumbing.
_SUMMARY_SKIP = {
    "status", "last_sync", "truncated", "original_bytes",
    "saga_steps", "saga_errors", "step_timings", "method",
}


def summarize_result_blob(raw: Optional[str], max_fields: int = 6) -> Optional[str]:
    """One short line derived from a stored result blob, or None if empty.

    This is what `naukri_task_history` returns INSTEAD of the blob. The blob
    for a due-reminder sweep was 2000 unparseable bytes; this is
    `total=50, due_count=50, reminders=50, oldest overdue 161d` -- about a
    dozen tokens, and unlike the blob it can actually be read.
    """
    if not raw:
        return None
    obj, state = salvage_json(raw)
    if state == "unreadable":
        return f"unreadable blob ({len(raw)}B)"
    if not isinstance(obj, dict):
        return f"{type(obj).__name__} result"

    parts: list = []
    for key, value in obj.items():
        if len(parts) >= max_fields:
            break
        if key in _SUMMARY_SKIP:
            continue
        if isinstance(value, bool):
            if value:
                parts.append(key)
        elif isinstance(value, (int, float)):
            parts.append(f"{key}={value}")
        elif isinstance(value, list):
            parts.append(f"{key}={len(value)}")
        elif isinstance(value, dict):
            if value.get("omitted"):
                parts.append(f"{key}={value.get('items', value.get('keys', '?'))}")
            else:
                parts.append(f"{key}={len(value)} keys")
        elif isinstance(value, str) and value:
            parts.append(f"{key}={value[:80]}")

    overdue = _overdue_note(obj)
    if overdue:
        parts.append(overdue)
    if state == "salvaged":
        parts.append("[salvaged from severed blob]")
    if obj.get("truncated"):
        parts.append(f"[reduced from {obj.get('original_bytes')}B]")
    return ", ".join(str(p) for p in parts) if parts else None


def _overdue_note(obj: dict) -> Optional[str]:
    """`oldest overdue 161d` when the row carries a due-date digest."""
    for value in obj.values():
        if isinstance(value, dict) and isinstance(value.get("days_until_due_min"), (int, float)):
            worst = value["days_until_due_min"]
            if worst < 0:
                return f"oldest overdue {abs(int(worst))}d"
        elif isinstance(value, list):
            due = [it.get("days_until_due") for it in value
                   if isinstance(it, dict) and isinstance(it.get("days_until_due"), (int, float))]
            if due and min(due) < 0:
                return f"oldest overdue {abs(int(min(due)))}d"
    return None


def reduce_result_for_storage(result, cap: int = RESULT_STORAGE_CAP_BYTES) -> str:
    """Serialise a task result to at most `cap` bytes of ALWAYS-VALID JSON.

    Returns a string that `json.loads` accepts, unconditionally. When the
    result does not fit, what comes back still parses and still says how much
    was dropped -- `truncated`, `original_bytes`, and a per-field count for
    every container removed. Never a slice of a longer document.
    """
    if not isinstance(result, dict):
        result = {"status": "success", "result": result}

    full = _dumps(result)
    if len(full) <= cap:
        return full

    original_bytes = len(full)

    # Pass 1 -- keep every scalar, replace every container with its count.
    reduced: dict = {}
    for key, value in result.items():
        if isinstance(value, (list, dict)):
            reduced[key] = _describe_container(value)
        elif isinstance(value, str) and len(value) > _SCALAR_STRING_CAP:
            # Truncating a string VALUE is safe: it stays a quoted JSON string.
            # Truncating the DOCUMENT is what broke 137 rows.
            reduced[key] = value[:_SCALAR_STRING_CAP] + "..."
        else:
            reduced[key] = value
    reduced["truncated"] = True
    reduced["original_bytes"] = original_bytes

    candidate = _dumps(reduced)
    if len(candidate) <= cap:
        return candidate

    # Pass 2 -- pathological width (hundreds of scalar keys). Keep only the
    # fields that carry the verdict, and say how many were dropped.
    keep = {k: reduced[k] for k in ("status", "message", "error", "error_code")
            if k in reduced}
    keep["truncated"] = True
    keep["original_bytes"] = original_bytes
    keep["summary"] = f"{len(result)} fields dropped; result too large to store"
    candidate = _dumps(keep)
    if len(candidate) <= cap:
        return candidate

    # Pass 3 -- the floor. Fixed shape, always well under any sane cap.
    return _dumps({
        "truncated": True,
        "original_bytes": original_bytes,
        "summary": "result too large to store",
    })


@dataclass
class ScheduledTask:
    """Definition of a task to run on a schedule."""
    name: str
    fn: Callable[[], Awaitable[dict]]
    interval_seconds: float
    description: str = ""
    enabled: bool = True
    run_at_hour: Optional[int] = None  # Hour in IST (0-23), None = interval-based
    timeout_seconds: float = 120.0
    # Run shortly after startup when the persisted last run is older than
    # interval_seconds, instead of waiting a full interval the process may not
    # survive. OFF by default: catch-up means "runs on every server start",
    # which is only safe for tasks that neither mutate the Naukri account nor
    # delete local rows.
    catch_up: bool = False
    # How long after `run_at_hour` a missed window may still be served. Only
    # meaningful when run_at_hour is set.
    #
    # This is NOT catch_up and must not be confused with it. catch_up is "once
    # per server START"; this is "once per IST DAY", read from persisted
    # history, so 26 restarts inside the window still produce one run. That
    # difference is what lets boost_profile -- which rewrites the live Naukri
    # headline -- opt in at all.
    #
    # Bounded rather than open-ended because the point of a fixed hour is the
    # hour: a 08:00 brief delivered at 13:00 is late but still today's brief,
    # and delivered at 23:00 it is not a morning brief at all.
    catch_up_window_hours: float = 6.0


class TaskScheduler:
    """Runs registered tasks on declared intervals.

    Reuses the asyncio background-task pattern from HealthProbeScheduler:
    one asyncio.Task per interval bucket, sleep → execute → log → repeat.
    """

    def __init__(self):
        self._tasks: dict[str, ScheduledTask] = {}
        self._asyncio_tasks: list[asyncio.Task] = []
        self._running = False
        self._task_locks: dict[str, asyncio.Lock] = {}
        # Wall-clock epoch seconds of the last execution START, per task. This
        # is the interval clock: a task is due only once interval_seconds have
        # actually elapsed. Before this existed, a bucket wake ran every task
        # in the bucket, so a WEEKLY task in the >=86400s bucket (which wakes
        # hourly) fired 168x its declared rate -- measured on 2026-08-20:
        # api_discovery ran at 08:34 and again at 09:35.
        self._last_started: dict[str, float] = {}

    # Backstop pause after an unexpected error inside an interval loop, so a
    # persistent failure degrades to slow retries instead of a hot loop.
    _ERROR_BACKOFF_SECONDS = 60.0

    # First-wake delay used INSTEAD of a full interval when a bucket already
    # holds an overdue catch_up task. Long enough for the browser and the API
    # session to finish coming up, short enough that the work actually happens
    # on a process that may only live an hour.
    _STARTUP_GRACE_SECONDS = 45.0

    def register(self, task: ScheduledTask):
        """Register a scheduled task."""
        self._tasks[task.name] = task
        logger.info("Registered scheduled task: %s (every %ds)", task.name, task.interval_seconds)

    async def start(self):
        """Create one asyncio task per interval bucket (matching HealthProbeScheduler pattern)."""
        self._running = True
        await self.prime_schedule_from_db()

        # Group tasks by interval (bucket pattern from HealthProbeScheduler)
        buckets: dict[float, list[ScheduledTask]] = {}
        for task in self._tasks.values():
            if task.enabled:
                buckets.setdefault(task.interval_seconds, []).append(task)

        for interval, tasks in buckets.items():
            at = asyncio.create_task(
                self._run_interval(interval, tasks),
                name=f"scheduler-{interval}s",
            )
            self._asyncio_tasks.append(at)

        logger.info(
            "TaskScheduler started: %d tasks in %d interval buckets",
            sum(len(t) for t in buckets.values()),
            len(buckets),
        )

    async def stop(self):
        """Signal shutdown and wait for running tasks to finish."""
        self._running = False
        # Give running tasks up to 10s to finish naturally
        for _ in range(100):  # 100 * 0.1s = 10s
            if all(not lock.locked() for lock in self._task_locks.values()):
                break
            await asyncio.sleep(0.1)
        # Then cancel any remaining
        for task in self._asyncio_tasks:
            task.cancel()
        if self._asyncio_tasks:
            await asyncio.gather(*self._asyncio_tasks, return_exceptions=True)
        self._asyncio_tasks.clear()
        logger.info("TaskScheduler stopped")

    @property
    def status(self) -> dict:
        """Get scheduler status summary."""
        return {
            "running": self._running,
            "total_tasks": len(self._tasks),
            "enabled_tasks": sum(1 for t in self._tasks.values() if t.enabled),
            "disabled_tasks": sum(1 for t in self._tasks.values() if not t.enabled),
            "tasks": {
                name: {
                    "enabled": t.enabled,
                    "interval_seconds": t.interval_seconds,
                    "run_at_hour": t.run_at_hour,
                    "description": t.description,
                    "catch_up": t.catch_up,
                    # Only meaningful for fixed-hour tasks; None elsewhere so a
                    # reader is not invited to think an interval task has a
                    # window.
                    "catch_up_window_hours": (
                        t.catch_up_window_hours if t.run_at_hour is not None
                        else None
                    ),
                    **self._window_report(t),
                    "seconds_since_last_start": (
                        None if name not in self._last_started
                        else round(time.time() - self._last_started[name], 1)
                    ),
                }
                for name, t in self._tasks.items()
            },
        }

    def _window_report(self, task: ScheduledTask) -> dict:
        """The window/dueness fields for `status`, never raising.

        `status` backs `naukri_scheduler_status`, which is the operator's only
        window into this subsystem. A task registered with an out-of-range
        `run_at_hour` makes `_window_start` raise (datetime.replace rejects
        hour=25), and before the day window existed nothing in `status` touched
        the hour at all -- so computing it here introduced a way for a
        READ-ONLY tool to die on a misconfiguration it exists to reveal.

        The exception is not swallowed anywhere it matters: `_run_interval`
        still lets it hit the backstop sleep, so a bad task degrades to slow
        retries rather than a hot loop. This method only ensures the report
        NAMES the broken task instead of replacing the whole listing with a
        stack trace.
        """
        report: dict = {"window_opened_at": None}
        try:
            if task.run_at_hour is not None:
                report["window_opened_at"] = self._window_start(task).isoformat()
            report["due_now"] = self.is_due(task)
        except Exception as exc:  # noqa: BLE001 - a report must not crash
            report["due_now"] = None
            report["error"] = f"{type(exc).__name__}: {exc}"
        return report

    def enable_task(self, task_name: str) -> bool:
        """Enable a task. Returns True if found."""
        if task_name in self._tasks:
            self._tasks[task_name].enabled = True
            return True
        return False

    def disable_task(self, task_name: str) -> bool:
        """Disable a task. Returns True if found."""
        if task_name in self._tasks:
            self._tasks[task_name].enabled = False
            return True
        return False

    async def run_now(self, task_name: str) -> dict:
        """Run a specific task immediately, regardless of schedule."""
        if task_name not in self._tasks:
            return {"status": "error", "message": f"Unknown task: {task_name}"}
        task = self._tasks[task_name]
        return await self._execute_task(task)

    # -- interval clock -------------------------------------------------------

    def is_due(self, task: ScheduledTask, now: Optional[float] = None) -> bool:
        """Is this task due to run right now?

        Two different clocks, chosen by whether the task declares an hour:

        INTERVAL tasks ask whether interval_seconds have elapsed since the last
        start. An unseeded task counts as due. prime_schedule() seeds every
        non-catch_up task with "now" so the historical sleep-then-run cadence
        is preserved exactly; catch_up tasks are seeded from their persisted
        last run, or left unseeded (= due) when there is none.

        FIXED-HOUR tasks ask whether today's window is open and unserved. They
        must NOT use the interval clock: combining it with the hour gate is
        what killed daily_brief and boost_profile outright. The interval clock
        restarts from zero on every process start, so it demands 24h of
        continuous uptime, and the hour gate then demands the loop also be
        awake inside one specific hour. Measured over ten days on the live
        server: 26 restarts, only two sessions ever reached 24h, and neither
        crossed 08:00 or 09:00 IST after passing its 24h mark. Zero runs from
        2,228 recorded executions. See tests/test_scheduler_day_window.py.
        """
        if task.run_at_hour is not None:
            return self._window_is_open_and_unserved(task, now)
        last = self._last_started.get(task.name)
        if last is None:
            return True
        return (now if now is not None else time.time()) - last >= task.interval_seconds

    def _window_start(self, task: ScheduledTask, now: Optional[float] = None) -> datetime:
        """The most recent opening of this task's daily window, in IST.

        Today's occurrence of run_at_hour if that has already passed,
        otherwise yesterday's -- so a window opened before midnight still
        belongs to the day it opened on.
        """
        now_ist = datetime.fromtimestamp(
            now if now is not None else time.time(), IST,
        )
        start = now_ist.replace(
            hour=task.run_at_hour, minute=0, second=0, microsecond=0,
        )
        if start > now_ist:
            start -= timedelta(days=1)
        return start

    def _window_is_open_and_unserved(
        self, task: ScheduledTask, now: Optional[float] = None,
    ) -> bool:
        """Due iff we are inside the current window and it has not run in it.

        "Has not run in it" is answered from `_last_started`, which
        prime_schedule_from_db seeds from PERSISTED history for fixed-hour
        tasks. That persistence is the safety property, not an optimisation:
        without it every restart inside the window re-opens it, and for
        boost_profile each re-open is another write to the live Naukri
        profile.
        """
        epoch = now if now is not None else time.time()
        start = self._window_start(task, epoch)
        if epoch >= (start + timedelta(hours=task.catch_up_window_hours)).timestamp():
            return False
        last = self._last_started.get(task.name)
        return last is None or last < start.timestamp()

    def prime_schedule(self, last_started: Optional[dict] = None) -> None:
        """Seed the interval clock before the loops start.

        `last_started` maps task name -> epoch seconds and wins over defaults.
        """
        now = time.time()
        provided = last_started or {}
        for name, task in self._tasks.items():
            if name in provided:
                self._last_started[name] = provided[name]
            elif task.catch_up or task.run_at_hour is not None:
                # Unseeded == due on the first wake. Fixed-hour tasks join
                # catch_up tasks here because seeding them with "now" is
                # precisely what blocked them: it puts _last_started inside
                # the current window, so the window reads as already served
                # and the task can never run in the process that started it.
                self._last_started.pop(name, None)
            else:
                self._last_started[name] = now

    @staticmethod
    def _epoch_from_run_row(row: Optional[dict]) -> Optional[float]:
        """Convert a scheduled_runs row into epoch seconds (None if unusable)."""
        if not row:
            return None
        started = row.get("started_at")
        if not started:
            return None
        try:
            dt = datetime.fromisoformat(started)
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    async def prime_schedule_from_db(self) -> None:
        """Seed the interval clock, consulting persisted history for catch_up tasks.

        Failure to read history is never fatal: the task simply falls back to
        "due now" (catch_up) or "wait a full interval" (everything else).
        """
        seed: dict[str, float] = {}
        for name, task in self._tasks.items():
            fixed_hour = task.run_at_hour is not None
            if not task.catch_up and not fixed_hour:
                continue
            try:
                from naukri_server.database import get_last_run
                ts = self._epoch_from_run_row(await get_last_run(name))
            except Exception as exc:
                logger.warning("Could not read last run for %s: %s", name, exc)
                if fixed_hour:
                    # FAIL CLOSED. Unseeded means "due", so falling through
                    # here would run the task on every restart for as long as
                    # the database stays unreadable -- and boost_profile's run
                    # is a write to the live Naukri profile. Treating the
                    # window as already served costs at most one skipped day.
                    seed[name] = time.time()
                continue
            if ts is not None:
                seed[name] = ts
        self.prime_schedule(last_started=seed)

    # -- internal -----------------------------------------------------------

    async def _run_interval(self, interval: float, tasks: list[ScheduledTask]):
        """Loop: sleep → check schedule → run eligible tasks → repeat."""
        first_wake = True
        while self._running:
            try:
                if first_wake and self._bucket_has_work_waiting(tasks):
                    # Something is ALREADY due. Sleeping a whole interval here
                    # is what made 4h and 6h tasks never run at all: the server
                    # does not live that long, so the first wake never arrived.
                    # Use a short startup grace instead.
                    #
                    # A fixed-hour task with an open window qualifies too. The
                    # 86400s bucket otherwise sleeps up to an hour before it
                    # looks, and several measured uptime sessions were under 90
                    # minutes -- an hour of that spent asleep is the same
                    # defect wearing a smaller number.
                    logger.info(
                        "Scheduler bucket %ss: work already due, "
                        "first wake in %.0fs instead of %ss",
                        interval, self._STARTUP_GRACE_SECONDS, interval,
                    )
                    await asyncio.sleep(self._STARTUP_GRACE_SECONDS)
                # For daily tasks with run_at_hour, calculate sleep to next target
                elif interval >= 86400:
                    sleep_time = self._seconds_until_next_run(tasks)
                    if sleep_time > 0:
                        await asyncio.sleep(min(sleep_time, 3600))  # Check every hour max
                    else:
                        await asyncio.sleep(60)  # Check again in 1 min
                else:
                    await asyncio.sleep(interval)

                first_wake = False
                if not self._running:
                    break
                for task in tasks:
                    if not task.enabled:
                        logger.debug("Task %s is disabled, skipping", task.name)
                        continue
                    if not self.is_due(task):
                        # Buckets with interval >= 86400 wake HOURLY (they sleep
                        # min(seconds_until_next_run, 3600)), so without this
                        # gate every run_at_hour=None task in such a bucket ran
                        # once an hour regardless of its declared interval.
                        logger.debug(
                            "Task %s: not due yet (interval %ss)",
                            task.name, task.interval_seconds,
                        )
                        continue
                    try:
                        await self._execute_task(task)
                    except Exception as exc:
                        logger.error("Task %s raised unhandled exception: %s", task.name, exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                # BACKSTOP SLEEP. Anything raised BEFORE this iteration's sleep
                # (e.g. _seconds_until_next_run on a task with an out-of-range
                # run_at_hour) used to send the loop straight back to the top
                # without sleeping -- a hot loop that pins a core and never runs
                # a single task. Found while writing tests/test_scheduler_cadence.py.
                logger.warning("Scheduler interval %ss error: %s", interval, exc)
                try:
                    await asyncio.sleep(min(interval, self._ERROR_BACKOFF_SECONDS))
                except asyncio.CancelledError:
                    break

    def _bucket_has_work_waiting(self, tasks: list[ScheduledTask]) -> bool:
        """Is anything in this bucket already due at startup?

        Only two kinds of task can be: a catch_up task whose persisted last run
        is older than its interval, and a fixed-hour task whose window is open
        and unserved. A plain interval task is seeded with "now" by
        prime_schedule and is never due here, so this stays false for the
        buckets that must keep their sleep-first cadence.
        """
        return any(t.enabled and self.is_due(t) for t in tasks)

    def _seconds_until_next_run(self, tasks: list[ScheduledTask]) -> float:
        """Calculate seconds until the next task should run."""
        now = datetime.now(IST)
        min_wait = 86400.0  # 24h default
        for task in tasks:
            if not task.enabled or task.run_at_hour is None:
                continue
            target = now.replace(hour=task.run_at_hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            min_wait = min(min_wait, wait)
        return min_wait

    async def _execute_task(self, task: ScheduledTask) -> dict:
        """Run a single task with timeout, log to database, emit events."""
        # Per-task execution lock — skip if previous run still in progress
        lock = self._task_locks.setdefault(task.name, asyncio.Lock())
        if lock.locked():
            logger.info("Task %s still running from previous cycle, skipping", task.name)
            return {"status": "skipped", "reason": "previous_execution_not_complete"}

        async with lock:
            from naukri_server.database import insert_scheduled_run, update_scheduled_run

            # Stamp the interval clock at START, not at completion: a task that
            # takes minutes must not become instantly due again.
            self._last_started[task.name] = time.time()

            started_at = datetime.now(timezone.utc).isoformat()
            run_id = await insert_scheduled_run(task.name, started_at)
            start_time = time.monotonic()

            try:
                async with _timeout(task.timeout_seconds):
                    result = await task.fn()

                duration_ms = (time.monotonic() - start_time) * 1000
                finished_at = datetime.now(timezone.utc).isoformat()
                # Reduce, never sever. `reduce_result_for_storage` returns
                # valid JSON at or under the cap in every case, including the
                # non-dict case the old `str(result)` branch used to store as
                # un-parseable Python repr.
                result_json = reduce_result_for_storage(result)
                if len(result_json) < len(_dumps(result if isinstance(result, dict)
                                                 else {"status": "success", "result": result})):
                    logger.debug("Task %s result reduced to %d chars", task.name, len(result_json))

                await update_scheduled_run(
                    run_id, "completed", finished_at, duration_ms,
                    result=result_json,
                )

                # Emit completion event
                try:
                    from naukri_server.events import event_bus, ScheduledTaskCompleted
                    await event_bus.emit(ScheduledTaskCompleted(
                        task_name=task.name,
                        duration_ms=duration_ms,
                        status="completed",
                    ))
                except Exception as e:
                    logger.warning("Failed to emit %s: %s", "ScheduledTaskCompleted", e)

                logger.info("Task %s completed in %.0fms", task.name, duration_ms)
                return result if isinstance(result, dict) else {"status": "success", "result": str(result)}

            except asyncio.TimeoutError:
                duration_ms = (time.monotonic() - start_time) * 1000
                finished_at = datetime.now(timezone.utc).isoformat()
                await update_scheduled_run(
                    run_id, "failed", finished_at, duration_ms,
                    error=f"Timeout after {task.timeout_seconds}s",
                )
                logger.warning("Task %s timed out after %.0fs", task.name, task.timeout_seconds)
                return {"status": "error", "message": f"Timeout after {task.timeout_seconds}s"}

            except Exception as exc:
                duration_ms = (time.monotonic() - start_time) * 1000
                finished_at = datetime.now(timezone.utc).isoformat()
                await update_scheduled_run(
                    run_id, "failed", finished_at, duration_ms,
                    error=str(exc)[:500],
                )

                # Emit failure event
                try:
                    from naukri_server.events import event_bus, ScheduledTaskCompleted
                    await event_bus.emit(ScheduledTaskCompleted(
                        task_name=task.name,
                        duration_ms=duration_ms,
                        status="failed",
                        error=str(exc)[:200],
                    ))
                except Exception as e:
                    logger.warning("Failed to emit %s: %s", "ScheduledTaskCompleted", e)

                logger.warning("Task %s failed: %s", task.name, exc)
                return {"status": "error", "message": str(exc)}


# Module-level singleton — set by lifespan in __init__.py
scheduler: Optional[TaskScheduler] = None
