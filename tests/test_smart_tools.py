"""Tests for smart tools and daily brief — smart_apply, compare, auto_hunt, skill_gap, daily_brief.

Every test is PURE: no network, no browser, no file I/O.
We exercise validation logic and orchestration with mocked helpers.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =====================================================================
# 1. naukri_smart_apply
# =====================================================================

class TestSmartApply:
    """Tests for naukri_server.tools.smart_apply.naukri_smart_apply."""

    @pytest.mark.asyncio
    async def test_smart_apply_requires_job_id(self):
        """Calling without job_id and without action returns validation error."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply()
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_smart_apply_unknown_action(self):
        """Unknown action returns validation error."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(action="invalid_action")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "invalid_action" in result["message"]

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
# 1b. naukri_smart_apply(action="bulk_saved")
# =====================================================================

class TestBulkSavedScoring:
    """Tests for naukri_smart_apply(action='bulk_saved')."""

    # Reusable mock data
    _MOCK_SAVED_JOBS = {
        "status": "success",
        "total": 3,
        "count": 3,
        "page": 1,
        "has_more": False,
        "saved_jobs": [
            {"job_id": "101", "title": "Python Dev", "company": "AlphaCo", "salary": "15-25 LPA", "location": "Bangalore"},
            {"job_id": "102", "title": "Java Dev", "company": "BetaCo", "salary": "12-20 LPA", "location": "Mumbai"},
            {"job_id": "103", "title": "React Dev", "company": "GammaCo", "salary": "10-18 LPA", "location": "Remote"},
        ],
    }

    _MOCK_PROFILE = {
        "status": "success",
        "key_skills": ["Python", "Django", "AWS", "Docker"],
        "total_experience": "5 years 0 months",
        "current_location": "Bangalore",
        "expected_ctc": 20.0,
    }

    _MOCK_JOB_DETAILS = {
        "101": {
            "status": "success", "title": "Python Dev", "company": "AlphaCo",
            "salary": "15-25 LPA", "experience": "3-7 years", "location": "Bangalore",
            "work_mode": "Remote", "skills": ["Python", "Django", "AWS"],
        },
        "102": {
            "status": "success", "title": "Java Dev", "company": "BetaCo",
            "salary": "12-20 LPA", "experience": "5-8 years", "location": "Mumbai",
            "work_mode": "Office", "skills": ["Java", "Spring Boot", "MySQL"],
        },
        "103": {
            "status": "success", "title": "React Dev", "company": "GammaCo",
            "salary": "10-18 LPA", "experience": "2-5 years", "location": "Remote",
            "work_mode": "WFH", "skills": ["React", "JavaScript", "Node.js"],
        },
    }

    def _mock_get_job(self, job_id_or_url):
        """Return mock job detail based on job_id."""
        return self._MOCK_JOB_DETAILS.get(job_id_or_url, {"status": "error", "message": "not found"})

    @pytest.mark.asyncio
    async def test_bulk_saved_returns_scored_jobs(self):
        """bulk_saved returns scored and ranked saved jobs."""
        from naukri_server.tools.smart_apply import naukri_smart_apply

        mock_get_job = AsyncMock(side_effect=self._mock_get_job)

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.jobs.naukri_get_job", mock_get_job):
            mock_saved.return_value = self._MOCK_SAVED_JOBS
            mock_profile.return_value = self._MOCK_PROFILE

            result = await naukri_smart_apply(action="bulk_saved", min_fit_score=0)

        assert result["status"] == "success"
        assert result["total_saved"] == 3
        assert result["scored_count"] == 3
        assert len(result["scored_jobs"]) == 3

        # Verify sorted by fit_score descending
        scores = [j["fit_score"] for j in result["scored_jobs"]]
        assert scores == sorted(scores, reverse=True)

        # Python Dev should rank highest (matched skills: Python, Django, AWS + location + remote)
        top = result["scored_jobs"][0]
        assert top["job_id"] == "101"
        assert top["title"] == "Python Dev"
        assert "fit_details" in top
        assert top["fit_score"] > 0

    @pytest.mark.asyncio
    async def test_bulk_saved_empty_saved_jobs(self):
        """bulk_saved returns empty when no saved jobs."""
        from naukri_server.tools.smart_apply import naukri_smart_apply

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_saved.return_value = {"status": "success", "total": 0, "saved_jobs": []}
            mock_profile.return_value = self._MOCK_PROFILE

            result = await naukri_smart_apply(action="bulk_saved")

        assert result["status"] == "success"
        assert result["total_saved"] == 0
        assert result["scored_count"] == 0
        assert result["scored_jobs"] == []
        assert "No saved jobs found" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_bulk_saved_filters_by_min_score(self):
        """bulk_saved respects min_fit_score filter."""
        from naukri_server.tools.smart_apply import naukri_smart_apply

        mock_get_job = AsyncMock(side_effect=self._mock_get_job)

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.jobs.naukri_get_job", mock_get_job):
            mock_saved.return_value = self._MOCK_SAVED_JOBS
            mock_profile.return_value = self._MOCK_PROFILE

            # Use a high min_fit_score to filter out most jobs
            result = await naukri_smart_apply(action="bulk_saved", min_fit_score=80)

        assert result["status"] == "success"
        assert result["total_saved"] == 3
        assert result["min_fit_score"] == 80
        # All returned jobs must meet the threshold
        for job in result["scored_jobs"]:
            assert job["fit_score"] >= 80
        # scored_count should be <= total_saved
        assert result["scored_count"] <= result["total_saved"]

    @pytest.mark.asyncio
    async def test_bulk_saved_handles_saved_jobs_error(self):
        """bulk_saved returns error when saved jobs fetch fails."""
        from naukri_server.tools.smart_apply import naukri_smart_apply

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_saved.return_value = {"status": "error", "message": "auth expired"}
            mock_profile.return_value = self._MOCK_PROFILE

            result = await naukri_smart_apply(action="bulk_saved")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "saved jobs" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_bulk_saved_handles_profile_error(self):
        """bulk_saved returns error when profile fetch fails."""
        from naukri_server.tools.smart_apply import naukri_smart_apply

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_saved.return_value = self._MOCK_SAVED_JOBS
            mock_profile.return_value = {"status": "error", "message": "token expired"}

            result = await naukri_smart_apply(action="bulk_saved")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "profile" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_bulk_saved_handles_partial_job_detail_failures(self):
        """bulk_saved still scores jobs that succeed even when some detail fetches fail."""
        from naukri_server.tools.smart_apply import naukri_smart_apply

        async def _partial_get_job(job_id_or_url):
            if job_id_or_url == "102":
                return {"status": "error", "message": "not found"}
            return self._MOCK_JOB_DETAILS.get(job_id_or_url, {"status": "error", "message": "not found"})

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job:
            mock_saved.return_value = self._MOCK_SAVED_JOBS
            mock_profile.return_value = self._MOCK_PROFILE
            mock_job.side_effect = _partial_get_job

            result = await naukri_smart_apply(action="bulk_saved", min_fit_score=0)

        assert result["status"] == "success"
        assert result["total_saved"] == 3
        # Only 2 jobs scored (102 failed)
        assert result["scored_count"] == 2
        scored_ids = {j["job_id"] for j in result["scored_jobs"]}
        assert "101" in scored_ids
        assert "103" in scored_ids
        assert "102" not in scored_ids

    @pytest.mark.asyncio
    async def test_bulk_saved_min_fit_score_validation(self):
        """bulk_saved still validates min_fit_score range."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(action="bulk_saved", min_fit_score=101)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "min_fit_score" in result["message"]


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
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock) as mock_assess:
            mock_recs.return_value = {"status": "success", "jobs": []}
            mock_profile.return_value = {"status": "success", "key_skills": []}
            mock_assess.return_value = {"status": "success", "assessments": []}
            result = await naukri_skill_gap_analysis(sample_size=200)
            # Verify recommendations was called with clamped limit=50
            _, call_kwargs = mock_recs.call_args
            assert call_kwargs["limit"] == 50


