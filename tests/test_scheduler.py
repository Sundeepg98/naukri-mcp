"""Tests for scheduler module — TaskScheduler, task definitions, MCP tool, and DB CRUD.

Every test is PURE: no network, no browser, no file I/O (except tmp_path for DB tests).
We mock all async helpers and verify scheduler lifecycle, task execution, tool dispatch,
and database persistence.
"""

import asyncio
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from naukri_server.scheduler import TaskScheduler, ScheduledTask


# =====================================================================
# Helpers
# =====================================================================


async def _init_db_in(tmp: Path):
    """Run init_db with DB_PATH pointed at tmp directory."""
    db_path = tmp / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()
    return db_path


# =====================================================================
# 1. ScheduledTask dataclass tests
# =====================================================================


class TestScheduledTaskDataclass:
    """Tests for ScheduledTask dataclass — default values and construction."""

    def test_scheduled_task_defaults(self):
        """ScheduledTask should have enabled=True, timeout=120, run_at_hour=None by default."""
        async def _fn():
            return {}

        task = ScheduledTask(name="test", fn=_fn, interval_seconds=3600)
        assert task.enabled is True
        assert task.timeout_seconds == 120.0
        assert task.run_at_hour is None
        assert task.description == ""

    def test_scheduled_task_with_run_at_hour(self):
        """ScheduledTask should store run_at_hour when provided."""
        async def _fn():
            return {}

        task = ScheduledTask(name="daily", fn=_fn, interval_seconds=86400, run_at_hour=8)
        assert task.run_at_hour == 8
        assert task.interval_seconds == 86400


# =====================================================================
# 2. TaskScheduler core tests
# =====================================================================


@pytest.fixture
def scheduler():
    return TaskScheduler()


@pytest.fixture
def sample_task():
    async def _fn():
        return {"status": "success", "count": 42}

    return ScheduledTask(
        name="test_task",
        fn=_fn,
        interval_seconds=3600,
        description="Test task for unit tests",
    )


