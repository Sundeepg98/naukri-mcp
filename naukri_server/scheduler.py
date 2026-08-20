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
        while self._running:
            try:
                # For daily tasks with run_at_hour, calculate sleep to next target
                if interval >= 86400:
                    sleep_time = self._seconds_until_next_run(tasks)
                    if sleep_time > 0:
                        await asyncio.sleep(min(sleep_time, 3600))  # Check every hour max
                    else:
                        await asyncio.sleep(60)  # Check again in 1 min
                else:
                    await asyncio.sleep(interval)

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
                result_json = json.dumps(result) if isinstance(result, dict) else str(result)
                if isinstance(result, dict) and len(json.dumps(result)) > 2000:
                    logger.debug("Task %s result truncated from %d to 2000 chars", task.name, len(json.dumps(result)))

                await update_scheduled_run(
                    run_id, "completed", finished_at, duration_ms,
                    result=result_json[:2000],  # Cap result size
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