# =====================================================================
# 5. naukri_daily_brief
# =====================================================================

class TestDailyBrief:
    """Tests for naukri_server.tools.daily_brief.naukri_daily_brief.

    All 16 internal helpers are mocked at their source modules — no network, no browser.
    """

    # All 16 helpers patched at their source modules (where they are defined).
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
        "naukri_server.tools.alerts._get_alerts_list",
        "naukri_server.tools.assessments._get_profile_completeness",
        "naukri_server.tools.tracking._list_saved_jobs",
        "naukri_server.tools.performance._get_search_impressions",
        "naukri_server.tools.assessments._list_assessments",
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
        {"status": "success", "alerts": [{"id": "al1"}, {"id": "al2"}]},
        {"status": "success", "completeness_percent": 85},
        {"status": "success", "total": 5, "saved_jobs": []},
        {"status": "success", "total_appearances": 200, "days": 7},
        {"status": "success", "assessments": [{"skill": "Python", "status": "passed"}]},
    ]

    # Short labels matching the patch order (for override dict keys)
    _PATCH_LABELS = [
        "inbox", "notifications", "recommendations", "recruiter_activity",
        "activity_level", "applications", "dashboard", "early_access",
        "subscription", "reminders", "stale_applications",
        "job_alerts", "profile_completeness", "saved_jobs",
        "search_impressions", "assessments",
    ]

    # Patch for _detect_new_roles (called synchronously from _build_early_access_section).
    _DETECT_NEW_ROLES_PATCH = "naukri_server.tools.early_access._detect_new_roles"

    def _build_patches(self, overrides=None):
        """Create a dict of patch path -> AsyncMock with return values.

        Also includes a default mock for _detect_new_roles (sync, returns no new roles).

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
        # _detect_new_roles is synchronous, default: no new roles
        dnr_path = self._DETECT_NEW_ROLES_PATCH
        dnr_mock = MagicMock(return_value=([], 0))
        if overrides and dnr_path in overrides:
            override = overrides[dnr_path]
            if isinstance(override, Exception):
                dnr_mock.side_effect = override
            else:
                dnr_mock.return_value = override
        patches[dnr_path] = dnr_mock
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

        # All 16 sections must be present
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
            "job_alerts",
            "profile_completeness",
            "saved_jobs",
            "search_impressions",
            "assessments",
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
        # New sections
        assert result["job_alerts"]["triggered_count"] == 2
        assert result["profile_completeness"]["completeness_percent"] == 85
        assert result["saved_jobs"]["total"] == 5
        assert result["search_impressions"]["total_appearances"] == 200
        assert result["assessments"]["total"] == 1

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
        assert len(result["errors"]) == 16
        # All sections should be zeroed/default
        assert result["unread_messages"]["count"] == 0
        assert result["activity_level"] == "UNKNOWN"
        assert result["subscription"] is None
        assert result["profile_completeness"] is None
        assert result["search_impressions"] is None
        assert result["saved_jobs"]["total"] == 0
        assert result["assessments"]["total"] == 0
        assert result["job_alerts"]["triggered_count"] == 0


# =====================================================================
# 6. naukri_mock_interview(action="prep")
# =====================================================================

class TestInterviewPrep:
    """Tests for naukri_mock_interview(action='prep')."""

    @pytest.mark.asyncio
    async def test_prep_requires_job_id(self):
        """prep action requires job_id."""
        from naukri_server.tools.mock_interview import naukri_mock_interview
        result = await naukri_mock_interview(action="prep")
        assert result["status"] == "error"
        assert "job_id" in result["message"].lower() or "prep requires" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_prep_returns_job_summary(self):
        """prep returns job summary and preparation guide."""
        from naukri_server.tools.mock_interview import naukri_mock_interview
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job, \
             patch("naukri_server.tools.mock_interview._get_topics", new_callable=AsyncMock) as mock_topics:
            mock_job.return_value = {
                "status": "success", "title": "Python Dev", "company": "Acme Corp",
                "salary": "15-25 LPA", "experience": "5-8 yrs", "location": "Bangalore",
                "skills": ["Python", "Django", "AWS"],
            }
            mock_topics.return_value = {"status": "success", "topics": [{"name": "Python"}]}
            # AmbitionBox helpers — patch at source since _interview_prep imports them locally
            with patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock) as mock_iv, \
                 patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock) as mock_rev:
                mock_iv.return_value = {
                    "status": "success", "total_interviews": 50,
                    "overall_difficulty": {"easy": "30%", "moderate": "50%", "hard": "20%"},
                    "interview_experiences": [
                        {"designation": "SDE", "questions": ["Q1", "Q2"]},
                    ],
                }
                mock_rev.return_value = {
                    "status": "success", "overall_rating": 4.2,
                    "category_ratings": {"Work-Life Balance": 3.8},
                }
                result = await naukri_mock_interview(action="prep", job_id="123")
            assert result["status"] == "success"
            assert result["job_summary"]["title"] == "Python Dev"
            assert result["job_summary"]["company"] == "Acme Corp"
            assert result["preparation_guide"]["key_skills"] == ["Python", "Django", "AWS"]
            assert result["company_interviews"]["total_interviews"] == 50
            assert result["company_reviews"]["overall_rating"] == 4.2
            assert len(result["mock_interview_topics"]) == 1

    @pytest.mark.asyncio
    async def test_prep_handles_job_fetch_failure(self):
        """prep returns error when job fetch fails."""
        from naukri_server.tools.mock_interview import naukri_mock_interview
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job:
            mock_job.return_value = {"status": "error", "message": "Job not found"}
            result = await naukri_mock_interview(action="prep", job_id="999")
            assert result["status"] == "error"
            assert "job not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_prep_partial_success_when_ambitionbox_fails(self):
        """prep returns partial_success when AmbitionBox data is unavailable."""
        from naukri_server.tools.mock_interview import naukri_mock_interview
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job, \
             patch("naukri_server.tools.mock_interview._get_topics", new_callable=AsyncMock) as mock_topics, \
             patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock) as mock_iv, \
             patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock) as mock_rev:
            mock_job.return_value = {
                "status": "success", "title": "Python Dev", "company": "Acme Corp",
                "salary": "15-25 LPA", "experience": "5-8 yrs", "location": "Bangalore",
                "skills": ["Python"],
            }
            mock_topics.return_value = {"status": "success", "topics": []}
            mock_iv.return_value = {"status": "error", "message": "timeout"}
            mock_rev.return_value = {"status": "error", "message": "timeout"}
            result = await naukri_mock_interview(action="prep", job_id="123")
            assert result["status"] == "partial_success"
            assert "errors" in result
            assert result["company_interviews"] is None
            assert result["company_reviews"] is None

    @pytest.mark.asyncio
    async def test_prep_no_company_name(self):
        """prep returns partial_success when job has no company name."""
        from naukri_server.tools.mock_interview import naukri_mock_interview
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job, \
             patch("naukri_server.tools.mock_interview._get_topics", new_callable=AsyncMock) as mock_topics:
            mock_job.return_value = {
                "status": "success", "title": "Python Dev", "company": "",
                "salary": "15-25 LPA", "experience": "5-8 yrs", "location": "Bangalore",
                "skills": ["Python"],
            }
            mock_topics.return_value = {"status": "success", "topics": []}
            result = await naukri_mock_interview(action="prep", job_id="123")
            assert result["status"] == "partial_success"
            assert any("company" in e.lower() for e in result["errors"])


# =====================================================================
# 7. naukri_applications(action="follow_up")
# =====================================================================

class TestApplicationFollowUp:
    """Tests for naukri_applications(action='follow_up')."""

    @pytest.mark.asyncio
    async def test_follow_up_returns_action_items(self):
        """follow_up returns prioritized action items for stale applications."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.tracking._get_stale_applications", new_callable=AsyncMock) as mock_stale, \
             patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_inbox, \
             patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_rem:
            mock_stale.return_value = {
                "status": "success",
                "stale_applications": [
                    {"job_id": "1", "title": "Dev", "company": "Acme", "applied_date": "2026-01-01", "stale_score": 80, "reasons": ["No response"]},
                ],
            }
            mock_inbox.return_value = {"status": "success", "messages": []}
            mock_rem.return_value = {"status": "success", "total": 0, "due_count": 0, "reminders": []}
            result = await naukri_applications(action="follow_up")
            assert result["status"] == "success"
            assert result["summary"]["total_stale"] == 1
            assert len(result["action_items"]) == 1
            # stale_score 80 >= 70 → medium priority
            assert result["action_items"][0]["priority"] == "medium"

    @pytest.mark.asyncio
    async def test_follow_up_stale_failure(self):
        """follow_up returns error when stale detection fails."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.tracking._get_stale_applications", new_callable=AsyncMock) as mock_stale, \
             patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_inbox, \
             patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_rem:
            mock_stale.return_value = {"status": "error", "message": "Failed"}
            mock_inbox.return_value = {"status": "success", "messages": []}
            mock_rem.return_value = {"status": "success", "total": 0, "due_count": 0, "reminders": []}
            result = await naukri_applications(action="follow_up")
            assert result["status"] == "error"
            assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    async def test_follow_up_recruiter_contact_high_priority(self):
        """follow_up marks apps with recruiter messages as high priority."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.tracking._get_stale_applications", new_callable=AsyncMock) as mock_stale, \
             patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_inbox, \
             patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_rem:
            mock_stale.return_value = {
                "status": "success",
                "stale_applications": [
                    {"job_id": "1", "title": "Dev", "company": "Acme Corp", "applied_date": "2026-01-01", "stale_score": 50, "reasons": []},
                ],
            }
            mock_inbox.return_value = {
                "status": "success",
                "messages": [
                    {
                        "subject": "We'd like to interview you",
                        "date": "2026-02-20",
                        "sender": "HR Team",
                        "company_details": {"company_name": "Acme Corp"},
                    },
                ],
            }
            mock_rem.return_value = {"status": "success", "total": 0, "due_count": 0, "reminders": []}
            result = await naukri_applications(action="follow_up")
            assert result["status"] == "success"
            assert result["summary"]["with_recruiter_contact"] == 1
            assert result["action_items"][0]["priority"] == "high"
            assert "respond" in result["action_items"][0]["action"].lower()

    @pytest.mark.asyncio
    async def test_follow_up_due_reminder_high_priority(self):
        """follow_up marks apps with due reminders as high priority."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.tracking._get_stale_applications", new_callable=AsyncMock) as mock_stale, \
             patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_inbox, \
             patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_rem:
            mock_stale.return_value = {
                "status": "success",
                "stale_applications": [
                    {"job_id": "42", "title": "SDE", "company": "TechCo", "applied_date": "2026-01-10", "stale_score": 50, "reasons": []},
                ],
            }
            mock_inbox.return_value = {"status": "success", "messages": []}
            mock_rem.return_value = {
                "status": "success", "total": 1, "due_count": 1,
                "reminders": [{"job_id": "42", "is_due": True, "note": "Follow up"}],
            }
            result = await naukri_applications(action="follow_up")
            assert result["status"] == "success"
            assert result["summary"]["with_pending_reminder"] == 1
            assert result["action_items"][0]["priority"] == "high"
            assert "reminder" in result["action_items"][0]["action"].lower()

    @pytest.mark.asyncio
    async def test_follow_up_partial_success_inbox_failure(self):
        """follow_up returns partial_success when inbox fetch fails."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.tracking._get_stale_applications", new_callable=AsyncMock) as mock_stale, \
             patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_inbox, \
             patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_rem:
            mock_stale.return_value = {
                "status": "success",
                "stale_applications": [
                    {"job_id": "1", "title": "Dev", "company": "Acme", "applied_date": "2026-01-01", "stale_score": 50, "reasons": []},
                ],
            }
            mock_inbox.side_effect = RuntimeError("API timeout")
            mock_rem.return_value = {"status": "success", "total": 0, "due_count": 0, "reminders": []}
            result = await naukri_applications(action="follow_up")
            assert result["status"] == "partial_success"
            assert "errors" in result
            assert any("Inbox" in e for e in result["errors"])
            # Should still have action items
            assert len(result["action_items"]) == 1

    @pytest.mark.asyncio
    async def test_follow_up_low_priority_for_low_stale_score(self):
        """follow_up assigns low priority when stale_score < 70 and no inbox/reminder matches."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.tracking._get_stale_applications", new_callable=AsyncMock) as mock_stale, \
             patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_inbox, \
             patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_rem:
            mock_stale.return_value = {
                "status": "success",
                "stale_applications": [
                    {"job_id": "1", "title": "Dev", "company": "Acme", "applied_date": "2026-01-01", "stale_score": 45, "reasons": []},
                ],
            }
            mock_inbox.return_value = {"status": "success", "messages": []}
            mock_rem.return_value = {"status": "success", "total": 0, "due_count": 0, "reminders": []}
            result = await naukri_applications(action="follow_up")
            assert result["status"] == "success"
            assert result["action_items"][0]["priority"] == "low"
            assert "monitor" in result["action_items"][0]["action"].lower()

    @pytest.mark.asyncio
    async def test_follow_up_priority_sorting(self):
        """follow_up sorts action items: high first, then medium, then low."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.tracking._get_stale_applications", new_callable=AsyncMock) as mock_stale, \
             patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_inbox, \
             patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_rem:
            mock_stale.return_value = {
                "status": "success",
                "stale_applications": [
                    {"job_id": "1", "title": "Low", "company": "LowCo", "applied_date": "2026-01-01", "stale_score": 45, "reasons": []},
                    {"job_id": "2", "title": "Medium", "company": "MedCo", "applied_date": "2026-01-01", "stale_score": 75, "reasons": []},
                    {"job_id": "3", "title": "High", "company": "HighCo", "applied_date": "2026-01-01", "stale_score": 50, "reasons": []},
                ],
            }
            mock_inbox.return_value = {
                "status": "success",
                "messages": [
                    {"subject": "Interview", "date": "2026-02-20", "sender": "HR", "company_details": {"company_name": "HighCo"}},
                ],
            }
            mock_rem.return_value = {"status": "success", "total": 0, "due_count": 0, "reminders": []}
            result = await naukri_applications(action="follow_up")
            priorities = [item["priority"] for item in result["action_items"]]
            assert priorities == ["high", "medium", "low"]

    @pytest.mark.asyncio
    async def test_follow_up_empty_stale_apps(self):
        """follow_up returns empty lists when no stale applications exist."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.tracking._get_stale_applications", new_callable=AsyncMock) as mock_stale, \
             patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_inbox, \
             patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_rem:
            mock_stale.return_value = {"status": "success", "stale_applications": []}
            mock_inbox.return_value = {"status": "success", "messages": []}
            mock_rem.return_value = {"status": "success", "total": 0, "due_count": 0, "reminders": []}
            result = await naukri_applications(action="follow_up")
            assert result["status"] == "success"
            assert result["summary"]["total_stale"] == 0
            assert result["stale_applications"] == []
            assert result["action_items"] == []


# =====================================================================
# 8. naukri_skill_gap_analysis — assessment integration
# =====================================================================

class TestSkillGapAssessments:
    """Tests for assessment integration in skill_gap_analysis."""

    # Shared fixtures -----------------------------------------------

    _MOCK_JOBS = {
        "status": "success",
        "jobs": [
            {"job_id": "1", "title": "Backend Dev", "company": "Acme", "tags": ["Python", "Django", "AWS", "Docker"]},
            {"job_id": "2", "title": "Full Stack", "company": "Globex", "tags": ["Python", "React", "AWS", "SQL"]},
            {"job_id": "3", "title": "Data Engineer", "company": "Initech", "tags": ["Python", "SQL", "Kafka", "Docker"]},
        ],
    }

    _MOCK_PROFILE = {
        "status": "success",
        "key_skills": ["Python", "Django", "SQL", "Docker"],
        "skills_with_experience": [
            {"skill": "Python", "experience_years": 4, "experience_months": 0},
            {"skill": "Django", "experience_years": 2, "experience_months": 6},
        ],
    }

    _MOCK_ASSESSMENTS = {
        "status": "success",
        "total": 2,
        "count": 2,
        "page": 1,
        "has_more": False,
        "assessments": [
            {"skill": "Python", "status": "passed", "score": 85, "level": "Advanced"},
            {"skill": "SQL", "status": "passed", "score": 72, "level": "Intermediate"},
            {"skill": "React", "status": "failed", "score": 30, "level": ""},
        ],
    }

    @pytest.mark.asyncio
    async def test_skill_gap_with_assessments_boost(self):
        """Passed assessments boost matched skill frequency by 2x."""
        from naukri_server.tools.skill_gap import naukri_skill_gap_analysis

        with patch("naukri_server.tools.search.naukri_get_recommendations", new_callable=AsyncMock) as mock_recs, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock) as mock_assess:
            mock_recs.return_value = self._MOCK_JOBS
            mock_profile.return_value = self._MOCK_PROFILE
            mock_assess.return_value = self._MOCK_ASSESSMENTS

            result = await naukri_skill_gap_analysis()

        assert result["status"] == "success"
        assert result["assessments_used"] == 2  # Python + SQL passed

        # Build lookup for strong skills
        strong_map = {s["skill"]: s for s in result["strong_skills"]}

        # Python matched in all 3 jobs -> base freq 3, boosted to 6
        assert strong_map["python"]["frequency"] == 6
        assert strong_map["python"]["assessment_passed"] is True

        # SQL matched in 2 jobs -> base freq 2, boosted to 4
        assert strong_map["sql"]["frequency"] == 4
        assert strong_map["sql"]["assessment_passed"] is True

        # Django matched in 1 job -> base freq 1, no boost (no passed assessment)
        assert strong_map["django"]["frequency"] == 1
        assert "assessment_passed" not in strong_map["django"]

        # Docker matched in 2 jobs -> base freq 2, no boost
        assert strong_map["docker"]["frequency"] == 2
        assert "assessment_passed" not in strong_map["docker"]

    @pytest.mark.asyncio
    async def test_skill_gap_without_assessments(self):
        """include_assessments=False skips assessment fetch entirely."""
        from naukri_server.tools.skill_gap import naukri_skill_gap_analysis

        with patch("naukri_server.tools.search.naukri_get_recommendations", new_callable=AsyncMock) as mock_recs, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock) as mock_assess:
            mock_recs.return_value = self._MOCK_JOBS
            mock_profile.return_value = self._MOCK_PROFILE
            mock_assess.return_value = self._MOCK_ASSESSMENTS

            result = await naukri_skill_gap_analysis(include_assessments=False)

        assert result["status"] == "success"
        assert result["assessments_used"] == 0

        # _list_assessments should NOT have been called
        mock_assess.assert_not_called()

        # No boosting — Python base freq 3
        strong_map = {s["skill"]: s for s in result["strong_skills"]}
        assert strong_map["python"]["frequency"] == 3
        assert "assessment_passed" not in strong_map["python"]

    @pytest.mark.asyncio
    async def test_skill_gap_assessments_fetch_failure_non_fatal(self):
        """Assessment fetch failure is non-fatal — analysis proceeds without boost."""
        from naukri_server.tools.skill_gap import naukri_skill_gap_analysis

        with patch("naukri_server.tools.search.naukri_get_recommendations", new_callable=AsyncMock) as mock_recs, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock) as mock_assess:
            mock_recs.return_value = self._MOCK_JOBS
            mock_profile.return_value = self._MOCK_PROFILE
            mock_assess.side_effect = RuntimeError("API timeout")

            result = await naukri_skill_gap_analysis()

        assert result["status"] == "success"
        assert result["assessments_used"] == 0

        # No boosting — Python base freq 3
        strong_map = {s["skill"]: s for s in result["strong_skills"]}
        assert strong_map["python"]["frequency"] == 3
        assert "assessment_passed" not in strong_map["python"]

    @pytest.mark.asyncio
    async def test_skill_gap_assessments_error_status_non_fatal(self):
        """Assessment returning error status is non-fatal — analysis proceeds."""
        from naukri_server.tools.skill_gap import naukri_skill_gap_analysis

        with patch("naukri_server.tools.search.naukri_get_recommendations", new_callable=AsyncMock) as mock_recs, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock) as mock_assess:
            mock_recs.return_value = self._MOCK_JOBS
            mock_profile.return_value = self._MOCK_PROFILE
            mock_assess.return_value = {"status": "error", "message": "auth expired"}

            result = await naukri_skill_gap_analysis()

        assert result["status"] == "success"
        assert result["assessments_used"] == 0

        # No boosting
        strong_map = {s["skill"]: s for s in result["strong_skills"]}
        assert strong_map["python"]["frequency"] == 3

    @pytest.mark.asyncio
    async def test_skill_gap_assessment_boost_reorders_skills(self):
        """Boosted skills should sort higher than non-boosted skills with same base freq."""
        from naukri_server.tools.skill_gap import naukri_skill_gap_analysis

        # Docker matched in 3 jobs, Python matched in 2 jobs
        # But Python has passed assessment so 2*2=4 > Docker's 3
        jobs = {
            "status": "success",
            "jobs": [
                {"job_id": "1", "title": "A", "company": "X", "tags": ["Python", "Docker"]},
                {"job_id": "2", "title": "B", "company": "Y", "tags": ["Python", "Docker"]},
                {"job_id": "3", "title": "C", "company": "Z", "tags": ["Docker"]},
            ],
        }
        profile = {"status": "success", "key_skills": ["Python", "Docker"], "skills_with_experience": []}
        assessments = {
            "status": "success", "total": 1, "count": 1, "page": 1, "has_more": False,
            "assessments": [{"skill": "Python", "status": "passed", "score": 90, "level": "Expert"}],
        }

        with patch("naukri_server.tools.search.naukri_get_recommendations", new_callable=AsyncMock) as mock_recs, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock) as mock_assess:
            mock_recs.return_value = jobs
            mock_profile.return_value = profile
            mock_assess.return_value = assessments

            result = await naukri_skill_gap_analysis()

        assert result["status"] == "success"
        # Python: base 2, boosted to 4; Docker: base 3, no boost
        # Python (4) should sort above Docker (3)
        assert result["strong_skills"][0]["skill"] == "python"
        assert result["strong_skills"][0]["frequency"] == 4
        assert result["strong_skills"][0]["assessment_passed"] is True
        assert result["strong_skills"][1]["skill"] == "docker"
        assert result["strong_skills"][1]["frequency"] == 3


# =====================================================================
# 9. Early Access Role Delta Tracking
# =====================================================================

class TestEarlyAccessTracking:
    """Tests for early access role delta tracking (_detect_new_roles)."""

    def test_detect_new_roles_first_run(self):
        """First run — all roles are new since tracking file is empty."""
        from naukri_server.tools.early_access import _detect_new_roles
        roles = [
            {"job_id": "100", "title": "Dev A"},
            {"job_id": "200", "title": "Dev B"},
            {"job_id": "300", "title": "Dev C"},
        ]
        with patch("naukri_server.tools.early_access._load_seen_roles", return_value=set()), \
             patch("naukri_server.tools.early_access._save_seen_roles") as mock_save:
            new_roles, total = _detect_new_roles(roles)
            assert len(new_roles) == 3
            assert total == 3
            # All three IDs should be saved
            saved_ids = mock_save.call_args[0][0]
            assert saved_ids == {"100", "200", "300"}

    def test_detect_new_roles_with_existing(self):
        """Second run — only truly new roles are flagged."""
        from naukri_server.tools.early_access import _detect_new_roles
        roles = [
            {"job_id": "100", "title": "Dev A"},
            {"job_id": "200", "title": "Dev B"},
            {"job_id": "400", "title": "Dev D"},
        ]
        with patch("naukri_server.tools.early_access._load_seen_roles", return_value={"100", "200", "300"}), \
             patch("naukri_server.tools.early_access._save_seen_roles") as mock_save:
            new_roles, total = _detect_new_roles(roles)
            # Only 400 is new
            assert len(new_roles) == 1
            assert new_roles[0]["job_id"] == "400"
            assert total == 3
            # Saved set includes old 300 plus current
            saved_ids = mock_save.call_args[0][0]
            assert saved_ids == {"100", "200", "300", "400"}

    def test_detect_new_roles_no_new(self):
        """All current roles already seen — no new roles."""
        from naukri_server.tools.early_access import _detect_new_roles
        roles = [
            {"job_id": "100", "title": "Dev A"},
            {"job_id": "200", "title": "Dev B"},
        ]
        with patch("naukri_server.tools.early_access._load_seen_roles", return_value={"100", "200"}), \
             patch("naukri_server.tools.early_access._save_seen_roles") as mock_save:
            new_roles, total = _detect_new_roles(roles)
            assert len(new_roles) == 0
            assert total == 2

    def test_detect_new_roles_empty_input(self):
        """Empty roles list — nothing new, nothing saved beyond prior seen."""
        from naukri_server.tools.early_access import _detect_new_roles
        with patch("naukri_server.tools.early_access._load_seen_roles", return_value={"100"}), \
             patch("naukri_server.tools.early_access._save_seen_roles") as mock_save:
            new_roles, total = _detect_new_roles([])
            assert len(new_roles) == 0
            assert total == 0
            # Seen set should still include old IDs
            saved_ids = mock_save.call_args[0][0]
            assert "100" in saved_ids

    def test_detect_new_roles_skips_empty_job_id(self):
        """Roles without a job_id are skipped."""
        from naukri_server.tools.early_access import _detect_new_roles
        roles = [
            {"job_id": "", "title": "No ID"},
            {"title": "Missing key"},
            {"job_id": "500", "title": "Valid"},
        ]
        with patch("naukri_server.tools.early_access._load_seen_roles", return_value=set()), \
             patch("naukri_server.tools.early_access._save_seen_roles") as mock_save:
            new_roles, total = _detect_new_roles(roles)
            assert len(new_roles) == 1
            assert new_roles[0]["job_id"] == "500"
            assert total == 1

    def test_load_seen_roles_returns_set_from_dict(self):
        """_load_seen_roles extracts IDs from a dict payload."""
        from naukri_server.tools.early_access import _load_seen_roles
        with patch("naukri_server.tools.early_access.load_json_with_backup",
                    return_value={"seen_role_ids": ["10", "20", "30"]}):
            result = _load_seen_roles()
            assert result == {"10", "20", "30"}

    def test_load_seen_roles_returns_empty_for_list(self):
        """_load_seen_roles returns empty set when file returns a list (no dict)."""
        from naukri_server.tools.early_access import _load_seen_roles
        with patch("naukri_server.tools.early_access.load_json_with_backup",
                    return_value=[]):
            result = _load_seen_roles()
            assert result == set()

    def test_load_seen_roles_returns_empty_for_missing_key(self):
        """_load_seen_roles returns empty set when dict has no seen_role_ids key."""
        from naukri_server.tools.early_access import _load_seen_roles
        with patch("naukri_server.tools.early_access.load_json_with_backup",
                    return_value={"other_key": []}):
            result = _load_seen_roles()
            assert result == set()


class TestDailyBriefEarlyAccessDelta:
    """Tests for newly_posted_count and new_roles in the daily brief early access section."""

    # Reuse TestDailyBrief's patch infrastructure
    _DAILY_BRIEF_PATCHES = TestDailyBrief._DAILY_BRIEF_PATCHES
    _MOCK_RETURNS = list(TestDailyBrief._MOCK_RETURNS)  # copy

    def _build_patches(self, overrides=None):
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
    async def test_daily_brief_includes_new_role_fields(self):
        """Daily brief early_access_roles section includes newly_posted_count and new_roles."""
        from naukri_server.tools.daily_brief import naukri_daily_brief

        patches = self._build_patches()
        ctx_managers = [patch(path, new=mock) for path, mock in patches.items()]

        for cm in ctx_managers:
            cm.__enter__()
        try:
            # Mock _detect_new_roles to return 1 new role
            with patch("naukri_server.tools.early_access._detect_new_roles",
                        return_value=([{"job_id": "r2", "title": "New Role"}], 2)):
                result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        ea = result["early_access_roles"]
        assert "newly_posted_count" in ea
        assert ea["newly_posted_count"] == 1
        assert len(ea["new_roles"]) == 1
        assert ea["new_roles"][0]["job_id"] == "r2"

    @pytest.mark.asyncio
    async def test_daily_brief_no_early_access_data(self):
        """When early access fetch fails, newly_posted_count defaults to 0."""
        from naukri_server.tools.daily_brief import naukri_daily_brief

        overrides = {
            "naukri_server.tools.early_access._list_early_access_roles": RuntimeError("timeout"),
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

        ea = result["early_access_roles"]
        assert ea["count"] == 0
        assert ea["newly_posted_count"] == 0
        assert ea["new_roles"] == []

    @pytest.mark.asyncio
    async def test_daily_brief_tracking_error_graceful(self):
        """When _detect_new_roles raises, section still has count/roles, error logged."""
        from naukri_server.tools.daily_brief import naukri_daily_brief

        patches = self._build_patches()
        ctx_managers = [patch(path, new=mock) for path, mock in patches.items()]

        for cm in ctx_managers:
            cm.__enter__()
        try:
            with patch("naukri_server.tools.early_access._detect_new_roles",
                        side_effect=OSError("disk full")):
                result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        ea = result["early_access_roles"]
        # Should still have the basic fields
        assert ea["count"] == 2
        assert len(ea["roles"]) == 2
        # Delta fields default to 0/empty
        assert ea["newly_posted_count"] == 0
        assert ea["new_roles"] == []
        # Error should be captured
        assert result["status"] == "partial_success"
        assert any("Early access tracking" in e for e in result["errors"])
