"""Error attribution - an expired session must never be reported as a browser fault.

Background (measured, not guessed): a live probe found the server's Playwright
browser had died and every tool failed. Detection was fine; ATTRIBUTION was
wrong on 16 of 18 failures. The REST-first paths in tools/search.py and
tools/jobs.py swallowed EVERY exception at INFO level and then entered a
browser fallback that cannot succeed without a token, so the caller saw a
second-order browser error for what was really an auth expiry.

Contract pinned here:
  * NotLoggedInError / AuthExpiredError -> AUTH_ERROR, browser fallback SKIPPED
  * any other exception                 -> browser fallback STILL runs
  * navigation failure in the browser branch -> BROWSER_ERROR (and no blocking
    wait on an intercept that can never arrive)
  * handle_tool_action gives one error_code per condition

Every test is PURE: no network, no browser, no file I/O.
"""

import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_server.browser import (
    AuthExpiredError,
    BrowserUnavailableError,
    NotLoggedInError,
)

BROWSER_SEARCH_PAYLOAD = {
    "jobDetails": [
        {
            "jobId": "77001",
            "title": "Node.js Developer",
            "companyName": "Acme",
            "placeholders": [],
            "tagsAndSkills": "node.js,typescript",
            "createdDate": "1d ago",
        }
    ],
    "noOfJobs": 1,
    "clusters": {},
}


