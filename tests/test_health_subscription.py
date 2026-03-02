"""Tests for health_check and subscription_status tools.

Every test is PURE: no network, no browser, no file I/O.
We mock all async helpers and verify return structure, status logic, and error handling.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_server.api import NaukriAPIError


# =====================================================================
# 1. naukri_health_check
# =====================================================================


class TestHealthCheck:
    """Tests for naukri_server.tools.health.naukri_health_check."""

    @pytest.mark.asyncio
    async def test_all_checks_pass_api_only(self):
        """When all API checks return ok and include_browser=False, overall status is success with 0 failures."""
        from naukri_server.tools.health import naukri_health_check

        ok_login = {"name": "login", "status": "ok", "message": "Logged in", "elapsed_ms": 10}
        ok_profile = {"name": "profile_api", "status": "ok", "message": "Profile loaded: Test User", "elapsed_ms": 15}
        ok_search = {"name": "search_api", "status": "ok", "message": "Search working, 100 total results", "elapsed_ms": 20}
        ok_recs = {"name": "recommendations_api", "status": "ok", "message": "Recommendations working, 5 jobs", "elapsed_ms": 25}
        ok_dash = {"name": "dashboard_api", "status": "ok", "message": "Dashboard working, 42 profile views", "elapsed_ms": 12}

        with patch("naukri_server.tools.health._check_login", new_callable=AsyncMock, return_value=ok_login), \
             patch("naukri_server.tools.health._check_profile_api", new_callable=AsyncMock, return_value=ok_profile), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock, return_value=ok_search), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock, return_value=ok_recs), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock, return_value=ok_dash), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.os.path.isdir", return_value=True):

            mock_browser.page_pool = None  # No pool stats when browser excluded
            result = await naukri_health_check(include_browser=False)

        assert result["status"] == "success"
        assert result["summary"]["ok"] == 5
        assert result["summary"]["warn"] == 0
        assert result["summary"]["fail"] == 0
        assert len(result["checks"]) == 5
        assert result["pool_stats"] is None

    @pytest.mark.asyncio
    async def test_includes_browser_pool_stats(self):
        """When browser.page_pool exists, pool_stats should be included in response."""
        from naukri_server.tools.health import naukri_health_check

        ok_check = {"name": "check", "status": "ok", "message": "ok", "elapsed_ms": 5}
        pool_stats = {"max_tabs": 3, "checkouts": 10, "returns": 10, "crashes": 0, "max_wait_ms": 50}

        with patch("naukri_server.tools.health._check_login", new_callable=AsyncMock, return_value=ok_check), \
             patch("naukri_server.tools.health._check_profile_api", new_callable=AsyncMock, return_value=ok_check), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock, return_value=ok_check), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock, return_value=ok_check), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock, return_value=ok_check), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.os.path.isdir", return_value=True):

            mock_pool = MagicMock()
            mock_pool.get_stats.return_value = pool_stats
            mock_browser.page_pool = mock_pool
            result = await naukri_health_check(include_browser=False)

        assert result["pool_stats"] == pool_stats
        assert result["pool_stats"]["max_tabs"] == 3
        assert result["pool_stats"]["crashes"] == 0

    @pytest.mark.asyncio
    async def test_partial_failure_mixed_statuses(self):
        """When some checks fail and others pass, summary counts should reflect correctly."""
        from naukri_server.tools.health import naukri_health_check

        ok = {"name": "login", "status": "ok", "message": "ok", "elapsed_ms": 10}
        warn = {"name": "search_api", "status": "warn", "message": "406 reCAPTCHA", "elapsed_ms": 20}
        fail = {"name": "dashboard_api", "status": "fail", "message": "Timeout", "elapsed_ms": 5000}

        with patch("naukri_server.tools.health._check_login", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_profile_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock, return_value=warn), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock, return_value=fail), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock, return_value=fail), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.os.path.isdir", return_value=True):

            mock_browser.page_pool = None
            result = await naukri_health_check(include_browser=False)

        assert result["summary"]["ok"] == 2
        assert result["summary"]["warn"] == 1
        assert result["summary"]["fail"] == 2
        assert len(result["checks"]) == 5

    @pytest.mark.asyncio
    async def test_include_browser_adds_two_extra_checks(self):
        """include_browser=True should add browser_alive and ambitionbox checks (7 total)."""
        from naukri_server.tools.health import naukri_health_check

        ok = {"name": "check", "status": "ok", "message": "ok", "elapsed_ms": 5}
        browser_ok = {"name": "browser_alive", "status": "ok", "message": "Browser working", "elapsed_ms": 50}
        ambition_ok = {"name": "ambitionbox", "status": "ok", "message": "AmbitionBox working", "elapsed_ms": 100}

        with patch("naukri_server.tools.health._check_login", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_profile_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_browser_alive", new_callable=AsyncMock, return_value=browser_ok), \
             patch("naukri_server.tools.health._check_ambitionbox", new_callable=AsyncMock, return_value=ambition_ok), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.os.path.isdir", return_value=True):

            mock_browser.page_pool = None
            result = await naukri_health_check(include_browser=True)

        assert len(result["checks"]) == 7
        check_names = [c["name"] for c in result["checks"]]
        assert "browser_alive" in check_names
        assert "ambitionbox" in check_names
        assert result["summary"]["ok"] == 7

    @pytest.mark.asyncio
    async def test_gather_exception_becomes_fail(self):
        """If asyncio.gather catches an exception from a check, it should be recorded as a fail."""
        from naukri_server.tools.health import naukri_health_check

        ok = {"name": "check", "status": "ok", "message": "ok", "elapsed_ms": 5}

        async def raise_runtime():
            raise RuntimeError("unexpected crash")

        with patch("naukri_server.tools.health._check_login", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_profile_api", side_effect=raise_runtime), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.os.path.isdir", return_value=True):

            mock_browser.page_pool = None
            result = await naukri_health_check(include_browser=False)

        assert result["summary"]["fail"] == 1
        assert result["summary"]["ok"] == 4
        # The failed check should be at index 1 (profile_api)
        failed = [c for c in result["checks"] if c["status"] == "fail"]
        assert len(failed) == 1
        assert failed[0]["name"] == "profile_api"
        assert "RuntimeError" in failed[0]["message"]

    @pytest.mark.asyncio
    async def test_chrome_profile_missing_adds_warning(self):
        """When CHROME_PROFILE dir doesn't exist, a warning should be included."""
        from naukri_server.tools.health import naukri_health_check

        ok = {"name": "check", "status": "ok", "message": "ok", "elapsed_ms": 5}

        with patch("naukri_server.tools.health._check_login", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_profile_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.os.path.isdir", return_value=False):

            mock_browser.page_pool = None
            result = await naukri_health_check(include_browser=False)

        assert "warnings" in result
        assert any("Chrome profile" in w for w in result["warnings"])


# =====================================================================
# 2. naukri_get_subscription_status
# =====================================================================


class TestSubscriptionStatus:
    """Tests for naukri_server.tools.subscription.naukri_get_subscription_status."""

    @pytest.mark.asyncio
    async def test_success_full_response(self):
        """Successful API response with all fields should return structured plan info."""
        from naukri_server.tools.subscription import naukri_get_subscription_status

        mock_data = {
            "user": {
                "isPaid": True,
                "hasActiveSubscription": True,
                "isJobseekerAgentEligible": False,
            },
            "promo": {
                "code": "SUMMER25",
                "discount": 20,
            },
            "servDetails": {
                "resumeHighlighter": {"active": True},
                "jobsBoost": {"active": False},
            },
        }

        with patch("naukri_server.tools.subscription.api_get", new_callable=AsyncMock, return_value=mock_data):
            result = await naukri_get_subscription_status()

        assert result["status"] == "success"
        assert result["is_paid"] is True
        assert result["has_active_subscription"] is True
        assert result["is_jobseeker_agent_eligible"] is False
        assert result["promo_code"] == "SUMMER25"
        assert result["promo_discount"] == 20
        assert "resumeHighlighter" in result["services"]

    @pytest.mark.asyncio
    async def test_api_error_401_returns_auth_error(self):
        """401 NaukriAPIError should be caught by @api_tool and return AUTH_ERROR."""
        from naukri_server.tools.subscription import naukri_get_subscription_status

        with patch("naukri_server.tools.subscription.api_get", new_callable=AsyncMock,
                    side_effect=NaukriAPIError(401, "Session expired")):
            result = await naukri_get_subscription_status()

        assert result["status"] == "error"
        assert result["error_code"] == "AUTH_ERROR"
        assert result["http_status"] == 401
        assert "401" in result["message"]

    @pytest.mark.asyncio
    async def test_api_error_500_returns_api_error(self):
        """Non-401 NaukriAPIError should return API_ERROR error_code."""
        from naukri_server.tools.subscription import naukri_get_subscription_status

        with patch("naukri_server.tools.subscription.api_get", new_callable=AsyncMock,
                    side_effect=NaukriAPIError(500, "Internal server error")):
            result = await naukri_get_subscription_status()

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 500

    @pytest.mark.asyncio
    async def test_empty_response_data(self):
        """Empty API response should return safe defaults (False, empty strings)."""
        from naukri_server.tools.subscription import naukri_get_subscription_status

        with patch("naukri_server.tools.subscription.api_get", new_callable=AsyncMock, return_value={}):
            result = await naukri_get_subscription_status()

        assert result["status"] == "success"
        assert result["is_paid"] is False
        assert result["has_active_subscription"] is False
        assert result["is_jobseeker_agent_eligible"] is False
        assert result["promo_code"] == ""
        assert result["promo_discount"] == ""
        assert result["services"] == {}

    @pytest.mark.asyncio
    async def test_unexpected_exception_caught_by_api_tool(self):
        """Generic exceptions should be caught by @api_tool decorator and returned as error."""
        from naukri_server.tools.subscription import naukri_get_subscription_status

        with patch("naukri_server.tools.subscription.api_get", new_callable=AsyncMock,
                    side_effect=ConnectionError("Network down")):
            result = await naukri_get_subscription_status()

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "ConnectionError" in result["message"]
        assert "Get subscription status" in result["message"]

    @pytest.mark.asyncio
    async def test_alternate_key_names_userState_and_services(self):
        """API may use 'userState' instead of 'user' and 'services' instead of 'servDetails'."""
        from naukri_server.tools.subscription import naukri_get_subscription_status

        mock_data = {
            "userState": {
                "isPaid": False,
                "hasActiveSubscription": False,
                "isJobseekerAgentEligible": True,
            },
            "promo": {
                "promoCode": "WINTER30",
                "percentOff": 30,
            },
            "services": {
                "resumeDisplay": {"active": True},
            },
        }

        with patch("naukri_server.tools.subscription.api_get", new_callable=AsyncMock, return_value=mock_data):
            result = await naukri_get_subscription_status()

        assert result["status"] == "success"
        assert result["is_jobseeker_agent_eligible"] is True
        assert result["promo_code"] == "WINTER30"
        assert result["promo_discount"] == 30
        assert "resumeDisplay" in result["services"]

    @pytest.mark.asyncio
    async def test_non_dict_user_and_promo_fields(self):
        """If user/promo fields are non-dict (e.g., null/string), should fall back to defaults."""
        from naukri_server.tools.subscription import naukri_get_subscription_status

        mock_data = {
            "user": "invalid",
            "promo": None,
            "servDetails": "not_a_dict",
        }

        with patch("naukri_server.tools.subscription.api_get", new_callable=AsyncMock, return_value=mock_data):
            result = await naukri_get_subscription_status()

        assert result["status"] == "success"
        assert result["is_paid"] is False
        assert result["promo_code"] == ""
        assert result["services"] == {}
