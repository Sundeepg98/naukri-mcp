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
                    "due_now": self.is_due(t),
                    "seconds_since_last_start": (
                        None if name not in self._last_started
                        else round(time.time() - self._last_started[name], 1)
                    ),
                }
                for name, t in self._tasks.items()
            },
        }

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
        """Has interval_seconds actually elapsed since this task last started?

        An unseeded task counts as due. prime_schedule() seeds every
        non-catch_up task with "now" so the historical sleep-then-run cadence
        is preserved exactly; catch_up tasks are seeded from their persisted
        last run, or left unseeded (= due) when there is none.
        """
        last = self._last_started.get(task.name)
        if last is None:
            return True
        return (now if now is not None else time.time()) - last >= task.interval_seconds

    def prime_schedule(self, last_started: Optional[dict] = None) -> None:
        """Seed the interval clock before the loops start.

        `last_started` maps task name -> epoch seconds and wins over defaults.
        """
        now = time.time()
        provided = last_started or {}
        for name, task in self._tasks.items():
            if name in provided:
                self._last_started[name] = provided[name]
            elif task.catch_up:
                # Unseeded == due on the first wake.
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
            if not task.catch_up:
                continue
            try:
                from naukri_server.database import get_last_run
                ts = self._epoch_from_run_row(await get_last_run(name))
            except Exception as exc:
                logger.warning("Could not read last run for %s: %s", name, exc)
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
                if first_wake and self._bucket_has_overdue_catch_up(tasks):
                    # An opted-in task is ALREADY overdue. Sleeping a whole
                    # interval here is what made 4h and 6h tasks never run at
                    # all: the server does not live that long, so the first
                    # wake never arrived. Use a short startup grace instead.
                    logger.info(
                        "Scheduler bucket %ss: overdue catch-up task present, "
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
                    if task.run_at_hour is not None and not self._is_target_hour(task.run_at_hour):
                        logger.debug("Task %s: not target hour (want %d, now %d)", task.name, task.run_at_hour, datetime.now(IST).hour)
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

    def _bucket_has_overdue_catch_up(self, tasks: list[ScheduledTask]) -> bool:
        """Is any enabled catch_up task in this bucket already due?"""
        return any(t.enabled and t.catch_up and self.is_due(t) for t in tasks)

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

    def _is_target_hour(self, hour: int) -> bool:
        """Check if current IST hour matches target."""
        now_ist = datetime.now(IST)
        return now_ist.hour == hour

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