class TestTaskSchedulerCore:
    """Tests for TaskScheduler — lifecycle, registration, enable/disable, run_now."""

    async def test_scheduler_start_stop(self, scheduler, sample_task):
        """start() sets _running=True and creates asyncio tasks; stop() cancels them."""
        scheduler.register(sample_task)
        await scheduler.start()

        assert scheduler._running is True
        assert len(scheduler._asyncio_tasks) > 0

        await scheduler.stop()
        assert scheduler._running is False
        assert len(scheduler._asyncio_tasks) == 0

    async def test_scheduler_register(self, scheduler, sample_task):
        """register() adds tasks that appear in status."""
        scheduler.register(sample_task)

        assert "test_task" in scheduler._tasks
        assert scheduler.status["total_tasks"] == 1
        assert "test_task" in scheduler.status["tasks"]

    async def test_scheduler_status_property(self, scheduler, sample_task):
        """status dict should have running, total_tasks, enabled_tasks, disabled_tasks, tasks."""
        scheduler.register(sample_task)
        status = scheduler.status

        assert "running" in status
        assert "total_tasks" in status
        assert "enabled_tasks" in status
        assert "disabled_tasks" in status
        assert "tasks" in status
        assert status["running"] is False
        assert status["total_tasks"] == 1
        assert status["enabled_tasks"] == 1
        assert status["disabled_tasks"] == 0

        # Verify task detail fields
        task_info = status["tasks"]["test_task"]
        assert task_info["enabled"] is True
        assert task_info["interval_seconds"] == 3600
        assert task_info["run_at_hour"] is None
        assert task_info["description"] == "Test task for unit tests"

    async def test_scheduler_enable_disable(self, scheduler, sample_task):
        """enable_task/disable_task toggle the enabled flag and return True."""
        scheduler.register(sample_task)

        assert scheduler.disable_task("test_task") is True
        assert scheduler._tasks["test_task"].enabled is False
        assert scheduler.status["enabled_tasks"] == 0
        assert scheduler.status["disabled_tasks"] == 1

        assert scheduler.enable_task("test_task") is True
        assert scheduler._tasks["test_task"].enabled is True
        assert scheduler.status["enabled_tasks"] == 1

    async def test_scheduler_enable_unknown_returns_false(self, scheduler):
        """enable_task for a non-existent task returns False."""
        assert scheduler.enable_task("nonexistent") is False

    async def test_scheduler_disable_unknown_returns_false(self, scheduler):
        """disable_task for a non-existent task returns False."""
        assert scheduler.disable_task("nonexistent") is False

    async def test_scheduler_run_now_unknown_task(self, scheduler):
        """run_now with unknown name returns error dict."""
        result = await scheduler.run_now("nonexistent")
        assert result["status"] == "error"
        assert "Unknown task" in result["message"]

    async def test_scheduler_run_now_executes_task(self, scheduler, sample_task):
        """run_now calls the task function and logs to DB."""
        scheduler.register(sample_task)

        with patch("naukri_server.database.insert_scheduled_run", new_callable=AsyncMock, return_value=1), \
             patch("naukri_server.database.update_scheduled_run", new_callable=AsyncMock):
            result = await scheduler.run_now("test_task")

        assert result["status"] == "success"
        assert result["count"] == 42

    async def test_scheduler_execute_task_timeout(self, scheduler):
        """Task exceeding timeout is handled gracefully with error result."""
        async def _slow_fn():
            await asyncio.sleep(10)
            return {"status": "success"}

        task = ScheduledTask(
            name="slow_task", fn=_slow_fn, interval_seconds=3600,
            timeout_seconds=0.01,  # Very short timeout for testing
        )
        scheduler.register(task)

        with patch("naukri_server.database.insert_scheduled_run", new_callable=AsyncMock, return_value=1), \
             patch("naukri_server.database.update_scheduled_run", new_callable=AsyncMock) as mock_update:
            result = await scheduler.run_now("slow_task")

        assert result["status"] == "error"
        assert "Timeout" in result["message"]
        # Verify the DB was updated with failed status
        mock_update.assert_awaited_once()
        call_args = mock_update.call_args
        assert call_args[0][1] == "failed"  # status

    async def test_scheduler_execute_task_failure(self, scheduler):
        """Task raising an exception is caught and logged with error result."""
        async def _failing_fn():
            raise ValueError("Something broke")

        task = ScheduledTask(
            name="fail_task", fn=_failing_fn, interval_seconds=3600,
        )
        scheduler.register(task)

        with patch("naukri_server.database.insert_scheduled_run", new_callable=AsyncMock, return_value=1), \
             patch("naukri_server.database.update_scheduled_run", new_callable=AsyncMock) as mock_update:
            result = await scheduler.run_now("fail_task")

        assert result["status"] == "error"
        assert "Something broke" in result["message"]
        # Verify the DB was updated with failed status
        mock_update.assert_awaited_once()
        call_args = mock_update.call_args
        assert call_args[0][1] == "failed"

    async def test_window_start_is_the_most_recent_opening(self, scheduler):
        """Replaced test_is_target_hour, which covered a helper the loop no
        longer calls. The hour gate was an equality test on the current hour;
        it is now a day window, and _window_start is the piece that decides
        which day a window belongs to."""
        from datetime import datetime, timezone, timedelta

        IST = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(IST)

        async def _fn():
            return {}

        # An hour that has already passed today opens TODAY.
        past = ScheduledTask(name="past", fn=_fn, interval_seconds=86400,
                             run_at_hour=(now_ist.hour - 2) % 24)
        # An hour still ahead of us today opens YESTERDAY.
        future = ScheduledTask(name="future", fn=_fn, interval_seconds=86400,
                               run_at_hour=(now_ist.hour + 2) % 24)

        assert scheduler._window_start(past) <= now_ist
        assert scheduler._window_start(future) <= now_ist
        assert (now_ist - scheduler._window_start(future)) < timedelta(days=1)

    async def test_scheduler_start_groups_by_interval(self, scheduler):
        """start() groups tasks into interval buckets — same interval = same asyncio task."""
        async def _fn():
            return {}

        task1 = ScheduledTask(name="t1", fn=_fn, interval_seconds=3600)
        task2 = ScheduledTask(name="t2", fn=_fn, interval_seconds=3600)
        task3 = ScheduledTask(name="t3", fn=_fn, interval_seconds=7200)

        scheduler.register(task1)
        scheduler.register(task2)
        scheduler.register(task3)

        await scheduler.start()
        # 2 interval buckets: 3600 and 7200
        assert len(scheduler._asyncio_tasks) == 2
        await scheduler.stop()

    async def test_scheduler_start_skips_disabled_tasks(self, scheduler):
        """start() should not create asyncio tasks for disabled tasks."""
        async def _fn():
            return {}

        task = ScheduledTask(name="disabled_task", fn=_fn, interval_seconds=3600, enabled=False)
        scheduler.register(task)

        await scheduler.start()
        # No enabled tasks, so no interval buckets
        assert len(scheduler._asyncio_tasks) == 0
        await scheduler.stop()


