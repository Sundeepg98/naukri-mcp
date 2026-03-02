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
        assert set(stats.keys()) == {"total_requests", "success", "errors", "retries", "auth_refreshes"}


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
        # 6 browser + 6 api + 4 discovery = 16
        assert len(_HANDLERS) == 16
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
    """Tests for company follow_status, follow, and unfollow routing in naukri_company."""

    @pytest.mark.asyncio
    async def test_follow_status_requires_id(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="follow_status")
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
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="follow_status", group_id="g1")
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
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="follow", group_ids=["g1", "g2"])
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
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="unfollow", group_id="g1")
        assert result["status"] == "success"
        mock_unfollow.assert_awaited_once_with(["g1"], "unfollow")

    @pytest.mark.asyncio
    async def test_company_invalid_action(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="nonexistent")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]
