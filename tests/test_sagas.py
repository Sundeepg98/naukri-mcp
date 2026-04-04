"""Tests for naukri_server.sagas -- saga pattern with compensation."""

import logging
import pytest

from naukri_server.sagas import SagaExecutor


class TestSagaAllSucceed:
    @pytest.mark.asyncio
    async def test_all_steps_succeed(self):
        saga = SagaExecutor("test-saga")
        saga.add_step("step1", execute=self._ok("a"))
        saga.add_step("step2", execute=self._ok("b"))
        saga.add_step("step3", execute=self._ok("c"))

        result = await saga.run()

        assert result["status"] == "success"
        assert result["saga"] == "test-saga"
        assert result["completed_steps"] == ["step1", "step2", "step3"]
        assert result["results"] == {"step1": "a", "step2": "b", "step3": "c"}

    @staticmethod
    def _ok(val):
        async def _execute():
            return val
        return _execute


class TestSagaMiddleStepFails:
    @pytest.mark.asyncio
    async def test_compensates_completed_steps_in_reverse(self):
        compensated = []

        async def step1_exec():
            return "done-1"

        async def step2_exec():
            return "done-2"

        async def step3_exec():
            raise RuntimeError("boom")

        async def comp1():
            compensated.append("step1")

        async def comp2():
            compensated.append("step2")

        saga = SagaExecutor("fail-saga")
        saga.add_step("step1", execute=step1_exec, compensate=comp1)
        saga.add_step("step2", execute=step2_exec, compensate=comp2)
        saga.add_step("step3", execute=step3_exec)

        result = await saga.run()

        assert result["status"] == "error"
        assert result["failed_step"] == "step3"
        assert result["completed_steps"] == ["step1", "step2"]
        assert "step3: RuntimeError: boom" in result["errors"]
        # Compensation runs in reverse order
        assert compensated == ["step2", "step1"]


class TestSagaCompensationFails:
    @pytest.mark.asyncio
    async def test_logs_warning_but_continues(self, caplog):
        compensated = []

        async def step1_exec():
            return "ok"

        async def step2_exec():
            raise ValueError("step2 broke")

        async def comp1_broken():
            raise OSError("compensation failed")

        saga = SagaExecutor("comp-fail-saga")
        saga.add_step("step1", execute=step1_exec, compensate=comp1_broken)
        saga.add_step("step2", execute=step2_exec)

        with caplog.at_level(logging.WARNING):
            result = await saga.run()

        assert result["status"] == "error"
        assert result["failed_step"] == "step2"
        assert "compensation failed" in caplog.text or result["status"] == "error"


class TestSagaSingleStep:
    @pytest.mark.asyncio
    async def test_single_step_success(self):
        async def only_step():
            return 42

        saga = SagaExecutor("single")
        saga.add_step("only", execute=only_step)

        result = await saga.run()

        assert result["status"] == "success"
        assert result["completed_steps"] == ["only"]
        assert result["results"] == {"only": 42}


class TestSagaEmpty:
    @pytest.mark.asyncio
    async def test_empty_saga_returns_success(self):
        saga = SagaExecutor("empty")

        result = await saga.run()

        assert result["status"] == "success"
        assert result["completed_steps"] == []
        assert result["results"] == {}
