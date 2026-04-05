"""Chaos tests -- verify graceful degradation under failure conditions.

All tests are PURE: no network, no browser, no file I/O.
Tests simulate timeouts, crashes, and partial failures.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.sagas import SagaExecutor
from naukri_server.events import EventBus, ApplicationSubmitted, ApplicationStatusChanged


class TestApiTimeoutDuringApply:
    """Chaos: API times out mid-apply -- verify graceful failure."""

    @pytest.mark.asyncio
    async def test_api_timeout_returns_error(self):
        """When apply API hangs, the saga step times out gracefully."""

        async def slow_apply():
            await asyncio.sleep(10)  # will be killed by timeout
            return {"status": "applied"}

        saga = SagaExecutor("apply_timeout")
        saga.add_step("apply", slow_apply, timeout=0.05)  # 50ms timeout

        result = await saga.run()

        assert result["status"] == "error"
        assert result["failed_step"] == "apply"
        assert any("timed out" in e for e in result["errors"])
        assert result["state"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_timeout_triggers_compensation(self):
        """When a step times out, earlier steps get compensated."""
        compensated = []

        async def step_one():
            return "saved"

        async def step_two():
            await asyncio.sleep(10)  # will timeout
            return "never"

        async def comp_one():
            compensated.append("unsaved")

        saga = SagaExecutor("timeout_compensation")
        saga.add_step("save", step_one, compensate=comp_one)
        saga.add_step("process", step_two, timeout=0.05)

        result = await saga.run()

        assert result["status"] == "error"
        assert result["failed_step"] == "process"
        assert "unsaved" in compensated

    @pytest.mark.asyncio
    async def test_timeout_default_30s_not_triggered_for_fast_step(self):
        """Fast steps complete well within the default 30s timeout."""

        async def fast_step():
            return "quick"

        saga = SagaExecutor("fast_saga")
        saga.add_step("fast", fast_step)  # default timeout=30

        result = await saga.run()

        assert result["status"] == "success"
        assert result["completed_steps"] == ["fast"]


class TestSagaStepTimeout:
    """Chaos: saga step exceeds timeout -- verify timeout error."""

    @pytest.mark.asyncio
    async def test_middle_step_timeout_preserves_earlier_results(self):
        """Step 2 times out, but step 1 result is preserved in state."""

        async def step_a():
            return {"data": "from_a"}

        async def step_b():
            await asyncio.sleep(10)
            return {"data": "from_b"}

        async def step_c():
            return {"data": "from_c"}

        saga = SagaExecutor("middle_timeout")
        saga.add_step("fetch", step_a)
        saga.add_step("process", step_b, timeout=0.05)
        saga.add_step("save", step_c)

        result = await saga.run()

        assert result["status"] == "error"
        assert result["failed_step"] == "process"
        assert result["completed_steps"] == ["fetch"]
        assert result["state"]["steps_completed"] == ["fetch"]

    @pytest.mark.asyncio
    async def test_custom_timeout_per_step(self):
        """Each step can have its own timeout value."""

        async def quick_step():
            return "fast"

        async def slow_step():
            await asyncio.sleep(10)
            return "slow"

        saga = SagaExecutor("custom_timeouts")
        saga.add_step("quick", quick_step, timeout=5.0)
        saga.add_step("slow", slow_step, timeout=0.05)

        result = await saga.run()

        assert result["status"] == "error"
        assert result["failed_step"] == "slow"
        # Quick step succeeded with its generous timeout
        assert "quick" in result["completed_steps"]

    @pytest.mark.asyncio
    async def test_timeout_error_message_includes_step_name(self):
        """Timeout error message is descriptive with step name and duration."""

        async def hanging_step():
            await asyncio.sleep(10)

        saga = SagaExecutor("descriptive_timeout")
        saga.add_step("data_fetch", hanging_step, timeout=0.05)

        result = await saga.run()

        assert result["status"] == "error"
        error_msg = result["errors"][0]
        assert "data_fetch" in error_msg
        assert "timed out" in error_msg


class TestEventSubscriberCrash:
    """Chaos: subscriber throws -- verify other subscribers still run."""

    @pytest.mark.asyncio
    async def test_crashing_subscriber_does_not_block_others(self):
        """When one subscriber crashes, other subscribers still execute."""
        test_bus = EventBus()
        results = []

        async def good_handler_1(event):
            results.append("handler_1_ok")

        async def crashing_handler(event):
            raise RuntimeError("subscriber exploded")

        async def good_handler_2(event):
            results.append("handler_2_ok")

        test_bus.subscribe(ApplicationSubmitted, good_handler_1)
        test_bus.subscribe(ApplicationSubmitted, crashing_handler)
        test_bus.subscribe(ApplicationSubmitted, good_handler_2)

        with patch("naukri_server.database.log_event", new_callable=AsyncMock):
            await test_bus.emit(ApplicationSubmitted(
                job_id="CRASH1", company="BoomCo", title="Dev"
            ))

        # Both good handlers ran despite the crash
        assert "handler_1_ok" in results
        assert "handler_2_ok" in results

    @pytest.mark.asyncio
    async def test_all_subscribers_crash_event_still_logged(self):
        """Even if all subscribers crash, the event is recorded in the log."""
        test_bus = EventBus()

        async def crash_1(event):
            raise ValueError("boom1")

        async def crash_2(event):
            raise TypeError("boom2")

        test_bus.subscribe(ApplicationSubmitted, crash_1)
        test_bus.subscribe(ApplicationSubmitted, crash_2)

        with patch("naukri_server.database.log_event", new_callable=AsyncMock):
            await test_bus.emit(ApplicationSubmitted(
                job_id="ALL_CRASH", company="FailCo", title="Dev"
            ))

        # Event should still be in the internal log
        recent = test_bus.get_log(ApplicationSubmitted, limit=5)
        assert len(recent) == 1
        assert recent[0].job_id == "ALL_CRASH"

    @pytest.mark.asyncio
    async def test_event_depth_guard_prevents_infinite_loop(self):
        """Subscriber that emits more events is capped by depth guard."""
        test_bus = EventBus()
        emit_count = []

        async def recursive_handler(event):
            emit_count.append(1)
            # Try to emit another event inside the handler
            await test_bus.emit(ApplicationSubmitted(
                job_id=f"RECURSE_{len(emit_count)}", company="Loop", title="Dev"
            ))

        test_bus.subscribe(ApplicationSubmitted, recursive_handler)

        with patch("naukri_server.database.log_event", new_callable=AsyncMock):
            await test_bus.emit(ApplicationSubmitted(
                job_id="START", company="Seed", title="Dev"
            ))

        # Depth guard (max_emit_depth=3) should cap total emissions
        assert len(emit_count) <= test_bus._max_emit_depth
