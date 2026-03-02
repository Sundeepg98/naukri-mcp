"""Tests for naukri_server.api — helper functions, decorator, and session management."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_server.api import (
    _raise_api_error,
    api_tool,
    get_session,
    NaukriAPIError,
)
import naukri_server.api as api_module


# ---------------------------------------------------------------------------
# _raise_api_error
# ---------------------------------------------------------------------------

class TestRaiseApiError:
    """Tests for _raise_api_error: JSON parsing, HTML detection, fallback."""

    def test_json_with_message(self):
        """Top-level 'message' field is extracted."""
        body = json.dumps({"message": "Profile not found"})
        with pytest.raises(NaukriAPIError) as exc_info:
            _raise_api_error(404, body)
        assert exc_info.value.status == 404
        assert exc_info.value.message == "Profile not found"

    def test_json_with_nested_error_message(self):
        """Nested 'error.message' field is extracted when top-level absent."""
        body = json.dumps({"error": {"message": "Token expired", "code": "INVALID_TOKEN"}})
        with pytest.raises(NaukriAPIError) as exc_info:
            _raise_api_error(401, body)
        assert exc_info.value.message == "Token expired"
        assert exc_info.value.code == "INVALID_TOKEN"

    def test_json_with_code_extraction(self):
        """Top-level 'code' field is extracted."""
        body = json.dumps({"message": "Rate limited", "code": "RATE_LIMIT"})
        with pytest.raises(NaukriAPIError) as exc_info:
            _raise_api_error(429, body)
        assert exc_info.value.code == "RATE_LIMIT"

    def test_html_response_detection(self):
        """HTML body (<!doctype) produces a human-friendly message."""
        body = "<!doctype html><html><body>Server Error</body></html>"
        with pytest.raises(NaukriAPIError) as exc_info:
            _raise_api_error(500, body)
        assert "non-JSON response" in exc_info.value.message
        assert "500" in exc_info.value.message

    def test_malformed_json_fallback(self):
        """Non-JSON, non-HTML body falls back to raw text (truncated at 500)."""
        body = "some plain text error"
        with pytest.raises(NaukriAPIError) as exc_info:
            _raise_api_error(502, body)
        assert exc_info.value.message == body
        assert exc_info.value.code is None


# ---------------------------------------------------------------------------
# api_tool decorator
# ---------------------------------------------------------------------------

class TestApiToolDecorator:
    """Tests for the @api_tool() decorator error-handling wrapper."""

    @pytest.mark.asyncio
    async def test_naukri_api_error_401_returns_auth_error(self):
        """NaukriAPIError with status 401 maps to error_code AUTH_ERROR."""
        @api_tool("Test Context")
        async def failing():
            raise NaukriAPIError(401, "Session expired")

        result = await failing()
        assert result["status"] == "error"
        assert result["error_code"] == "AUTH_ERROR"
        assert result["http_status"] == 401

    @pytest.mark.asyncio
    async def test_naukri_api_error_non_401_returns_api_error(self):
        """NaukriAPIError with non-401 status maps to error_code API_ERROR."""
        @api_tool("Test Context")
        async def failing():
            raise NaukriAPIError(500, "Internal server error")

        result = await failing()
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 500

    @pytest.mark.asyncio
    async def test_generic_exception_uses_context_label(self):
        """Generic Exception uses the context label in the message."""
        @api_tool("Fetch Jobs")
        async def failing():
            raise ValueError("unexpected value")

        result = await failing()
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Fetch Jobs failed" in result["message"]
        assert "ValueError" in result["message"]

    @pytest.mark.asyncio
    async def test_generic_exception_fallback_label(self):
        """Without explicit context, label is derived from function name."""
        @api_tool()
        async def naukri_search_jobs():
            raise RuntimeError("boom")

        result = await naukri_search_jobs()
        assert "Search Jobs" in result["message"]


# ---------------------------------------------------------------------------
# get_session — double-checked locking
# ---------------------------------------------------------------------------

class TestGetSession:
    """Tests for get_session: lazy creation and double-checked locking."""

    def setup_method(self):
        """Reset module-level _session before each test."""
        api_module._session = None

    def teardown_method(self):
        """Clean up module-level _session after each test."""
        api_module._session = None

    @pytest.mark.asyncio
    async def test_creates_session_when_none(self):
        """First call creates a new ClientSession."""
        mock_connector = MagicMock()
        mock_session = MagicMock(spec=["closed"])
        mock_session.closed = False

        with patch("naukri_server.api.aiohttp.TCPConnector", return_value=mock_connector) as mock_tcp, \
             patch("naukri_server.api.aiohttp.ClientSession", return_value=mock_session) as mock_cs:
            session = await get_session()

        assert session is mock_session
        mock_tcp.assert_called_once()
        mock_cs.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_existing_when_alive(self):
        """Second call returns the same session when it is still open."""
        mock_session = MagicMock(spec=["closed"])
        mock_session.closed = False
        api_module._session = mock_session

        with patch("naukri_server.api.aiohttp.ClientSession") as mock_cs:
            session = await get_session()

        assert session is mock_session
        mock_cs.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_new_when_closed(self):
        """If existing session is closed, a new one is created."""
        old_session = MagicMock(spec=["closed"])
        old_session.closed = True
        api_module._session = old_session

        new_session = MagicMock(spec=["closed"])
        new_session.closed = False

        with patch("naukri_server.api.aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("naukri_server.api.aiohttp.ClientSession", return_value=new_session):
            session = await get_session()

        assert session is new_session
        assert session is not old_session
