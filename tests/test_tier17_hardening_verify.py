"""Hardening verification tests — 5xx retry backoff and browser startup failure handling."""

import asyncio

import pytest
from unittest.mock import patch, AsyncMock, MagicMock, PropertyMock


# ============================================================================
# 1. 5xx retry backoff tests (api.py _api_request)
# ============================================================================


def _make_mock_response(status, json_data=None, text="", headers=None):
    """Create a mock aiohttp response with the given status and data."""
    resp = AsyncMock()
    resp.status = status
    default_headers = {"content-type": "application/json"}
    if headers:
        default_headers.update(headers)
    resp.headers = default_headers
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    return resp


def _make_session_with_responses(responses):
    """Create a mock aiohttp session that yields responses in order.

    Each call to session.request() returns a context manager whose __aenter__
    yields the next response from the list.
    """
    call_idx = {"i": 0}

    def make_ctx(*args, **kwargs):
        idx = call_idx["i"]
        call_idx["i"] += 1
        resp = responses[idx]
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session = AsyncMock()
    session.request = MagicMock(side_effect=make_ctx)
    return session


class TestBackoff502SingleRetry:
    """502 on first call, 200 on second -- verify sleep called with BACKOFF_BASE * 2**0."""

    @pytest.mark.asyncio
    async def test_502_then_200_sleeps_once(self):
        from naukri_server.api import _api_request
        from naukri_server.config import API_BACKOFF_BASE as BACKOFF_BASE

        resp_502 = _make_mock_response(502, text="Bad Gateway")
        resp_200 = _make_mock_response(200, json_data={"ok": True})
        session = _make_session_with_responses([resp_502, resp_200])

        mock_token_mgr = AsyncMock()
        mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
        mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")

        with patch("naukri_server.api.get_session", AsyncMock(return_value=session)), \
             patch("naukri_server.api.browser") as mock_browser, \
             patch("naukri_server.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_browser.token_manager = mock_token_mgr
            mock_browser.token_manager.get_cookies = MagicMock(return_value="cookie=val")

            result = await _api_request("GET", "/test-path")

        assert result == {"ok": True}
        mock_sleep.assert_called_once()
        actual_delay = mock_sleep.call_args[0][0]
        # Jittered: BACKOFF_BASE * 2^0 * (0.5..1.5)
        assert BACKOFF_BASE * 0.5 <= actual_delay <= BACKOFF_BASE * 1.5


class TestBackoff503TwiceIncreasingDelays:
    """503 twice then 200 -- verify sleep called with increasing delays."""

    @pytest.mark.asyncio
    async def test_503_503_200_sleeps_twice(self):
        from naukri_server.api import _api_request
        from naukri_server.config import API_BACKOFF_BASE as BACKOFF_BASE

        resp_503_a = _make_mock_response(503, text="Service Unavailable")
        resp_503_b = _make_mock_response(503, text="Service Unavailable")
        resp_200 = _make_mock_response(200, json_data={"result": "ok"})
        session = _make_session_with_responses([resp_503_a, resp_503_b, resp_200])

        mock_token_mgr = AsyncMock()
        mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
        mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")

        with patch("naukri_server.api.get_session", AsyncMock(return_value=session)), \
             patch("naukri_server.api.browser") as mock_browser, \
             patch("naukri_server.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_browser.token_manager = mock_token_mgr
            mock_browser.token_manager.get_cookies = MagicMock(return_value="cookie=val")

            result = await _api_request("GET", "/test-path")

        assert result == {"result": "ok"}
        assert mock_sleep.call_count == 2
        # First retry: _attempt=0 -> delay = BACKOFF_BASE * 2^0 * jitter(0.5..1.5)
        d0 = mock_sleep.call_args_list[0][0][0]
        assert BACKOFF_BASE * 0.5 <= d0 <= BACKOFF_BASE * 1.5
        # Second retry: _attempt=1 -> delay = BACKOFF_BASE * 2^1 * jitter(0.5..1.5)
        d1 = mock_sleep.call_args_list[1][0][0]
        assert BACKOFF_BASE * 2 * 0.5 <= d1 <= BACKOFF_BASE * 2 * 1.5


class TestBackoff504ExhaustsRetries:
    """504 exhausts MAX_RETRIES -- verify error is raised (not infinite retries)."""

    @pytest.mark.asyncio
    async def test_504_exhausts_retries_raises_error(self):
        from naukri_server.api import _api_request, NaukriAPIError
        from naukri_server.config import API_MAX_RETRIES as MAX_RETRIES

        # Need MAX_RETRIES + 1 responses (initial call + MAX_RETRIES retries)
        # All return 504
        responses = [
            _make_mock_response(504, text='{"message": "Gateway Timeout"}')
            for _ in range(MAX_RETRIES + 1)
        ]
        session = _make_session_with_responses(responses)

        mock_token_mgr = AsyncMock()
        mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
        mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")

        with patch("naukri_server.api.get_session", AsyncMock(return_value=session)), \
             patch("naukri_server.api.browser") as mock_browser, \
             patch("naukri_server.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_browser.token_manager = mock_token_mgr
            mock_browser.token_manager.get_cookies = MagicMock(return_value="cookie=val")

            with pytest.raises(NaukriAPIError) as exc_info:
                await _api_request("GET", "/test-path")

        assert exc_info.value.status == 504
        # Sleep should have been called MAX_RETRIES times (once per retry before exhaustion)
        assert mock_sleep.call_count == MAX_RETRIES


# ============================================================================
# 2. Browser startup failure tests (browser.py NaukriBrowser)
# ============================================================================


class TestBrowserStartupFailure:
    """When playwright startup throws, browser.available should be False."""

    @pytest.mark.asyncio
    async def test_start_exception_sets_available_false(self):
        from naukri_server.browser import NaukriBrowser

        br = NaukriBrowser()
        assert br.available is False  # default state

        with patch("naukri_server.browser.async_playwright") as mock_apw:
            # async_playwright() returns an object whose .start() is awaited
            mock_pw_cm = AsyncMock()
            mock_apw.return_value = mock_pw_cm
            mock_pw_cm.start = AsyncMock(side_effect=RuntimeError("Playwright install missing"))

            await br.start()

        # Should NOT have raised -- exception is caught
        assert br.available is False


class TestBrowserStartupSuccess:
    """When playwright starts successfully, browser.available should be True."""

    @pytest.mark.asyncio
    async def test_start_success_sets_available_true(self):
        from naukri_server.browser import NaukriBrowser

        br = NaukriBrowser()

        # Build the mock chain: pw -> chromium -> launch_persistent_context -> context
        mock_context = AsyncMock()
        mock_first_page = AsyncMock()
        mock_context.pages = [mock_first_page]
        mock_context.cookies = AsyncMock(return_value=[
            {"name": "nauk_at", "value": "fake-jwt-token"}
        ])

        mock_pw = AsyncMock()
        mock_pw.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

        mock_apw_instance = AsyncMock()
        mock_apw_instance.start = AsyncMock(return_value=mock_pw)

        with patch("naukri_server.browser.async_playwright", return_value=mock_apw_instance), \
             patch("naukri_server.browser.MAX_TABS", 3), \
             patch("naukri_server.browser.CHROME_PROFILE", "/tmp/fake-profile"):
            # Also patch the session validation (api_get) to avoid real network call
            with patch("naukri_server.api.api_get", new_callable=AsyncMock, return_value={"profile": []}):
                await br.start()

        assert br.available is True
        assert br.context is mock_context
        assert br.page_pool is not None
