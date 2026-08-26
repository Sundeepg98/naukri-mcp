"""Deep infrastructure tests — ApiMetrics, session lifecycle, config constants,
debug action routing, and company follow routing.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =====================================================================
# 1. _ApiMetrics — counter increments and get_stats()
# =====================================================================

class TestApiMetrics:
    """Tests for naukri_server.api._ApiMetrics."""

    def test_initial_state(self):
        from naukri_server.api import _ApiMetrics
        m = _ApiMetrics()
        stats = m.get_stats()
        assert stats == {
            "total_requests": 0,
            "success": 0,
            "errors": 0,
            "retries": 0,
            "auth_refreshes": 0,
            "blocks": 0,
            "blocks_by_state": {},
        }

    def test_increment_counters(self):
        from naukri_server.api import _ApiMetrics
        m = _ApiMetrics()
        m.total += 3
        m.success += 2
        m.errors += 1
        m.retries += 1
        m.auth_refreshes += 1
        stats = m.get_stats()
        assert stats["total_requests"] == 3
        assert stats["success"] == 2
        assert stats["errors"] == 1
        assert stats["retries"] == 1
        assert stats["auth_refreshes"] == 1

    def test_get_stats_returns_dict(self):
        from naukri_server.api import _ApiMetrics
        m = _ApiMetrics()
        stats = m.get_stats()
        assert isinstance(stats, dict)
        assert set(stats.keys()) == {
            "total_requests", "success", "errors", "retries", "auth_refreshes",
            "blocks", "blocks_by_state",
        }

    def test_record_block_increments_total_and_per_state(self):
        from naukri_server.api import _ApiMetrics
        m = _ApiMetrics()
        m.record_block("soft_block")
        m.record_block("soft_block")
        m.record_block("captcha")
        stats = m.get_stats()
        assert stats["blocks"] == 3
        assert stats["blocks_by_state"] == {"soft_block": 2, "captcha": 1}


# =====================================================================
# 2. close_api_session — None session and active session
# =====================================================================

class TestCloseApiSession:
    """Tests for naukri_server.api.close_api_session."""

    @pytest.mark.asyncio
    async def test_none_session_is_noop(self):
        """close_api_session with _session=None should be a no-op."""
        import naukri_server.api as api_mod
        original = api_mod._session
        try:
            api_mod._session = None
            await api_mod.close_api_session()  # Should not raise
            assert api_mod._session is None
        finally:
            api_mod._session = original

    @pytest.mark.asyncio
    async def test_active_session_closes(self):
        """close_api_session with an open session should close it and set to None."""
        import naukri_server.api as api_mod
        original = api_mod._session
        try:
            mock_session = MagicMock()
            mock_session.closed = False
            mock_session.close = AsyncMock()
            api_mod._session = mock_session
            await api_mod.close_api_session()
            mock_session.close.assert_called_once()
            assert api_mod._session is None
        finally:
            api_mod._session = original

    @pytest.mark.asyncio
    async def test_already_closed_session_is_noop(self):
        """close_api_session with a session that is already closed should be a no-op."""
        import naukri_server.api as api_mod
        original = api_mod._session
        try:
            mock_session = MagicMock()
            mock_session.closed = True  # already closed
            mock_session.close = AsyncMock()
            api_mod._session = mock_session
            await api_mod.close_api_session()
            mock_session.close.assert_not_called()
            # _session should not be set to None since the branch was not entered
            assert api_mod._session is mock_session
        finally:
            api_mod._session = original


# =====================================================================
# 3. Config constants — existence and types
# =====================================================================

class TestConfigConstants:
    """Verify key config constants exist with correct types."""

    def test_naukri_base_is_string(self):
        from naukri_server.config import NAUKRI_BASE
        assert isinstance(NAUKRI_BASE, str)
        assert NAUKRI_BASE.startswith("https://")

    def test_api_headers_is_dict(self):
        from naukri_server.config import API_HEADERS
        assert isinstance(API_HEADERS, dict)
        assert "appid" in API_HEADERS
        assert "content-type" in API_HEADERS

    def test_timeout_constants_are_ints(self):
        from naukri_server.config import NAV_TIMEOUT, ELEMENT_TIMEOUT, API_TIMEOUT, MAX_TABS
        assert isinstance(NAV_TIMEOUT, int) and NAV_TIMEOUT > 0
        assert isinstance(ELEMENT_TIMEOUT, int) and ELEMENT_TIMEOUT > 0
        assert isinstance(API_TIMEOUT, int) and API_TIMEOUT > 0
        assert isinstance(MAX_TABS, int) and MAX_TABS > 0


# =====================================================================
# 4. naukri_debug — action routing and handler registry
# =====================================================================

class TestDebugRouting:
    """Tests for naukri_server.tools.debug handler registry and invalid-action routing."""

    def test_handler_keys_all_present(self):
        from naukri_server.tools.debug import _HANDLERS, _BROWSER_ACTIONS
        # 6 browser DOM + 6 api REST + 5 api-via-browser + 4 discovery = 21.
        # The five *_via_browser entries arrived 2026-08-26, when the six api_*
        # actions were moved onto the REST transport and the browser-context
        # fetch they used to run was kept under an honest name rather than
        # deleted -- it reaches cookie-authenticated surfaces the bearer cannot.
        assert len(_HANDLERS) == 21
        assert "browser_snapshot" in _HANDLERS
        assert "browser_screenshot" in _HANDLERS
        assert "browser_scan" in _HANDLERS
        assert "browser_deepscan" in _HANDLERS
        assert "browser_explore" in _HANDLERS
        assert "browser_notif_explore" in _HANDLERS

    def test_api_handler_keys(self):
        from naukri_server.tools.debug import _HANDLERS
        for key in ("api_fetch", "api_post", "api_put", "api_delete", "api_fetch_widget", "api_settings"):
            assert key in _HANDLERS, f"Missing handler key: {key}"

    def test_api_via_browser_handler_keys(self):
        """The browser-context transport survives under a name that says so."""
        from naukri_server.tools.debug import _HANDLERS
        for key in ("api_fetch_via_browser", "api_post_via_browser",
                    "api_put_via_browser", "api_delete_via_browser",
                    "api_fetch_widget_via_browser"):
            assert key in _HANDLERS, f"Missing handler key: {key}"

    def test_rest_actions_are_exactly_the_six_bearer_actions(self):
        from naukri_server.tools.debug import _REST_ACTIONS
        assert _REST_ACTIONS == {
            "api_fetch", "api_post", "api_put", "api_delete",
            "api_fetch_widget", "api_settings",
        }

    def test_discovery_handler_keys(self):
        from naukri_server.tools.debug import _HANDLERS
        for key in ("discover_pages", "discover_statuses", "discover_intercept", "discover_click"):
            assert key in _HANDLERS, f"Missing handler key: {key}"

    def test_browser_actions_set(self):
        from naukri_server.tools.debug import _BROWSER_ACTIONS
        expected = {
            "browser_snapshot", "browser_screenshot", "browser_scan",
            "browser_deepscan", "browser_explore", "browser_notif_explore",
        }
        assert _BROWSER_ACTIONS == expected

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        from naukri_server.tools.debug import naukri_debug
        result = await naukri_debug(action="nonexistent")
        assert result["status"] == "error"
        assert "Unknown action" in result["message"]
        assert "nonexistent" in result["message"]
        assert result["error_code"] == "BROWSER_ERROR"


# =====================================================================
# 5. naukri_company — follow routing
# =====================================================================

class TestCompanyFollowRouting:
    """Tests for atomic follow_status / follow / unfollow tools."""

    @pytest.mark.asyncio
    async def test_follow_status_requires_id(self):
        from naukri_server.tools.companies import naukri_follow_status
        result = await naukri_follow_status()
        assert result["status"] == "error"
        assert "group_id" in result["message"] or "group_ids" in result["message"]
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.companies._get_follow_status", new_callable=AsyncMock)
    async def test_follow_status_with_group_id(self, mock_get_status):
        mock_get_status.return_value = {
            "status": "success",
            "followed": ["g1"],
            "not_followed": [],
        }
        from naukri_server.tools.companies import naukri_follow_status
        result = await naukri_follow_status(group_id="g1")
        assert result["status"] == "success"
        mock_get_status.assert_awaited_once_with(["g1"])

    @pytest.mark.asyncio
    @patch("naukri_server.tools.companies._follow_or_unfollow", new_callable=AsyncMock)
    async def test_follow_with_group_ids(self, mock_follow):
        mock_follow.return_value = {
            "status": "success",
            "action": "followed",
            "followed": ["g1", "g2"],
        }
        from naukri_server.tools.companies import naukri_follow_company
        result = await naukri_follow_company(group_ids=["g1", "g2"], action="follow")
        assert result["status"] == "success"
        mock_follow.assert_awaited_once_with(["g1", "g2"], "follow")

    @pytest.mark.asyncio
    @patch("naukri_server.tools.companies._follow_or_unfollow", new_callable=AsyncMock)
    async def test_unfollow_with_single_group_id(self, mock_unfollow):
        mock_unfollow.return_value = {
            "status": "success",
            "action": "unfollowed",
            "unfollowed": ["g1"],
        }
        from naukri_server.tools.companies import naukri_follow_company
        result = await naukri_follow_company(group_id="g1", action="unfollow")
        assert result["status"] == "success"
        mock_unfollow.assert_awaited_once_with(["g1"], "unfollow")


# =====================================================================
# 5xx retry backoff tests (recovered from tier17_hardening_verify.py)
# =====================================================================

def _make_mock_response(status, json_data=None, text="", headers=None):
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
        d0 = mock_sleep.call_args_list[0][0][0]
        assert BACKOFF_BASE * 0.5 <= d0 <= BACKOFF_BASE * 1.5
        d1 = mock_sleep.call_args_list[1][0][0]
        assert BACKOFF_BASE * 2 * 0.5 <= d1 <= BACKOFF_BASE * 2 * 1.5


class TestBackoff504ExhaustsRetries:
    """504 exhausts MAX_RETRIES -- verify error is raised."""

    @pytest.mark.asyncio
    async def test_504_exhausts_retries_raises_error(self):
        from naukri_server.api import _api_request, NaukriAPIError
        from naukri_server.config import API_MAX_RETRIES as MAX_RETRIES

        responses = [_make_mock_response(504, text='{"message": "Gateway Timeout"}') for _ in range(MAX_RETRIES + 1)]
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
        assert mock_sleep.call_count == MAX_RETRIES
