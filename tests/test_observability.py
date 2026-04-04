"""Tests for observability improvements -- correlation IDs, saga timings, log filters."""

import logging
import pytest
from unittest.mock import AsyncMock

from naukri_server.api import NaukriAPIError
from naukri_server.correlation import CorrelationFilter, set_request_id, get_request_id
from naukri_server.error_handler import handle_tool_action
from naukri_server.sagas import SagaExecutor


# ---------------------------------------------------------------------------
# Correlation ID tests
# ---------------------------------------------------------------------------


class TestCorrelationIdInResponse:
    @pytest.mark.asyncio
    async def test_success_response_includes_request_id(self):
        """handle_tool_action injects _request_id into successful dict responses."""
        handler = AsyncMock(return_value={"status": "success", "data": 42})
        result = await handle_tool_action(handler, "test.corr_ok")

        assert "_request_id" in result
        assert isinstance(result["_request_id"], str)
        assert len(result["_request_id"]) == 8  # uuid hex[:8]

    @pytest.mark.asyncio
    async def test_error_response_includes_request_id(self):
        """handle_tool_action injects _request_id into error responses."""
        async def _boom():
            raise ValueError("kaboom")

        result = await handle_tool_action(_boom, "test.corr_err")

        assert result["status"] == "error"
        assert result["error_code"] == "INTERNAL_ERROR"
        assert "_request_id" in result
        assert isinstance(result["_request_id"], str)
        assert len(result["_request_id"]) == 8

    @pytest.mark.asyncio
    async def test_api_error_response_includes_request_id(self):
        """handle_tool_action injects _request_id into NaukriAPIError responses."""
        async def _api_fail():
            raise NaukriAPIError(status=429, message="Rate limited")

        result = await handle_tool_action(_api_fail, "test.corr_api")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "_request_id" in result
        assert len(result["_request_id"]) == 8


# ---------------------------------------------------------------------------
# CorrelationFilter test
# ---------------------------------------------------------------------------


class TestCorrelationFilter:
    def test_filter_adds_request_id_to_log_record(self):
        """CorrelationFilter sets request_id attribute on log records."""
        rid = set_request_id("test1234")
        filt = CorrelationFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        assert filt.filter(record) is True
        assert record.request_id == "test1234"

    def test_filter_returns_empty_when_no_id_set(self):
        """CorrelationFilter returns empty string when no ID has been set."""
        # Reset to empty via a fresh context -- set_request_id always sets,
        # so we test the default by importing the contextvar directly.
        from naukri_server.correlation import _request_id
        token = _request_id.set('')
        try:
            filt = CorrelationFilter()
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg="hello", args=(), exc_info=None,
            )
            filt.filter(record)
            assert record.request_id == ''
        finally:
            _request_id.reset(token)


# ---------------------------------------------------------------------------
# Saga step_timings test
# ---------------------------------------------------------------------------


class TestSagaStepTimings:
    @pytest.mark.asyncio
    async def test_success_result_includes_step_timings(self):
        """Successful saga run includes step_timings dict with ms values."""
        async def fast_step():
            return "done"

        saga = SagaExecutor("timing-test")
        saga.add_step("s1", execute=fast_step)
        saga.add_step("s2", execute=fast_step)

        result = await saga.run()

        assert result["status"] == "success"
        assert "step_timings" in result
        assert set(result["step_timings"].keys()) == {"s1", "s2"}
        # Timings should be non-negative integers
        for name, ms in result["step_timings"].items():
            assert isinstance(ms, int)
            assert ms >= 0

    @pytest.mark.asyncio
    async def test_error_result_includes_step_timings_for_completed(self):
        """Failed saga includes step_timings only for steps that completed before failure."""
        async def ok_step():
            return "ok"

        async def bad_step():
            raise RuntimeError("fail")

        saga = SagaExecutor("timing-fail")
        saga.add_step("s1", execute=ok_step)
        saga.add_step("s2", execute=bad_step)

        result = await saga.run()

        assert result["status"] == "error"
        assert "step_timings" in result
        assert "s1" in result["step_timings"]
        assert "s2" not in result["step_timings"]  # failed step not in timings
