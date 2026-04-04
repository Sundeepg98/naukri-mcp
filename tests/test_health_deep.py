"""Tests for health module — individual check functions.

Every test is PURE: no network, no browser, no file I/O.
We mock all async helpers and verify return structure, status logic, and error handling
for each _check_* function independently, plus the main naukri_health_check orchestrator.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =====================================================================
# 1. _check_login
# =====================================================================


class TestCheckLogin:
    """Tests for _check_login — verifies session via activity-level API."""

    @pytest.mark.asyncio
    async def test_success_returns_ok(self):
        """Valid dict response from activity-level API yields status=ok."""
        from naukri_server.tools.health import _check_login

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value={"level": 5}):
            result = await _check_login()

        assert result["name"] == "login"
        assert result["status"] == "ok"
        assert "session active" in result["message"]
        assert "elapsed_ms" in result

    @pytest.mark.asyncio
    async def test_non_dict_response_returns_warn(self):
        """If API returns a non-dict (e.g. a list or string), status should be warn."""
        from naukri_server.tools.health import _check_login

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value=["unexpected"]):
            result = await _check_login()

        assert result["name"] == "login"
        assert result["status"] == "warn"
        assert "Unexpected response type" in result["message"]
        assert "list" in result["message"]

    @pytest.mark.asyncio
    async def test_exception_returns_fail(self):
        """Network errors should be caught and returned as status=fail."""
        from naukri_server.tools.health import _check_login

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock,
                    side_effect=ConnectionError("Network unreachable")):
            result = await _check_login()

        assert result["name"] == "login"
        assert result["status"] == "fail"
        assert "ConnectionError" in result["message"]
        assert "Network unreachable" in result["message"]

    @pytest.mark.asyncio
    async def test_elapsed_ms_is_non_negative(self):
        """Elapsed time should always be a non-negative integer."""
        from naukri_server.tools.health import _check_login

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value={}):
            result = await _check_login()

        assert isinstance(result["elapsed_ms"], int)
        assert result["elapsed_ms"] >= 0


# =====================================================================
# 2. _check_profile_api
# =====================================================================


class TestCheckProfileApi:
    """Tests for _check_profile_api — verifies profile data has a name."""

    @pytest.mark.asyncio
    async def test_success_with_name(self):
        """Profile response with name yields ok and includes the name in the message."""
        from naukri_server.tools.health import _check_profile_api

        mock_data = {"profile": [{"name": "Sundeep G", "email": "test@test.com"}]}
        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value=mock_data):
            result = await _check_profile_api()

        assert result["name"] == "profile_api"
        assert result["status"] == "ok"
        assert "Sundeep G" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_profile_list_returns_warn(self):
        """Empty profile list should return warn about missing name."""
        from naukri_server.tools.health import _check_profile_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value={"profile": []}):
            result = await _check_profile_api()

        assert result["name"] == "profile_api"
        assert result["status"] == "warn"
        assert "name not found" in result["message"]

    @pytest.mark.asyncio
    async def test_profile_without_name_key_returns_warn(self):
        """Profile dict missing 'name' key should return warn."""
        from naukri_server.tools.health import _check_profile_api

        mock_data = {"profile": [{"email": "test@test.com"}]}
        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value=mock_data):
            result = await _check_profile_api()

        assert result["status"] == "warn"
        assert "schema change" in result["message"]

    @pytest.mark.asyncio
    async def test_no_profile_key_returns_warn(self):
        """Response without 'profile' key at all should return warn (not crash)."""
        from naukri_server.tools.health import _check_profile_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value={"other": "data"}):
            result = await _check_profile_api()

        assert result["status"] == "warn"

    @pytest.mark.asyncio
    async def test_exception_returns_fail(self):
        """API errors should produce status=fail with exception details."""
        from naukri_server.tools.health import _check_profile_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock,
                    side_effect=TimeoutError("Request timed out")):
            result = await _check_profile_api()

        assert result["name"] == "profile_api"
        assert result["status"] == "fail"
        assert "TimeoutError" in result["message"]


# =====================================================================
# 3. _check_search_api
# =====================================================================


class TestCheckSearchApi:
    """Tests for _check_search_api — verifies search API reachability."""

    @pytest.mark.asyncio
    async def test_success_with_jobs(self):
        """Search response with jobDetails and noOfJobs > 0 yields ok."""
        from naukri_server.tools.health import _check_search_api

        mock_data = {"jobDetails": [{"title": "Python Dev"}], "noOfJobs": 250}
        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value=mock_data):
            result = await _check_search_api()

        assert result["name"] == "search_api"
        assert result["status"] == "ok"
        assert "250 total results" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_job_details_returns_warn(self):
        """Response with empty jobDetails should return warn."""
        from naukri_server.tools.health import _check_search_api

        mock_data = {"jobDetails": [], "noOfJobs": 0}
        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value=mock_data):
            result = await _check_search_api()

        assert result["status"] == "warn"
        assert "jobDetails empty" in result["message"]

    @pytest.mark.asyncio
    async def test_no_job_details_key_returns_warn(self):
        """Response missing jobDetails key entirely should return warn."""
        from naukri_server.tools.health import _check_search_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value={"other": "data"}):
            result = await _check_search_api()

        assert result["status"] == "warn"

    @pytest.mark.asyncio
    async def test_406_recaptcha_returns_warn_not_fail(self):
        """406 reCAPTCHA error is expected (browser intercept needed), should be warn."""
        from naukri_server.tools.health import _check_search_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock,
                    side_effect=Exception("HTTP 406 reCAPTCHA required")):
            result = await _check_search_api()

        assert result["name"] == "search_api"
        assert result["status"] == "warn"
        assert "406" in result["message"] or "reCAPTCHA" in result["message"]

    @pytest.mark.asyncio
    async def test_recaptcha_case_insensitive(self):
        """The recaptcha check should be case-insensitive."""
        from naukri_server.tools.health import _check_search_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock,
                    side_effect=Exception("Blocked by reCAPTCHA challenge")):
            result = await _check_search_api()

        assert result["status"] == "warn"

    @pytest.mark.asyncio
    async def test_non_recaptcha_exception_returns_fail(self):
        """Non-406/non-reCAPTCHA exceptions should be status=fail."""
        from naukri_server.tools.health import _check_search_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock,
                    side_effect=ConnectionError("DNS resolution failed")):
            result = await _check_search_api()

        assert result["name"] == "search_api"
        assert result["status"] == "fail"
        assert "ConnectionError" in result["message"]

    @pytest.mark.asyncio
    async def test_job_details_present_but_no_of_jobs_zero_returns_warn(self):
        """jobDetails present but noOfJobs=0 should return warn (inconsistent data)."""
        from naukri_server.tools.health import _check_search_api

        mock_data = {"jobDetails": [{"title": "Dev"}], "noOfJobs": 0}
        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value=mock_data):
            result = await _check_search_api()

        # Both conditions must be true: job_details truthy AND noOfJobs > 0
        assert result["status"] == "warn"


# =====================================================================
# 4. _check_recommendations_api
# =====================================================================


class TestCheckRecommendationsApi:
    """Tests for _check_recommendations_api — verifies recommendations endpoint."""

    @pytest.mark.asyncio
    async def test_success_with_jobs(self):
        """Response with jobDetails list yields ok with job count."""
        from naukri_server.tools.health import _check_recommendations_api

        mock_data = {"jobDetails": [{"id": 1}, {"id": 2}, {"id": 3}]}
        with patch("naukri_server.tools.health.api_client.post", new_callable=AsyncMock, return_value=mock_data):
            result = await _check_recommendations_api()

        assert result["name"] == "recommendations_api"
        assert result["status"] == "ok"
        assert "3 jobs returned" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_job_details_returns_warn(self):
        """Empty jobDetails should return warn."""
        from naukri_server.tools.health import _check_recommendations_api

        with patch("naukri_server.tools.health.api_client.post", new_callable=AsyncMock,
                    return_value={"jobDetails": []}):
            result = await _check_recommendations_api()

        assert result["status"] == "warn"
        assert "jobDetails empty" in result["message"]

    @pytest.mark.asyncio
    async def test_missing_job_details_key_returns_warn(self):
        """Response without jobDetails key should return warn."""
        from naukri_server.tools.health import _check_recommendations_api

        with patch("naukri_server.tools.health.api_client.post", new_callable=AsyncMock,
                    return_value={"recommendations": []}):
            result = await _check_recommendations_api()

        assert result["status"] == "warn"

    @pytest.mark.asyncio
    async def test_exception_returns_fail(self):
        """API errors should return status=fail."""
        from naukri_server.tools.health import _check_recommendations_api

        with patch("naukri_server.tools.health.api_client.post", new_callable=AsyncMock,
                    side_effect=RuntimeError("Server error")):
            result = await _check_recommendations_api()

        assert result["name"] == "recommendations_api"
        assert result["status"] == "fail"
        assert "RuntimeError" in result["message"]


# =====================================================================
# 5. _check_dashboard_api
# =====================================================================


class TestCheckDashboardApi:
    """Tests for _check_dashboard_api — verifies dashboard profile view count."""

    @pytest.mark.asyncio
    async def test_success_with_profile_view_count(self):
        """Dashboard with profileViewCount yields ok and includes count in message."""
        from naukri_server.tools.health import _check_dashboard_api

        mock_data = {"dashBoard": {"profileViewCount": 42, "otherField": "x"}}
        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value=mock_data):
            result = await _check_dashboard_api()

        assert result["name"] == "dashboard_api"
        assert result["status"] == "ok"
        assert "42 profile views" in result["message"]

    @pytest.mark.asyncio
    async def test_zero_profile_views_still_ok(self):
        """profileViewCount=0 is still a valid response, should be ok."""
        from naukri_server.tools.health import _check_dashboard_api

        mock_data = {"dashBoard": {"profileViewCount": 0}}
        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value=mock_data):
            result = await _check_dashboard_api()

        assert result["status"] == "ok"
        assert "0 profile views" in result["message"]

    @pytest.mark.asyncio
    async def test_missing_profile_view_count_returns_warn(self):
        """Dashboard response without profileViewCount should return warn."""
        from naukri_server.tools.health import _check_dashboard_api

        mock_data = {"dashBoard": {"someOtherField": 10}}
        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock, return_value=mock_data):
            result = await _check_dashboard_api()

        assert result["status"] == "warn"
        assert "profileViewCount missing" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_dashboard_object_returns_warn(self):
        """Empty dashBoard dict should return warn."""
        from naukri_server.tools.health import _check_dashboard_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock,
                    return_value={"dashBoard": {}}):
            result = await _check_dashboard_api()

        assert result["status"] == "warn"

    @pytest.mark.asyncio
    async def test_no_dashboard_key_returns_warn(self):
        """Response without dashBoard key should return warn (not crash)."""
        from naukri_server.tools.health import _check_dashboard_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock,
                    return_value={"other": "data"}):
            result = await _check_dashboard_api()

        assert result["status"] == "warn"

    @pytest.mark.asyncio
    async def test_exception_returns_fail(self):
        """API errors should return status=fail with exception info."""
        from naukri_server.tools.health import _check_dashboard_api

        with patch("naukri_server.tools.health.api_client.get", new_callable=AsyncMock,
                    side_effect=OSError("Connection reset")):
            result = await _check_dashboard_api()

        assert result["name"] == "dashboard_api"
        assert result["status"] == "fail"
        assert "OSError" in result["message"]


# =====================================================================
# 6. _check_browser_alive
# =====================================================================


def _make_mock_pool(page_url="https://www.naukri.com/mnjuser/homepage", page_crashed=False):
    """Helper to create a mock browser.page_pool with acquire() context manager."""
    mock_page = MagicMock()
    mock_page.url = page_url
    if page_crashed:
        mock_page.url = page_url  # Won't matter since goto will raise

    @asynccontextmanager
    async def fake_acquire():
        yield mock_page

    mock_pool = MagicMock()
    mock_pool.acquire = fake_acquire
    return mock_pool, mock_page


class TestCheckBrowserAlive:
    """Tests for _check_browser_alive — verifies page pool and session."""

    @pytest.mark.asyncio
    async def test_success_navigates_to_homepage(self):
        """Browser navigates to homepage without redirect yields ok."""
        from naukri_server.tools.health import _check_browser_alive

        mock_pool, mock_page = _make_mock_pool("https://www.naukri.com/mnjuser/homepage")

        with patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.page_goto", new_callable=AsyncMock):
            mock_browser.page_pool = mock_pool
            result = await _check_browser_alive()

        assert result["name"] == "browser_alive"
        assert result["status"] == "ok"
        assert "page pool working" in result["message"]

    @pytest.mark.asyncio
    async def test_redirected_to_login_returns_fail(self):
        """If page URL contains /nlogin, session expired — should be fail."""
        from naukri_server.tools.health import _check_browser_alive

        mock_pool, mock_page = _make_mock_pool("https://www.naukri.com/nlogin/homepage?redirect=true")

        with patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.page_goto", new_callable=AsyncMock):
            mock_browser.page_pool = mock_pool
            result = await _check_browser_alive()

        assert result["name"] == "browser_alive"
        assert result["status"] == "fail"
        assert "login page" in result["message"].lower() or "session expired" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_page_pool_exception_returns_fail(self):
        """If page pool acquire raises (e.g. pool unavailable), status=fail."""
        from naukri_server.tools.health import _check_browser_alive

        @asynccontextmanager
        async def broken_acquire():
            raise RuntimeError("All tabs crashed")
            yield  # noqa: unreachable — needed for asynccontextmanager syntax

        mock_pool = MagicMock()
        mock_pool.acquire = broken_acquire

        with patch("naukri_server.tools.health.browser") as mock_browser:
            mock_browser.page_pool = mock_pool
            result = await _check_browser_alive()

        assert result["status"] == "fail"
        assert "RuntimeError" in result["message"]
        assert "All tabs crashed" in result["message"]

    @pytest.mark.asyncio
    async def test_page_goto_timeout_returns_fail(self):
        """If page_goto times out, should return fail."""
        from naukri_server.tools.health import _check_browser_alive

        mock_pool, _ = _make_mock_pool()

        with patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.page_goto", new_callable=AsyncMock,
                   side_effect=TimeoutError("Navigation timeout 30000ms exceeded")):
            mock_browser.page_pool = mock_pool
            result = await _check_browser_alive()

        assert result["status"] == "fail"
        assert "TimeoutError" in result["message"]


# =====================================================================
# 7. _check_ambitionbox
# =====================================================================


class TestCheckAmbitionbox:
    """Tests for _check_ambitionbox — verifies AmbitionBox __NEXT_DATA__ scraping."""

    @pytest.mark.asyncio
    async def test_success_next_data_found(self):
        """When __NEXT_DATA__ is extractable with pageProps, status=ok."""
        from naukri_server.tools.health import _check_ambitionbox

        next_data = {"props": {"pageProps": {"salaries": [{"role": "SDE", "salary": 1500000}]}}}
        mock_pool, mock_page = _make_mock_pool("https://www.ambitionbox.com/salaries/google-salaries")
        mock_page.evaluate = AsyncMock(return_value=next_data)

        with patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.health.asyncio.sleep", new_callable=AsyncMock):
            mock_browser.page_pool = mock_pool
            result = await _check_ambitionbox()

        assert result["name"] == "ambitionbox"
        assert result["status"] == "ok"
        assert "__NEXT_DATA__ extractable" in result["message"]

    @pytest.mark.asyncio
    async def test_next_data_missing_returns_warn(self):
        """When __NEXT_DATA__ is null (not found), status=warn."""
        from naukri_server.tools.health import _check_ambitionbox

        mock_pool, mock_page = _make_mock_pool()
        mock_page.evaluate = AsyncMock(return_value=None)

        with patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.health.asyncio.sleep", new_callable=AsyncMock):
            mock_browser.page_pool = mock_pool
            result = await _check_ambitionbox()

        assert result["status"] == "warn"
        assert "__NEXT_DATA__ not found" in result["message"]

    @pytest.mark.asyncio
    async def test_next_data_missing_page_props_returns_warn(self):
        """When __NEXT_DATA__ exists but has no pageProps, status=warn."""
        from naukri_server.tools.health import _check_ambitionbox

        mock_pool, mock_page = _make_mock_pool()
        mock_page.evaluate = AsyncMock(return_value={"props": {}})

        with patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.health.asyncio.sleep", new_callable=AsyncMock):
            mock_browser.page_pool = mock_pool
            result = await _check_ambitionbox()

        assert result["status"] == "warn"

    @pytest.mark.asyncio
    async def test_next_data_empty_props_returns_warn(self):
        """When __NEXT_DATA__ has no 'props' key at all, status=warn."""
        from naukri_server.tools.health import _check_ambitionbox

        mock_pool, mock_page = _make_mock_pool()
        mock_page.evaluate = AsyncMock(return_value={"other": "stuff"})

        with patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.page_goto", new_callable=AsyncMock), \
             patch("naukri_server.tools.health.asyncio.sleep", new_callable=AsyncMock):
            mock_browser.page_pool = mock_pool
            result = await _check_ambitionbox()

        assert result["status"] == "warn"

    @pytest.mark.asyncio
    async def test_exception_returns_fail(self):
        """Browser errors during AmbitionBox check should return fail."""
        from naukri_server.tools.health import _check_ambitionbox

        @asynccontextmanager
        async def broken_acquire():
            raise Exception("Page context destroyed")
            yield  # noqa

        mock_pool = MagicMock()
        mock_pool.acquire = broken_acquire

        with patch("naukri_server.tools.health.browser") as mock_browser:
            mock_browser.page_pool = mock_pool
            result = await _check_ambitionbox()

        assert result["name"] == "ambitionbox"
        assert result["status"] == "fail"
        assert "Page context destroyed" in result["message"]


# =====================================================================
# 8. naukri_health_check (orchestrator)
# =====================================================================


class TestNaukriHealthCheckOrchestrator:
    """Tests for the main naukri_health_check tool — orchestration and summary logic."""

    def _patch_all_checks(self, api_results=None, browser_result=None, ambitionbox_result=None, browser_iface_result=None):
        """Helper that returns a context manager patching all check functions."""
        ok = {"name": "check", "status": "ok", "message": "ok", "elapsed_ms": 5}
        if api_results is None:
            api_results = [ok, ok, ok, ok, ok]
        if browser_result is None:
            browser_result = {"name": "browser_alive", "status": "ok", "message": "ok", "elapsed_ms": 10}
        if ambitionbox_result is None:
            ambitionbox_result = {"name": "ambitionbox", "status": "ok", "message": "ok", "elapsed_ms": 20}
        if browser_iface_result is None:
            browser_iface_result = {"name": "browser_interface", "status": "ok", "message": "Browser provider interface available"}

        names = ["_check_login", "_check_profile_api", "_check_search_api",
                 "_check_recommendations_api", "_check_dashboard_api"]

        class _Ctx:
            def __enter__(self_inner):
                self_inner._patches = []
                for name, result in zip(names, api_results):
                    p = patch(f"naukri_server.tools.health.{name}", new_callable=AsyncMock, return_value=result)
                    p.start()
                    self_inner._patches.append(p)

                p_browser = patch("naukri_server.tools.health._check_browser_alive",
                                  new_callable=AsyncMock, return_value=browser_result)
                p_browser.start()
                self_inner._patches.append(p_browser)

                p_ambition = patch("naukri_server.tools.health._check_ambitionbox",
                                   new_callable=AsyncMock, return_value=ambitionbox_result)
                p_ambition.start()
                self_inner._patches.append(p_ambition)

                p_iface = patch("naukri_server.tools.health._check_browser_interface",
                                new_callable=AsyncMock, return_value=browser_iface_result)
                p_iface.start()
                self_inner._patches.append(p_iface)

                p_brow = patch("naukri_server.tools.health.browser")
                mock_browser = p_brow.start()
                mock_browser.page_pool = None
                self_inner._patches.append(p_brow)

                p_isdir = patch("naukri_server.tools.health.os.path.isdir", return_value=True)
                p_isdir.start()
                self_inner._patches.append(p_isdir)

                p_metrics = patch("naukri_server.tools.health.api_metrics")
                mock_metrics = p_metrics.start()
                mock_metrics.get_stats.return_value = {"total_calls": 100, "errors": 2}
                self_inner._patches.append(p_metrics)

                return self_inner

            def __exit__(self_inner, *args):
                for p in self_inner._patches:
                    p.stop()

        return _Ctx()

    @pytest.mark.asyncio
    async def test_all_pass_api_only(self):
        """All 5 API checks pass, browser excluded — summary should show 5 ok."""
        from naukri_server.tools.health import naukri_health_check

        with self._patch_all_checks():
            result = await naukri_health_check(include_browser=False)

        assert result["status"] == "success"
        assert result["summary"]["ok"] == 5
        assert result["summary"]["warn"] == 0
        assert result["summary"]["fail"] == 0
        assert len(result["checks"]) == 5

    @pytest.mark.asyncio
    async def test_all_pass_with_browser(self):
        """All 8 checks pass (5 API + 3 browser) — summary should show 8 ok."""
        from naukri_server.tools.health import naukri_health_check

        with self._patch_all_checks():
            result = await naukri_health_check(include_browser=True)

        assert result["summary"]["ok"] == 8
        assert len(result["checks"]) == 8
        check_names = [c["name"] for c in result["checks"]]
        assert "browser_alive" in check_names
        assert "ambitionbox" in check_names
        assert "browser_interface" in check_names

    @pytest.mark.asyncio
    async def test_partial_failures(self):
        """Mix of ok, warn, fail should be counted correctly."""
        from naukri_server.tools.health import naukri_health_check

        api = [
            {"name": "login", "status": "ok", "message": "ok", "elapsed_ms": 5},
            {"name": "profile_api", "status": "fail", "message": "timeout", "elapsed_ms": 5000},
            {"name": "search_api", "status": "warn", "message": "406 reCAPTCHA", "elapsed_ms": 200},
            {"name": "recommendations_api", "status": "ok", "message": "ok", "elapsed_ms": 10},
            {"name": "dashboard_api", "status": "fail", "message": "500 error", "elapsed_ms": 100},
        ]

        with self._patch_all_checks(api_results=api):
            result = await naukri_health_check(include_browser=False)

        assert result["summary"]["ok"] == 2
        assert result["summary"]["warn"] == 1
        assert result["summary"]["fail"] == 2

    @pytest.mark.asyncio
    async def test_include_browser_false_skips_browser_checks(self):
        """With include_browser=False, only 5 API checks should run."""
        from naukri_server.tools.health import naukri_health_check

        with self._patch_all_checks():
            result = await naukri_health_check(include_browser=False)

        check_names = [c["name"] for c in result["checks"]]
        assert "browser_alive" not in check_names
        assert "ambitionbox" not in check_names
        assert len(result["checks"]) == 5

    @pytest.mark.asyncio
    async def test_api_metrics_included(self):
        """Result should include api_metrics stats."""
        from naukri_server.tools.health import naukri_health_check

        with self._patch_all_checks():
            result = await naukri_health_check(include_browser=False)

        assert "api_metrics" in result
        assert result["api_metrics"]["total_calls"] == 100

    @pytest.mark.asyncio
    async def test_total_ms_is_present(self):
        """Summary should always include total_ms."""
        from naukri_server.tools.health import naukri_health_check

        with self._patch_all_checks():
            result = await naukri_health_check(include_browser=False)

        assert "total_ms" in result["summary"]
        assert isinstance(result["summary"]["total_ms"], int)
        assert result["summary"]["total_ms"] >= 0

    @pytest.mark.asyncio
    async def test_browser_fail_counted_in_summary(self):
        """Browser check failure should be counted in the summary fail count."""
        from naukri_server.tools.health import naukri_health_check

        browser_fail = {"name": "browser_alive", "status": "fail", "message": "crashed", "elapsed_ms": 0}

        with self._patch_all_checks(browser_result=browser_fail):
            result = await naukri_health_check(include_browser=True)

        assert result["summary"]["fail"] == 1
        assert result["summary"]["ok"] == 7  # 5 API + 1 ambitionbox + 1 browser_interface

    @pytest.mark.asyncio
    async def test_chrome_profile_missing_adds_warning(self):
        """When Chrome profile directory doesn't exist, warnings list should be populated."""
        from naukri_server.tools.health import naukri_health_check

        ok = {"name": "check", "status": "ok", "message": "ok", "elapsed_ms": 5}

        with patch("naukri_server.tools.health._check_login", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_profile_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.os.path.isdir", return_value=False), \
             patch("naukri_server.tools.health.api_metrics") as mock_metrics:

            mock_browser.page_pool = None
            mock_metrics.get_stats.return_value = {}
            result = await naukri_health_check(include_browser=False)

        assert "warnings" in result
        assert any("Chrome profile" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_no_warnings_when_chrome_profile_exists(self):
        """When Chrome profile exists, no warnings key should be present."""
        from naukri_server.tools.health import naukri_health_check

        with self._patch_all_checks():
            result = await naukri_health_check(include_browser=False)

        assert "warnings" not in result