# =====================================================================
# 3. Task definitions tests (scheduler_tasks.py)
# =====================================================================


class TestTaskDefinitions:
    """Tests for scheduler_tasks.py — task registration and definitions."""

    def test_register_all_registers_9_tasks(self):
        """register_all puts 11 tasks into the scheduler.

        History: 8 original + agent_cycle = 9, then + t2_verify_pending = 10
        when the self-healing pipeline was wired (the verify task drives the
        T2 snapshot+revert safety net), then + retention_sweep = 11 on
        2026-08-21 (cleanup_old_records existed but had no caller, so the
        retention horizon was never applied to any table).
        """
        from naukri_server.scheduler_tasks import register_all

        sched = TaskScheduler()
        register_all(sched)

        assert len(sched._tasks) == 11
        assert "t2_verify_pending" in sched._tasks
        assert "retention_sweep" in sched._tasks

    def test_task_definitions_have_valid_intervals(self):
        """All task definitions should have interval_seconds > 0."""
        from naukri_server.scheduler_tasks import TASK_DEFINITIONS

        for task_def in TASK_DEFINITIONS:
            assert task_def.interval_seconds > 0, f"Task {task_def.name} has invalid interval"

    def test_task_definitions_have_descriptions(self):
        """All task definitions should have non-empty descriptions."""
        from naukri_server.scheduler_tasks import TASK_DEFINITIONS

        for task_def in TASK_DEFINITIONS:
            assert task_def.description, f"Task {task_def.name} has empty description"

    def test_task_names_are_unique(self):
        """All 8 tasks should have unique names."""
        from naukri_server.scheduler_tasks import TASK_DEFINITIONS

        names = [t.name for t in TASK_DEFINITIONS]
        assert len(names) == len(set(names))

    async def test_task_sync_applications_calls_service(self):
        """_task_sync_applications should call the _sync_applications service."""
        from naukri_server.scheduler_tasks import _task_sync_applications

        with patch("naukri_server.tools.sync._sync_applications", new_callable=AsyncMock,
                   return_value={"synced": 5}) as mock_sync:
            result = await _task_sync_applications()

        mock_sync.assert_awaited_once()
        assert result == {"synced": 5}

    async def test_task_daily_brief_calls_service(self):
        """_task_daily_brief should call naukri_daily_brief."""
        from naukri_server.scheduler_tasks import _task_daily_brief

        with patch("naukri_server.tools.daily_brief.naukri_daily_brief", new_callable=AsyncMock,
                   return_value={"status": "success", "brief": "..."}) as mock_brief:
            result = await _task_daily_brief()

        mock_brief.assert_awaited_once()
        assert result["status"] == "success"

    async def test_task_stale_check_calls_service(self):
        """_task_stale_check should call get_stale_applications with correct params."""
        from naukri_server.scheduler_tasks import _task_stale_check

        with patch("naukri_server.services.application_service.get_stale_applications",
                   new_callable=AsyncMock,
                   return_value={"stale_count": 3}) as mock_stale:
            result = await _task_stale_check()

        # emit_events=True since 2026-08-21: this scheduled task is the ONLY
        # caller allowed to mint ApplicationStale notifications. The four read
        # callers (the MCP tool, the daily brief, follow-up priority, the HTTP
        # dashboard) all leave it False. See tests/test_read_path_purity.py.
        mock_stale.assert_awaited_once_with(days_threshold=14, min_stale_score=40,
                                            emit_events=True)
        assert result == {"stale_count": 3}

    async def test_task_boost_profile_calls_service(self):
        """_task_boost_profile should call _boost_visibility with randomize=False."""
        from naukri_server.scheduler_tasks import _task_boost_profile

        with patch("naukri_server.tools.profile_update._boost_visibility",
                   new_callable=AsyncMock,
                   return_value={"status": "success"}) as mock_boost:
            result = await _task_boost_profile()

        mock_boost.assert_awaited_once_with(randomize=False)
        assert result["status"] == "success"

    async def test_task_reminder_check_calls_service(self):
        """_task_reminder_check should call _list_reminders with include_past=True.

        emit_events=True since 2026-08-21: _list_reminders no longer emits on
        read, and this hourly task is the one path that is meant to notify him.
        See tests/test_reminder_storm.py.
        """
        from naukri_server.scheduler_tasks import _task_reminder_check

        with patch("naukri_server.tools.reminders._list_reminders",
                   new_callable=AsyncMock,
                   return_value={"due_count": 2, "reminders": []}) as mock_rem:
            result = await _task_reminder_check()

        mock_rem.assert_awaited_once_with(include_past=True, emit_events=True)
        assert result["due_count"] == 2


