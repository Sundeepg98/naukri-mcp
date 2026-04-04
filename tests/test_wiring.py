"""Tests for infrastructure wiring — AssessFitCommand, ApplicationGateway, browser_provider.

Every test is PURE: no network, no browser, no file I/O.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# =====================================================================
# 1. AssessFitCommand used in smart_apply single-job path
# =====================================================================


class TestAssessFitCommandInSmartApply:
    """AssessFitCommand validates and structures input in the single-job path."""

    @pytest.mark.asyncio
    async def test_assess_fit_command_used_for_single_job(self):
        """Single-job assessment should use AssessFitCommand to structure params."""
        from naukri_server.tools.smart_apply import naukri_smart_apply

        job_result = {
            "status": "success",
            "job_id": "J42",
            "title": "Backend Dev",
            "company": "TestCo",
            "salary": "10-15 LPA",
            "experience": "2-4 Yrs",
            "location": "Bangalore",
            "work_mode": "Remote",
            "tags": ["Node.js", "TypeScript"],
        }
        profile_result = {
            "status": "success",
            "key_skills": ["Node.js", "TypeScript", "Express.js"],
            "total_experience": 3,
            "current_location": "Bangalore",
            "expected_ctc": "12 LPA",
        }

        with patch("naukri_server.tools.jobs.naukri_get_job",
                    new_callable=AsyncMock, return_value=job_result) as mock_get_job, \
             patch("naukri_server.tools.profile.get_cached_profile",
                    new_callable=AsyncMock, return_value=profile_result), \
             patch("naukri_server.tools.jobs._fetch_match_score",
                    new_callable=AsyncMock, return_value=None):

            result = await naukri_smart_apply(job_id="J42", min_fit_score=50)

        assert result["status"] == "success"
        assert "fit_assessment" in result
        # Verify the job was fetched with the cmd.job_id (same value, structured via command)
        mock_get_job.assert_called_once_with(job_id_or_url="J42")

    @pytest.mark.asyncio
    async def test_assess_fit_command_preserves_min_fit_score(self):
        """AssessFitCommand should preserve the min_fit_score for auto-apply threshold."""
        from naukri_server.use_cases import AssessFitCommand

        cmd = AssessFitCommand(job_id="J99", min_fit_score=75)
        assert cmd.job_id == "J99"
        assert cmd.min_fit_score == 75

    @pytest.mark.asyncio
    async def test_assess_fit_command_default_min_score(self):
        """AssessFitCommand should default min_fit_score to 60."""
        from naukri_server.use_cases import AssessFitCommand

        cmd = AssessFitCommand(job_id="J1")
        assert cmd.min_fit_score == 60


# =====================================================================
# 2. _app_gateway.get_application returns correct app in _get_application_detail
# =====================================================================


class TestAppGatewayInTracking:
    """_app_gateway is wired into _get_application_detail for local tracking data."""

    @pytest.mark.asyncio
    async def test_get_application_detail_includes_local_tracking(self):
        """When gateway finds a local app, result should include local_tracking."""
        from naukri_server.tools.tracking import _get_application_detail

        api_response = {
            "jobDetails": {"jobTitle": "SDE", "company": "Acme"},
            "status": [],
        }
        local_app = {
            "job_id": "J100",
            "applied_at": "2026-03-15T10:00:00Z",
            "source": "smart_apply",
            "fit_score": 82,
        }

        with patch("naukri_server.tools.tracking.api_client.get",
                    new_callable=AsyncMock, return_value=api_response), \
             patch("naukri_server.tools.tracking._app_gateway.get_application",
                    new_callable=AsyncMock, return_value=local_app):

            result = await _get_application_detail("J100")

        assert result["status"] == "success"
        assert "local_tracking" in result
        assert result["local_tracking"]["applied_at"] == "2026-03-15T10:00:00Z"
        assert result["local_tracking"]["source"] == "smart_apply"
        assert result["local_tracking"]["fit_score"] == 82

    @pytest.mark.asyncio
    async def test_get_application_detail_no_local_tracking_when_missing(self):
        """When gateway returns None, result should NOT include local_tracking."""
        from naukri_server.tools.tracking import _get_application_detail

        api_response = {
            "jobDetails": {"jobTitle": "QA", "company": "Beta"},
            "status": [],
        }

        with patch("naukri_server.tools.tracking.api_client.get",
                    new_callable=AsyncMock, return_value=api_response), \
             patch("naukri_server.tools.tracking._app_gateway.get_application",
                    new_callable=AsyncMock, return_value=None):

            result = await _get_application_detail("J999")

        assert result["status"] == "success"
        assert "local_tracking" not in result


# =====================================================================
# 3. Health check includes browser_interface check
# =====================================================================


class TestBrowserInterfaceHealthCheck:
    """_check_browser_interface is included in health check when include_browser=True."""

    @pytest.mark.asyncio
    async def test_browser_interface_check_returns_ok(self):
        """_check_browser_interface should return ok when browser_provider exists."""
        from naukri_server.tools.health import _check_browser_interface

        result = await _check_browser_interface()

        assert result["name"] == "browser_interface"
        assert result["status"] == "ok"
        assert "available" in result["message"]

    @pytest.mark.asyncio
    async def test_health_check_includes_browser_interface_when_browser_enabled(self):
        """naukri_health_check with include_browser=True should include browser_interface."""
        from naukri_server.tools.health import naukri_health_check

        ok = {"name": "check", "status": "ok", "message": "ok", "elapsed_ms": 5}
        browser_ok = {"name": "browser_alive", "status": "ok", "message": "ok", "elapsed_ms": 10}
        ambition_ok = {"name": "ambitionbox", "status": "ok", "message": "ok", "elapsed_ms": 20}
        iface_ok = {"name": "browser_interface", "status": "ok", "message": "Browser provider interface available"}

        with patch("naukri_server.tools.health._check_login", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_profile_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_browser_alive", new_callable=AsyncMock, return_value=browser_ok), \
             patch("naukri_server.tools.health._check_ambitionbox", new_callable=AsyncMock, return_value=ambition_ok), \
             patch("naukri_server.tools.health._check_browser_interface", new_callable=AsyncMock, return_value=iface_ok), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.os.path.isdir", return_value=True), \
             patch("naukri_server.tools.health.api_metrics") as mock_metrics:

            mock_browser.page_pool = None
            mock_metrics.get_stats.return_value = {}

            result = await naukri_health_check(include_browser=True)

        check_names = [c["name"] for c in result["checks"]]
        assert "browser_interface" in check_names
        # 5 API + 3 browser (browser_alive, ambitionbox, browser_interface) = 8
        assert len(result["checks"]) == 8
        assert result["summary"]["ok"] == 8

    @pytest.mark.asyncio
    async def test_health_check_excludes_browser_interface_when_browser_disabled(self):
        """naukri_health_check with include_browser=False should NOT include browser_interface."""
        from naukri_server.tools.health import naukri_health_check

        ok = {"name": "check", "status": "ok", "message": "ok", "elapsed_ms": 5}

        with patch("naukri_server.tools.health._check_login", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_profile_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock, return_value=ok), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.os.path.isdir", return_value=True), \
             patch("naukri_server.tools.health.api_metrics") as mock_metrics:

            mock_browser.page_pool = None
            mock_metrics.get_stats.return_value = {}

            result = await naukri_health_check(include_browser=False)

        check_names = [c["name"] for c in result["checks"]]
        assert "browser_interface" not in check_names
        assert len(result["checks"]) == 5
