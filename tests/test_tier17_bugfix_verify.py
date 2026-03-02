"""Tier 17 — Bug fix verification tests.

Verifies three categories of Q1 bug fixes:
  1. daily_brief stale_count → total rename
  2. insights no_data returns include error_code: "NOT_FOUND"
  3. auto_hunt / compare silent exception blocks now call logger.debug

Every test is PURE: no network, no browser, no file I/O.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =====================================================================
# 1. Stale count fix in daily_brief  (stale.get("total") not "stale_count")
# =====================================================================

class TestDailyBriefStaleCountFix:
    """Verify daily_brief reads stale count from the 'total' key, not 'stale_count'."""

    # All 16 helpers patched at their source modules — same list as TestDailyBrief.
    _DAILY_BRIEF_PATCHES = [
        "naukri_server.tools.inbox._fetch_inbox",
        "naukri_server.tools.notifications._fetch_notifications",
        "naukri_server.tools.search.naukri_get_recommendations",
        "naukri_server.tools.performance._get_recruiter_activity",
        "naukri_server.tools.performance._get_activity_level",
        "naukri_server.tools.tracking._list_applications",
        "naukri_server.tools.profile._get_dashboard",
        "naukri_server.tools.early_access._list_early_access_roles",
        "naukri_server.tools.subscription._get_subscription_status",
        "naukri_server.tools.reminders._list_reminders",
        "naukri_server.tools.tracking._get_stale_applications",       # index 10
        "naukri_server.tools.alerts._get_alerts_list",
        "naukri_server.tools.assessments._get_profile_completeness",
        "naukri_server.tools.tracking._list_saved_jobs",
        "naukri_server.tools.performance._get_search_impressions",
        "naukri_server.tools.assessments._list_assessments",
    ]

    _DETECT_NEW_ROLES_PATCH = "naukri_server.tools.early_access._detect_new_roles"

    # Baseline returns — all succeed with minimal data.
    _MOCK_RETURNS = [
        {"status": "success", "count": 0, "messages": []},                     # inbox
        {"status": "success", "count": 0, "notifications": []},                # notifications
        {"status": "success", "count": 0, "jobs": []},                         # recommendations
        {"status": "success", "total_actions": 0, "activities": []},           # recruiter
        {"status": "success", "level": "LOW"},                                  # activity
        {"status": "success", "count": 0, "applications": []},                 # applications
        {"status": "success", "profile_views": 0, "total_matches": 0, "unread_invites": 0},  # dashboard
        {"status": "success", "count": 0, "roles": []},                        # early_access
        {"status": "success", "plan": "free"},                                  # subscription
        {"status": "success", "total": 0, "due_count": 0, "reminders": []},    # reminders
        None,  # stale — overridden per test
        {"status": "success", "alerts": []},                                    # alerts
        {"status": "success", "completeness_percent": 90},                      # completeness
        {"status": "success", "total": 0, "saved_jobs": []},                   # saved_jobs
        {"status": "success", "total_appearances": 0, "days": 7},             # impressions
        {"status": "success", "assessments": []},                               # assessments
    ]

    def _build_patches(self, stale_return: dict) -> dict:
        """Build patch dict with a custom stale_applications return."""
        patches = {}
        for i, (path, ret) in enumerate(zip(self._DAILY_BRIEF_PATCHES, self._MOCK_RETURNS)):
            mock = AsyncMock()
            if i == 10:  # stale_applications index
                mock.return_value = stale_return
            else:
                mock.return_value = ret
            patches[path] = mock
        # _detect_new_roles is synchronous — default: no new roles
        patches[self._DETECT_NEW_ROLES_PATCH] = MagicMock(return_value=([], 0))
        return patches

    def _enter_patches(self, patches: dict):
        """Enter all patch context managers, return list of contexts for cleanup."""
        ctx_managers = [patch(path, new=mock) for path, mock in patches.items()]
        for cm in ctx_managers:
            cm.__enter__()
        return ctx_managers

    @staticmethod
    def _exit_patches(ctx_managers):
        for cm in ctx_managers:
            cm.__exit__(None, None, None)

    @pytest.mark.asyncio
    async def test_stale_count_reads_total_key(self):
        """When _get_stale_applications returns {total: 5}, brief shows count=5."""
        from naukri_server.tools.daily_brief import naukri_daily_brief

        stale_return = {
            "status": "success",
            "total": 5,
            "stale_applications": [
                {"job_id": "s1"}, {"job_id": "s2"}, {"job_id": "s3"},
                {"job_id": "s4"}, {"job_id": "s5"},
            ],
        }
        patches = self._build_patches(stale_return)
        ctx_managers = self._enter_patches(patches)
        try:
            result = await naukri_daily_brief()
        finally:
            self._exit_patches(ctx_managers)

        assert result["stale_applications"]["count"] == 5
        # Top stale is capped at 3
        assert len(result["stale_applications"]["top_stale"]) == 3

    @pytest.mark.asyncio
    async def test_stale_count_ignores_old_stale_count_key(self):
        """Response with only 'total' (not 'stale_count') is handled correctly.

        This verifies the fix: code reads stale.get('total'), so 'stale_count'
        has no effect and the count comes from 'total'.
        """
        from naukri_server.tools.daily_brief import naukri_daily_brief

        stale_return = {
            "status": "success",
            "total": 7,
            # Old key present with a DIFFERENT value — should be ignored
            "stale_count": 999,
            "stale_applications": [{"job_id": f"s{i}"} for i in range(7)],
        }
        patches = self._build_patches(stale_return)
        ctx_managers = self._enter_patches(patches)
        try:
            result = await naukri_daily_brief()
        finally:
            self._exit_patches(ctx_managers)

        # The code reads 'total' (7), NOT 'stale_count' (999)
        assert result["stale_applications"]["count"] == 7


# =====================================================================
# 2. error_code: "NOT_FOUND" in insights no_data returns
# =====================================================================

class TestInsightsErrorCode:
    """Verify insights no_data returns include error_code='NOT_FOUND'."""

    @pytest.mark.asyncio
    async def test_application_insights_no_data_has_error_code(self):
        """Empty applications → no_data with error_code NOT_FOUND."""
        from naukri_server.tools.insights import naukri_insights

        with patch("naukri_server.tools.insights._load_json", return_value=[]), \
             patch("naukri_server.tools.insights._applications_lock", new=asyncio.Lock()):
            result = await naukri_insights(insight_type="applications", days=30)

        assert result["status"] == "no_data"
        assert result["error_code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_salary_no_data_has_error_code(self):
        """Empty applications → salary no_data with error_code NOT_FOUND."""
        from naukri_server.tools.insights import naukri_insights

        with patch("naukri_server.tools.insights._load_json", return_value=[]), \
             patch("naukri_server.tools.insights._applications_lock", new=asyncio.Lock()):
            result = await naukri_insights(insight_type="salary")

        assert result["status"] == "no_data"
        assert result["error_code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_cached_answers_empty_has_error_code(self):
        """Empty cache → cached_answers no_data with error_code NOT_FOUND."""
        from naukri_server.tools.insights import naukri_insights

        with patch("naukri_server.tools.insights._load_cache", return_value={}), \
             patch("naukri_server.tools.insights._cache_lock", new=asyncio.Lock()):
            result = await naukri_insights(insight_type="cached_answers", action="list")

        assert result["status"] == "no_data"
        assert result["error_code"] == "NOT_FOUND"


# =====================================================================
# 3. Silent exception logging in auto_hunt and compare
# =====================================================================

class TestSilentExceptionLogging:
    """Verify silent exception blocks call logger.debug (not silently pass)."""

    @pytest.mark.asyncio
    async def test_auto_hunt_logs_crossref_exception(self):
        """When local tracking load raises, auto_hunt logs via logger.debug."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        fake_jobs = [
            {"job_id": "j1", "title": "Dev", "company": "Acme", "tags": ["python"],
             "is_applied": False, "salary": "10 LPA", "location": "Bangalore",
             "work_mode": "wfh", "experience": "2-5 yrs"},
        ]
        fake_profile = {
            "status": "success",
            "key_skills": ["python"],
            "total_experience": 3,
            "current_location": "Bangalore",
            "expected_ctc": "12",
        }

        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.tracking._load_json", side_effect=RuntimeError("disk error")), \
             patch("naukri_server.tools.auto_hunt.logger") as mock_logger:
            mock_search.return_value = {"status": "success", "jobs": fake_jobs, "total": 1}
            mock_profile.return_value = fake_profile

            result = await naukri_auto_hunt(keywords="python", timeout_seconds=10)

        # logger.debug must have been called with the exception info
        mock_logger.debug.assert_called()
        call_args_str = str(mock_logger.debug.call_args)
        assert "disk error" in call_args_str or "Scoring failed" in call_args_str

    @pytest.mark.asyncio
    async def test_compare_logs_crossref_exception(self):
        """When local tracking load raises, compare logs via logger.debug."""
        from naukri_server.tools.compare import naukri_compare_jobs

        fake_job = {
            "status": "success",
            "job_id": "j1", "title": "Dev", "company": "Acme",
            "tags": ["python"], "salary": "10 LPA", "location": "Bangalore",
            "work_mode": "wfh", "experience": "2-5 yrs",
        }
        fake_profile = {
            "status": "success",
            "key_skills": ["python"],
            "total_experience": 3,
            "current_location": "Bangalore",
            "expected_ctc": "12",
        }

        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.tracking._load_json", side_effect=RuntimeError("disk error")), \
             patch("naukri_server.tools.compare.logger") as mock_logger:
            mock_job.return_value = fake_job
            mock_profile.return_value = fake_profile

            result = await naukri_compare_jobs(job_ids=["j1", "j2"], timeout_seconds=10)

        # logger.debug must have been called with the exception info
        mock_logger.debug.assert_called()
        call_args_str = str(mock_logger.debug.call_args)
        assert "disk error" in call_args_str or "Scoring failed" in call_args_str

    @pytest.mark.asyncio
    async def test_auto_hunt_exception_does_not_propagate(self):
        """Cross-ref exception is swallowed — function returns success, not error."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        fake_jobs = [
            {"job_id": "j1", "title": "Dev", "company": "Acme", "tags": ["python"],
             "is_applied": False, "salary": "10 LPA", "location": "Bangalore",
             "work_mode": "wfh", "experience": "2-5 yrs"},
        ]
        fake_profile = {
            "status": "success",
            "key_skills": ["python"],
            "total_experience": 3,
            "current_location": "Bangalore",
            "expected_ctc": "12",
        }

        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.tracking._load_json", side_effect=RuntimeError("disk error")), \
             patch("naukri_server.tools.auto_hunt.logger"):
            mock_search.return_value = {"status": "success", "jobs": fake_jobs, "total": 1}
            mock_profile.return_value = fake_profile

            # Must NOT raise — the exception is caught and logged
            result = await naukri_auto_hunt(keywords="python", timeout_seconds=10)

        # Function completed (success or at least not an error from the exception)
        assert result["status"] == "success"
        assert result["jobs_found"] >= 1