# =====================================================================
# 4. Scheduler tool tests (scheduler_tool.py)
# =====================================================================


class TestSchedulerTool:
    """Tests for the naukri_scheduler MCP tool — action dispatch and error handling."""

    async def test_scheduler_tool_status(self):
        """status action should return scheduler status with last run info."""
        from naukri_server.tools.scheduler_tool import _scheduler_status

        mock_sched = MagicMock()
        mock_sched.status = {
            "running": True,
            "total_tasks": 8,
            "enabled_tasks": 8,
            "disabled_tasks": 0,
            "tasks": {"sync_applications": {"enabled": True, "interval_seconds": 14400}},
        }

        with patch("naukri_server.tools.scheduler_tool._get_scheduler", return_value=mock_sched), \
             patch("naukri_server.database.get_last_run", new_callable=AsyncMock, return_value=None):
            result = await _scheduler_status()

        assert result["status"] == "success"
        assert result["running"] is True
        assert result["total_tasks"] == 8

    async def test_scheduler_tool_enable(self):
        """enable action should call enable_task on the scheduler."""
        from naukri_server.tools.scheduler_tool import _scheduler_enable

        mock_sched = MagicMock()
        mock_sched.enable_task.return_value = True

        with patch("naukri_server.tools.scheduler_tool._get_scheduler", return_value=mock_sched):
            result = await _scheduler_enable("sync_applications")

        mock_sched.enable_task.assert_called_once_with("sync_applications")
        assert result["status"] == "success"
        assert "enabled" in result["message"]

    async def test_scheduler_tool_enable_unknown(self):
        """enable with unknown task returns error with NOT_FOUND."""
        from naukri_server.tools.scheduler_tool import _scheduler_enable

        mock_sched = MagicMock()
        mock_sched.enable_task.return_value = False

        with patch("naukri_server.tools.scheduler_tool._get_scheduler", return_value=mock_sched):
            result = await _scheduler_enable("nonexistent")

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"

    async def test_scheduler_tool_disable(self):
        """disable action should call disable_task on the scheduler."""
        from naukri_server.tools.scheduler_tool import _scheduler_disable

        mock_sched = MagicMock()
        mock_sched.disable_task.return_value = True

        with patch("naukri_server.tools.scheduler_tool._get_scheduler", return_value=mock_sched):
            result = await _scheduler_disable("daily_brief")

        mock_sched.disable_task.assert_called_once_with("daily_brief")
        assert result["status"] == "success"
        assert "disabled" in result["message"]

    async def test_scheduler_tool_run_now(self):
        """run_now action should call run_now on the scheduler and return result."""
        from naukri_server.tools.scheduler_tool import _scheduler_run_now

        mock_sched = MagicMock()
        mock_sched.run_now = AsyncMock(return_value={"status": "success", "synced": 10})

        with patch("naukri_server.tools.scheduler_tool._get_scheduler", return_value=mock_sched):
            result = await _scheduler_run_now("sync_applications")

        mock_sched.run_now.assert_awaited_once_with("sync_applications")
        assert result["status"] == "success"
        assert result["synced"] == 10

    async def test_scheduler_tool_history(self):
        """history action should call list_scheduled_runs and return results."""
        from naukri_server.tools.scheduler_tool import _scheduler_history

        mock_runs = [
            {"task_name": "sync_applications", "started_at": "2026-04-05T10:00:00", "status": "completed"},
            {"task_name": "daily_brief", "started_at": "2026-04-05T08:00:00", "status": "completed"},
        ]

        with patch("naukri_server.database.list_scheduled_runs",
                   new_callable=AsyncMock, return_value=mock_runs):
            result = await _scheduler_history(task_name=None, limit=20)

        assert result["status"] == "success"
        assert result["total"] == 2
        assert len(result["runs"]) == 2

    async def test_scheduler_tool_history_filtered(self):
        """history with task_name should pass filter to list_scheduled_runs."""
        from naukri_server.tools.scheduler_tool import _scheduler_history

        mock_runs = [
            {"task_name": "daily_brief", "started_at": "2026-04-05T08:00:00", "status": "completed"},
        ]

        with patch("naukri_server.database.list_scheduled_runs",
                   new_callable=AsyncMock, return_value=mock_runs) as mock_list:
            result = await _scheduler_history(task_name="daily_brief", limit=10)

        mock_list.assert_awaited_once_with(task_name="daily_brief", limit=10)
        assert result["total"] == 1

    async def test_scheduler_tool_not_initialized(self):
        """When scheduler singleton is None, _get_scheduler should raise RuntimeError."""
        from naukri_server.tools.scheduler_tool import _get_scheduler

        with patch("naukri_server.tools.scheduler_tool._mod", create=True) as mock_mod:
            # Simulate: import naukri_server.scheduler as _mod; _mod.scheduler is None
            pass

        # Directly test _get_scheduler with scheduler=None
        with patch("naukri_server.scheduler.scheduler", None):
            with pytest.raises(RuntimeError, match="Scheduler not initialized"):
                _get_scheduler()