def _async_page_cm(page):
    """Build an async context manager that yields ``page`` (mimics acquire_page)."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=page)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# =====================================================================
# 1. tools/search.py - REST-first search
# =====================================================================

class TestSearchAttribution:
    """naukri_search_jobs must classify auth expiry instead of swallowing it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [
        NotLoggedInError("Not logged in - call naukri_login first"),
        AuthExpiredError("Session expired - Naukri returned no auth token"),
    ])
    async def test_auth_expiry_returns_auth_error_and_skips_browser(self, exc):
        from naukri_server.tools.search import naukri_search_jobs

        with patch("naukri_server.tools.search.browser_provider") as mock_provider, \
                patch("naukri_server.tools.search.api_client.get",
                      new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = exc
            result = await naukri_search_jobs(keywords="node.js", limit=5)

        assert result["status"] == "error"
        assert result["error_code"] == "AUTH_ERROR", (
            "auth expiry must be reported as AUTH_ERROR, got %r" % (result,)
        )
        assert "naukri_login" in result["message"]
        mock_provider.acquire_page.assert_not_called()

    @pytest.mark.asyncio
    async def test_generic_error_still_falls_back_to_browser(self):
        """A non-auth failure must keep the existing browser fallback."""
        from naukri_server.tools.search import naukri_search_jobs

        fake_page = MagicMock()

        with patch("naukri_server.tools.search.browser_provider") as mock_provider, \
                patch("naukri_server.tools.search.api_client.get",
                      new_callable=AsyncMock) as mock_get:
            mock_provider.acquire_page = MagicMock(
                return_value=_async_page_cm(fake_page))
            mock_provider.intercept_json = AsyncMock(
                return_value=BROWSER_SEARCH_PAYLOAD)
            mock_get.side_effect = RuntimeError("connection reset by peer")
            result = await naukri_search_jobs(keywords="node.js", limit=5)

        mock_provider.acquire_page.assert_called_once()
        assert result["status"] == "success"
        assert result["search_path"] == "browser"


# =====================================================================
# 2. tools/jobs.py - _get_job REST-first path
# =====================================================================

class TestGetJobAttribution:
    """_get_job must classify auth expiry and stop blaming the page."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [
        NotLoggedInError("Not logged in - call naukri_login first"),
        AuthExpiredError("Session expired - Naukri returned no auth token"),
    ])
    async def test_auth_expiry_returns_auth_error_and_skips_browser(self, exc):
        from naukri_server.tools.jobs import _get_job

        with patch("naukri_server.tools.jobs.browser_provider") as mock_provider, \
                patch("naukri_server.tools.jobs.api_client.get",
                      new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = exc
            result = await _get_job("40911234567")

        assert result["status"] == "error"
        assert result["error_code"] == "AUTH_ERROR", (
            "auth expiry must be reported as AUTH_ERROR, got %r" % (result,)
        )
        assert "naukri_login" in result["message"]
        mock_provider.acquire_page.assert_not_called()

    @pytest.mark.asyncio
    async def test_generic_error_still_falls_back_to_browser(self):
        """A non-auth failure must keep the existing browser fallback."""
        from naukri_server.tools.jobs import _get_job

        fake_page = MagicMock()

        with patch("naukri_server.tools.jobs.INTERCEPT_WAIT_TIMEOUT", 0.05), \
                patch("naukri_server.tools.jobs.BROWSER_PAGE_SETTLE", 0), \
                patch("naukri_server.tools.jobs.browser_provider") as mock_provider, \
                patch("naukri_server.tools.jobs.api_client.get",
                      new_callable=AsyncMock) as mock_get:
            mock_provider.acquire_page = MagicMock(
                return_value=_async_page_cm(fake_page))
            mock_provider.safe_goto = AsyncMock(return_value=True)
            mock_get.side_effect = RuntimeError("connection reset by peer")
            result = await _get_job("40911234567")

        mock_provider.acquire_page.assert_called_once()
        mock_provider.safe_goto.assert_awaited_once()
        # Navigation succeeded but no XHR arrived -> honest API_ERROR, not AUTH.
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    async def test_navigation_failure_is_browser_error_and_does_not_block(self):
        """safe_goto() == False must short-circuit with BROWSER_ERROR.

        Today the return value of safe_goto is DISCARDED: the code waits the
        full intercept timeout and then blames the page ("Page may not have
        loaded"). Both halves are wrong - assert the code and the latency.
        """
        from naukri_server.tools.jobs import _get_job

        fake_page = MagicMock()

        with patch("naukri_server.tools.jobs.INTERCEPT_WAIT_TIMEOUT", 8), \
                patch("naukri_server.tools.jobs.BROWSER_PAGE_SETTLE", 0), \
                patch("naukri_server.tools.jobs.browser_provider") as mock_provider, \
                patch("naukri_server.tools.jobs.api_client.get",
                      new_callable=AsyncMock) as mock_get:
            mock_provider.acquire_page = MagicMock(
                return_value=_async_page_cm(fake_page))
            mock_provider.safe_goto = AsyncMock(return_value=False)
            mock_get.side_effect = RuntimeError("connection reset by peer")
            started = time.monotonic()
            result = await _get_job("40911234567")
            elapsed = time.monotonic() - started

        assert result["status"] == "error"
        assert result["error_code"] == "BROWSER_ERROR", (
            "failed navigation must be a BROWSER_ERROR, got %r" % (result,)
        )
        assert "Page may not have loaded" not in result["message"]
        assert elapsed < 3.0, (
            "failed navigation must not block on the intercept timeout "
            "(took %.1fs)" % elapsed
        )


# =====================================================================
# 3. error_handler.handle_tool_action - one error_code per condition
# =====================================================================

class TestHandleToolActionAttribution:
    """Each failure class gets its own error_code."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc,expected", [
        (NotLoggedInError("Not logged in - call naukri_login first"), "AUTH_ERROR"),
        (AuthExpiredError("Session expired"), "AUTH_ERROR"),
        (BrowserUnavailableError("circuit open - browser is dead"), "BROWSER_ERROR"),
        (RuntimeError("boom"), "INTERNAL_ERROR"),
        (ValueError("plain value error"), "INTERNAL_ERROR"),
    ])
    async def test_error_code_per_condition(self, exc, expected):
        from naukri_server.error_handler import handle_tool_action

        async def _fail():
            raise exc

        result = await handle_tool_action(_fail, "test.attribution")
        assert result["status"] == "error"
        assert result["error_code"] == expected, (
            "%s must map to %s, got %r" % (type(exc).__name__, expected, result)
        )

    @pytest.mark.asyncio
    async def test_target_closed_is_browser_error(self):
        from playwright._impl._errors import TargetClosedError

        from naukri_server.error_handler import handle_tool_action

        async def _fail():
            raise TargetClosedError("Target page, context or browser has been closed")

        result = await handle_tool_action(_fail, "test.target_closed")
        assert result["status"] == "error"
        assert result["error_code"] == "BROWSER_ERROR"

    @pytest.mark.asyncio
    async def test_naukri_api_error_branch_is_untouched(self):
        from naukri_server.api import NaukriAPIError
        from naukri_server.error_handler import handle_tool_action

        async def _fail():
            raise NaukriAPIError(status=503, message="Service Unavailable")

        result = await handle_tool_action(_fail, "test.api_error")
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 503
