"""Tests for the shared error handler utility."""

import pytest
from unittest.mock import AsyncMock

from naukri_server.api import NaukriAPIError
from naukri_server.error_handler import handle_tool_action


@pytest.mark.asyncio
async def test_success_passes_through():
    """handle_tool_action returns the handler's result on success."""
    expected = {"status": "success", "data": [1, 2, 3]}
    handler = AsyncMock(return_value=expected)
    result = await handle_tool_action(handler, "test.success")
    assert result["status"] == "success"
    assert result["data"] == [1, 2, 3]
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_naukri_api_error_returns_api_error():
    """handle_tool_action catches NaukriAPIError and returns API_ERROR dict."""
    async def _failing():
        raise NaukriAPIError(status=403, message="Forbidden")

    result = await handle_tool_action(_failing, "test.api_error")
    assert result["status"] == "error"
    # 401/403 now classify as AUTH_ERROR, not API_ERROR. The same status
    # used to mean different things depending on which dispatch path a
    # tool used; a signed-out caller got no next step. Converted, not
    # deleted -- the case still matters, the expected code changed.
    assert result["error_code"] == "AUTH_ERROR"
    assert result["http_status"] == 403
    assert "Forbidden" in result["message"]


@pytest.mark.asyncio
async def test_generic_exception_returns_internal_error():
    """handle_tool_action catches unexpected exceptions and returns INTERNAL_ERROR dict."""
    async def _exploding():
        raise ValueError("something broke")

    result = await handle_tool_action(_exploding, "test.internal")
    assert result["status"] == "error"
    assert result["error_code"] == "INTERNAL_ERROR"
    assert "ValueError" in result["message"]
    assert "something broke" in result["message"]