# =====================================================================
# 5. Database CRUD tests (scheduled_runs table)
# =====================================================================


class TestScheduledRunsDB:
    """Tests for scheduled_runs table CRUD operations using a temp database."""

    async def test_insert_scheduled_run(self, tmp_path):
        """insert_scheduled_run should return a valid lastrowid."""
        db_path = await _init_db_in(tmp_path)

        with patch("naukri_server.database.DB_PATH", db_path):
            from naukri_server.database import insert_scheduled_run
            run_id = await insert_scheduled_run("test_task", "2026-04-05T10:00:00")

        assert isinstance(run_id, int)
        assert run_id > 0

    async def test_update_scheduled_run(self, tmp_path):
        """update_scheduled_run should update status, finished_at, duration_ms, and result."""
        db_path = await _init_db_in(tmp_path)

        with patch("naukri_server.database.DB_PATH", db_path):
            from naukri_server.database import insert_scheduled_run, update_scheduled_run, get_last_run

            run_id = await insert_scheduled_run("test_task", "2026-04-05T10:00:00")
            await update_scheduled_run(
                run_id, "completed", "2026-04-05T10:01:00", 60000.0,
                result='{"synced": 5}',
            )

            row = await get_last_run("test_task")

        assert row is not None
        assert row["status"] == "completed"
        assert row["finished_at"] == "2026-04-05T10:01:00"
        assert row["duration_ms"] == 60000.0
        assert row["result"] == '{"synced": 5}'
        assert row["error"] is None

    async def test_update_scheduled_run_with_error(self, tmp_path):
        """update_scheduled_run should store error field on failure."""
        db_path = await _init_db_in(tmp_path)

        with patch("naukri_server.database.DB_PATH", db_path):
            from naukri_server.database import insert_scheduled_run, update_scheduled_run, get_last_run

            run_id = await insert_scheduled_run("fail_task", "2026-04-05T10:00:00")
            await update_scheduled_run(
                run_id, "failed", "2026-04-05T10:00:05", 5000.0,
                error="Timeout after 120s",
            )

            row = await get_last_run("fail_task")

        assert row["status"] == "failed"
        assert row["error"] == "Timeout after 120s"

    async def test_get_last_run(self, tmp_path):
        """get_last_run should return the most recent run for a task."""
        db_path = await _init_db_in(tmp_path)

        with patch("naukri_server.database.DB_PATH", db_path):
            from naukri_server.database import insert_scheduled_run, update_scheduled_run, get_last_run

            # Insert 2 runs — the second is more recent
            run1 = await insert_scheduled_run("test_task", "2026-04-05T08:00:00")
            await update_scheduled_run(run1, "completed", "2026-04-05T08:01:00", 60000.0)

            run2 = await insert_scheduled_run("test_task", "2026-04-05T12:00:00")
            await update_scheduled_run(run2, "completed", "2026-04-05T12:01:00", 55000.0)

            row = await get_last_run("test_task")

        assert row is not None
        assert row["started_at"] == "2026-04-05T12:00:00"

    async def test_get_last_run_not_found(self, tmp_path):
        """get_last_run should return None for unknown task."""
        db_path = await _init_db_in(tmp_path)

        with patch("naukri_server.database.DB_PATH", db_path):
            from naukri_server.database import get_last_run
            row = await get_last_run("nonexistent_task")

        assert row is None

    async def test_list_scheduled_runs_all(self, tmp_path):
        """list_scheduled_runs without filter returns all runs ordered by started_at DESC."""
        db_path = await _init_db_in(tmp_path)

        with patch("naukri_server.database.DB_PATH", db_path):
            from naukri_server.database import insert_scheduled_run, update_scheduled_run, list_scheduled_runs

            r1 = await insert_scheduled_run("task_a", "2026-04-05T08:00:00")
            await update_scheduled_run(r1, "completed", "2026-04-05T08:01:00", 60000.0)

            r2 = await insert_scheduled_run("task_b", "2026-04-05T10:00:00")
            await update_scheduled_run(r2, "completed", "2026-04-05T10:01:00", 50000.0)

            r3 = await insert_scheduled_run("task_a", "2026-04-05T12:00:00")
            await update_scheduled_run(r3, "failed", "2026-04-05T12:00:05", 5000.0, error="crash")

            runs = await list_scheduled_runs()

        assert len(runs) == 3
        # Most recent first
        assert runs[0]["started_at"] == "2026-04-05T12:00:00"
        assert runs[0]["task_name"] == "task_a"
        assert runs[1]["started_at"] == "2026-04-05T10:00:00"
        assert runs[2]["started_at"] == "2026-04-05T08:00:00"

    async def test_list_scheduled_runs_filtered(self, tmp_path):
        """list_scheduled_runs with task_name should return only matching runs."""
        db_path = await _init_db_in(tmp_path)

        with patch("naukri_server.database.DB_PATH", db_path):
            from naukri_server.database import insert_scheduled_run, list_scheduled_runs

            await insert_scheduled_run("task_a", "2026-04-05T08:00:00")
            await insert_scheduled_run("task_b", "2026-04-05T09:00:00")
            await insert_scheduled_run("task_a", "2026-04-05T10:00:00")

            runs = await list_scheduled_runs(task_name="task_a")

        assert len(runs) == 2
        assert all(r["task_name"] == "task_a" for r in runs)
