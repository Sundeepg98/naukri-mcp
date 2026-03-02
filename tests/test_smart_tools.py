"""Tests for smart tools and daily brief — smart_apply, compare, auto_hunt, skill_gap, daily_brief.

Every test is PURE: no network, no browser, no file I/O.
We exercise validation logic and orchestration with mocked helpers.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. naukri_smart_apply
# =====================================================================

class TestSmartApply:
    """Tests for naukri_server.tools.smart_apply.naukri_smart_apply."""

    @pytest.mark.asyncio
    async def test_smart_apply_requires_job_id(self):
        """Calling without job_id (a required str param) raises TypeError."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        with pytest.raises(TypeError):
            await naukri_smart_apply()

    @pytest.mark.asyncio
    async def test_smart_apply_min_fit_score_too_high(self):
        """min_fit_score > 100 returns validation error."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(job_id="12345", min_fit_score=101)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "min_fit_score" in result["message"]

    @pytest.mark.asyncio
    async def test_smart_apply_min_fit_score_negative(self):
        """min_fit_score < 0 returns validation error."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(job_id="12345", min_fit_score=-1)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "min_fit_score" in result["message"]

    @pytest.mark.asyncio
    async def test_smart_apply_min_fit_score_boundary_valid(self):
        """min_fit_score at boundary (0) passes validation, proceeds to fetch."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        # Patch at the source modules since smart_apply uses local imports
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_job.return_value = {"status": "error", "message": "not found"}
            mock_profile.return_value = {"status": "success", "key_skills": []}
            # Score 0 should pass validation, then fail on job fetch
            result = await naukri_smart_apply(job_id="12345", min_fit_score=0)
            assert result["status"] == "error"
            assert result["error_code"] == "API_ERROR"


# =====================================================================
# 2. naukri_compare_jobs
# =====================================================================

class TestCompareJobs:
    """Tests for naukri_server.tools.compare.naukri_compare_jobs."""

    @pytest.mark.asyncio
    async def test_compare_requires_at_least_2(self):
        """Single job_id returns validation error."""
        from naukri_server.tools.compare import naukri_compare_jobs
        result = await naukri_compare_jobs(job_ids=["123"])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "at least 2" in result["message"]

    @pytest.mark.asyncio
    async def test_compare_empty_job_ids(self):
        """Empty job_ids list returns validation error."""
        from naukri_server.tools.compare import naukri_compare_jobs
        result = await naukri_compare_jobs(job_ids=[])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "at least 2" in result["message"]

    @pytest.mark.asyncio
    async def test_compare_max_5_jobs(self):
        """More than 5 job_ids returns validation error."""
        from naukri_server.tools.compare import naukri_compare_jobs
        result = await naukri_compare_jobs(job_ids=["1", "2", "3", "4", "5", "6"])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Maximum 5" in result["message"]


# =====================================================================
# 3. naukri_auto_hunt
# =====================================================================

class TestAutoHunt:
    """Tests for naukri_server.tools.auto_hunt.naukri_auto_hunt."""

    @pytest.mark.asyncio
    async def test_auto_hunt_requires_keywords(self):
        """Calling without keywords (a required str param) raises TypeError."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt
        with pytest.raises(TypeError):
            await naukri_auto_hunt()

    @pytest.mark.asyncio
    async def test_auto_hunt_min_fit_score_too_high(self):
        """min_fit_score > 100 returns validation error."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt
        result = await naukri_auto_hunt(keywords="python", min_fit_score=101)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "min_fit_score" in result["message"]

    @pytest.mark.asyncio
    async def test_auto_hunt_min_fit_score_negative(self):
        """min_fit_score < 0 returns validation error."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt
        result = await naukri_auto_hunt(keywords="python", min_fit_score=-5)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "min_fit_score" in result["message"]

    @pytest.mark.asyncio
    async def test_auto_hunt_limit_clamped(self):
        """limit above 50 should be clamped to 50 (not an error)."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt
        # Patch at source modules since auto_hunt uses local imports
        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = {"status": "success", "jobs": []}
            mock_profile.return_value = {"status": "success", "key_skills": []}
            result = await naukri_auto_hunt(keywords="python", limit=100)
            # Verify search was called with clamped limit=50
            _, call_kwargs = mock_search.call_args
            assert call_kwargs["limit"] == 50


# =====================================================================
# 4. naukri_skill_gap_analysis
# =====================================================================

class TestSkillGapAnalysis:
    """Tests for naukri_server.tools.skill_gap.naukri_skill_gap_analysis."""

    @pytest.mark.asyncio
    async def test_skill_gap_requires_keywords_when_no_recommendations(self):
        """keywords is required when use_recommendations is False."""
        from naukri_server.tools.skill_gap import naukri_skill_gap_analysis
        result = await naukri_skill_gap_analysis(use_recommendations=False)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keywords" in result["message"]

    @pytest.mark.asyncio
    async def test_skill_gap_sample_size_clamped(self):
        """sample_size above 50 should be clamped to 50."""
        from naukri_server.tools.skill_gap import naukri_skill_gap_analysis
        # Patch at source modules since skill_gap uses local imports
        with patch("naukri_server.tools.search.naukri_get_recommendations", new_callable=AsyncMock) as mock_recs, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_recs.return_value = {"status": "success", "jobs": []}
            mock_profile.return_value = {"status": "success", "key_skills": []}
            result = await naukri_skill_gap_analysis(sample_size=200)
            # Verify recommendations was called with clamped limit=50
            _, call_kwargs = mock_recs.call_args
            assert call_kwargs["limit"] == 50


# =====================================================================
# 5. naukri_daily_brief
# =====================================================================

class TestDailyBrief:
    """Tests for naukri_server.tools.daily_brief.naukri_daily_brief.

    All 11 internal helpers are mocked at their source modules — no network, no browser.
    """

    # All 11 helpers patched at their source modules (where they are defined).
    # daily_brief imports them locally, so the local import resolves to the source.
    _DAILY_BRIEF_PATCHES = [
        "naukri_server.tools.inbox._fetch_inbox",
        "naukri_server.tools.notifications._fetch_notifications",
        "naukri_server.tools.search.naukri_get_recommendations",
        "naukri_server.tools.performance._get_recruiter_activity",
        "naukri_server.tools.performance._get_activity_level",
        "naukri_server.tools.tracking._list_applications",
        "naukri_server.tools.profile.naukri_get_dashboard",
        "naukri_server.tools.early_access._list_early_access_roles",
        "naukri_server.tools.subscription.naukri_get_subscription_status",
        "naukri_server.tools.reminders._list_reminders",
        "naukri_server.tools.tracking._get_stale_applications",
    ]

    # Corresponding mock return values (order matches _DAILY_BRIEF_PATCHES)
    _MOCK_RETURNS = [
        {"status": "success", "count": 2, "messages": [{"id": "m1"}, {"id": "m2"}]},
        {"status": "success", "count": 1, "notifications": [{"id": "n1"}]},
        {"status": "success", "count": 3, "jobs": [{"job_id": "j1"}, {"job_id": "j2"}, {"job_id": "j3"}]},
        {"status": "success", "total_actions": 5, "percentage_change": 10, "activities": [{"action": "viewed"}]},
        {"status": "success", "level": "HIGH"},
        {"status": "success", "count": 1, "applications": [{"job_id": "a1"}]},
        {"status": "success", "profile_views": 42, "total_matches": 100, "unread_invites": 3},
        {"status": "success", "count": 2, "roles": [{"role_id": "r1"}, {"role_id": "r2"}]},
        {"status": "success", "plan": "premium", "days_left": 15},
        {"status": "success", "total": 1, "due_count": 1, "reminders": [{"job_id": "rm1", "is_due": True}]},
        {"status": "success", "stale_count": 2, "stale_applications": [{"job_id": "s1"}, {"job_id": "s2"}]},
    ]

    # Short labels matching the patch order (for override dict keys)
    _PATCH_LABELS = [
        "inbox", "notifications", "recommendations", "recruiter_activity",
        "activity_level", "applications", "dashboard", "early_access",
        "subscription", "reminders", "stale_applications",
    ]

    def _build_patches(self, overrides=None):
        """Create a dict of patch path -> AsyncMock with return values.

        Args:
            overrides: dict mapping patch path to a side_effect (Exception) or return_value (dict).
        """
        patches = {}
        for path, ret in zip(self._DAILY_BRIEF_PATCHES, self._MOCK_RETURNS):
            mock = AsyncMock()
            if overrides and path in overrides:
                override = overrides[path]
                if isinstance(override, Exception):
                    mock.side_effect = override
                else:
                    mock.return_value = override
            else:
                mock.return_value = ret
            patches[path] = mock
        return patches

    @pytest.mark.asyncio
    async def test_daily_brief_returns_all_sections(self):
        """All 11 sections present in response when all helpers succeed."""
        from naukri_server.tools.daily_brief import naukri_daily_brief

        patches = self._build_patches()
        ctx_managers = [patch(path, new=mock) for path, mock in patches.items()]

        # Enter all patches
        for cm in ctx_managers:
            cm.__enter__()
        try:
            result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        assert result["status"] == "success"
        assert "date" in result

        # All 11 sections must be present
        expected_sections = [
            "unread_messages",
            "notifications",
            "recommendations",
            "recruiter_activity",
            "activity_level",
            "todays_applications",
            "dashboard",
            "early_access_roles",
            "subscription",
            "due_reminders",
            "stale_applications",
        ]
        for section in expected_sections:
            assert section in result, f"Missing section: {section}"

        # Verify data was threaded through correctly
        assert result["unread_messages"]["count"] == 2
        assert len(result["unread_messages"]["messages"]) == 2
        assert result["notifications"]["count"] == 1
        assert result["recommendations"]["count"] == 3
        assert result["recruiter_activity"]["total"] == 5
        assert result["activity_level"] == "HIGH"
        assert result["todays_applications"]["count"] == 1
        assert result["dashboard"]["profile_views"] == 42
        assert result["early_access_roles"]["count"] == 2
        assert result["subscription"]["plan"] == "premium"
        assert result["due_reminders"]["count"] == 1
        assert result["stale_applications"]["count"] == 2

        # No errors when all succeed
        assert "errors" not in result

    @pytest.mark.asyncio
    async def test_daily_brief_handles_partial_failure(self):
        """When one helper raises an Exception, other sections still returned."""
        from naukri_server.tools.daily_brief import naukri_daily_brief

        # Make notifications helper raise an exception
        overrides = {
            "naukri_server.tools.notifications._fetch_notifications": RuntimeError("connection timeout"),
        }
        patches = self._build_patches(overrides=overrides)
        ctx_managers = [patch(path, new=mock) for path, mock in patches.items()]

        for cm in ctx_managers:
            cm.__enter__()
        try:
            result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        # Should be partial_success due to the failure
        assert result["status"] == "partial_success"
        assert "errors" in result
        assert len(result["errors"]) == 1
        assert "Notifications" in result["errors"][0]

        # Other sections should still have data
        assert result["unread_messages"]["count"] == 2
        assert result["recommendations"]["count"] == 3
        assert result["recruiter_activity"]["total"] == 5
        assert result["activity_level"] == "HIGH"
        assert result["todays_applications"]["count"] == 1
        assert result["dashboard"]["profile_views"] == 42
        assert result["subscription"]["plan"] == "premium"

        # Notifications section should be zeroed out
        assert result["notifications"]["count"] == 0
        assert result["notifications"]["items"] == []

    @pytest.mark.asyncio
    async def test_daily_brief_handles_error_status_from_helper(self):
        """When a helper returns {status: error}, section is zeroed and error logged."""
        from naukri_server.tools.daily_brief import naukri_daily_brief

        overrides = {
            "naukri_server.tools.inbox._fetch_inbox": {
                "status": "error", "message": "auth expired",
            },
        }
        patches = self._build_patches(overrides=overrides)
        ctx_managers = [patch(path, new=mock) for path, mock in patches.items()]

        for cm in ctx_managers:
            cm.__enter__()
        try:
            result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        assert result["status"] == "partial_success"
        assert any("Inbox" in e for e in result["errors"])
        # Inbox zeroed out
        assert result["unread_messages"]["count"] == 0
        assert result["unread_messages"]["messages"] == []
        # Other sections still fine
        assert result["recommendations"]["count"] == 3

    @pytest.mark.asyncio
    async def test_daily_brief_all_helpers_fail(self):
        """When all helpers fail, status is partial_success with all errors."""
        from naukri_server.tools.daily_brief import naukri_daily_brief

        overrides = {path: RuntimeError("boom") for path in self._DAILY_BRIEF_PATCHES}
        patches = self._build_patches(overrides=overrides)
        ctx_managers = [patch(path, new=mock) for path, mock in patches.items()]

        for cm in ctx_managers:
            cm.__enter__()
        try:
            result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        assert result["status"] == "partial_success"
        assert len(result["errors"]) == 11
        # All sections should be zeroed/default
        assert result["unread_messages"]["count"] == 0
        assert result["activity_level"] == "UNKNOWN"
        assert result["subscription"] is None
