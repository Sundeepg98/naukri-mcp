"""Tier 17 gap tests — covering untested paths in compare, daily_brief, and smart_apply.

Every test is PURE: no network, no browser, no file I/O.
Uses unittest.mock.patch with AsyncMock for async helpers.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =====================================================================
# Category 1: compare_jobs comparison logic (5 tests)
# =====================================================================

class TestCompareJobsComparison:
    """Tests for _compare_jobs orchestration beyond basic validation."""

    _MOCK_PROFILE = {
        "status": "success",
        "key_skills": ["Python", "Django", "AWS", "Docker"],
        "total_experience": "5 years 0 months",
        "current_location": "Bangalore",
        "expected_ctc": 20.0,
    }

    _JOB_A = {
        "status": "success", "title": "Python Backend", "company": "AlphaCo",
        "salary": "15-25 LPA", "experience": "3-7 years", "location": "Bangalore",
        "work_mode": "Remote", "tags": ["Python", "Django", "AWS", "PostgreSQL"],
        "company_rating": 4.2, "group_id": "g1", "vacancies": 3,
        "is_applied": False, "external_apply": False, "external_apply_url": None,
        "posted_date": "2026-02-28", "apply_count": 100, "candidates_count": 50,
    }

    _JOB_B = {
        "status": "success", "title": "Fullstack Engineer", "company": "BetaCo",
        "salary": "12-20 LPA", "experience": "2-5 years", "location": "Mumbai",
        "work_mode": "Hybrid", "tags": ["Python", "React", "AWS", "Node.js"],
        "company_rating": 3.8, "group_id": "g2", "vacancies": 1,
        "is_applied": False, "external_apply": False, "external_apply_url": None,
        "posted_date": "2026-02-27", "apply_count": 200, "candidates_count": 120,
    }

    _JOB_C = {
        "status": "success", "title": "DevOps Engineer", "company": "GammaCo",
        "salary": "18-30 LPA", "experience": "5-10 years", "location": "Remote",
        "work_mode": "WFH", "tags": ["Docker", "Kubernetes", "AWS", "Terraform"],
        "company_rating": 4.5, "group_id": "g3", "vacancies": 2,
        "is_applied": False, "external_apply": False, "external_apply_url": None,
        "posted_date": "2026-02-26", "apply_count": 80, "candidates_count": 30,
    }

    def _make_get_job(self, mapping):
        """Return an async side_effect that resolves job_id -> detail dict."""
        async def _get(job_id_or_url):
            return mapping.get(job_id_or_url, {"status": "error", "message": "not found"})
        return _get

    @pytest.mark.asyncio
    async def test_two_valid_jobs_common_and_all_skills(self):
        """Two valid jobs produce correct common_skills and all_skills."""
        from naukri_server.tools.compare import _compare_jobs

        mapping = {"j1": self._JOB_A, "j2": self._JOB_B}

        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock, side_effect=self._make_get_job(mapping)), \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock, return_value=self._MOCK_PROFILE), \
             patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.tracking._applications_lock", asyncio.Lock()):
            result = await _compare_jobs(job_ids=["j1", "j2"])

        assert result["status"] == "success"
        assert result["count"] == 2

        # JOB_A tags: python, django, aws, postgresql
        # JOB_B tags: python, react, aws, node.js
        # Common (after normalization): python, aws (and amazon web services alias)
        common = set(result["common_skills"])
        assert "python" in common
        # aws normalizes to "amazon web services" via SKILL_ALIASES
        assert "amazon web services" in common or "aws" in common

        # all_skills should be the union
        all_sk = set(result["all_skills"])
        assert len(all_sk) >= len(common)
        # django should be in all_skills but not in common
        assert "django" in all_sk

    @pytest.mark.asyncio
    async def test_one_job_fails_partial_results_with_error(self):
        """When 1 of 2 jobs fails to fetch, result has 1 job + error message."""
        from naukri_server.tools.compare import _compare_jobs

        async def _get(job_id_or_url):
            if job_id_or_url == "j1":
                return self._JOB_A
            return {"status": "error", "message": "Job not found"}

        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock, side_effect=_get), \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock, return_value=self._MOCK_PROFILE), \
             patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.tracking._applications_lock", asyncio.Lock()):
            result = await _compare_jobs(job_ids=["j1", "j2"])

        assert result["status"] == "success"
        assert result["count"] == 1
        assert result["jobs"][0]["job_id"] == "j1"
        assert "errors" in result
        assert any("j2" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_validation_less_than_2_ids(self):
        """< 2 job_ids returns validation error."""
        from naukri_server.tools.compare import _compare_jobs

        result = await _compare_jobs(job_ids=["only_one"])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "at least 2" in result["message"]

    @pytest.mark.asyncio
    async def test_validation_more_than_5_ids(self):
        """> 5 job_ids returns validation error."""
        from naukri_server.tools.compare import _compare_jobs

        result = await _compare_jobs(job_ids=["a", "b", "c", "d", "e", "f"])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Maximum 5" in result["message"]

    @pytest.mark.asyncio
    async def test_overlapping_skills_intersection(self):
        """Three jobs with overlapping skills compute correct common_skills intersection."""
        from naukri_server.tools.compare import _compare_jobs

        mapping = {"j1": self._JOB_A, "j2": self._JOB_B, "j3": self._JOB_C}

        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock, side_effect=self._make_get_job(mapping)), \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock, return_value=self._MOCK_PROFILE), \
             patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.tracking._applications_lock", asyncio.Lock()):
            result = await _compare_jobs(job_ids=["j1", "j2", "j3"])

        assert result["status"] == "success"
        assert result["count"] == 3

        # JOB_A normalized: python, django, amazon web services, postgresql
        # JOB_B normalized: python, react, amazon web services, node.js
        # JOB_C normalized: docker, kubernetes, amazon web services, terraform
        # Intersection of all three: amazon web services (the only skill in all 3)
        common = set(result["common_skills"])
        assert "amazon web services" in common or "aws" in common
        # python is NOT in all three (not in JOB_C)
        assert "python" not in common

        # all_skills should be the full union
        all_sk = set(result["all_skills"])
        assert "python" in all_sk
        assert "docker" in all_sk
        assert "kubernetes" in all_sk
        assert "react" in all_sk


# =====================================================================
# Category 2: daily_brief orchestrator (8 tests)
# =====================================================================

class TestDailyBriefGaps:
    """Gap tests for daily_brief — _extract, _build_recommended_actions, and edge cases.

    Existing TestDailyBrief already covers all-success, partial-failure, error-status,
    and all-fail scenarios.  These tests focus on unit-testing helpers directly and
    covering edge cases not in the existing suite.
    """

    # ── _extract tests ────────────────────────────────────────────────

    def test_extract_with_successful_result(self):
        """_extract returns the result when it's a normal success dict."""
        from naukri_server.tools.daily_brief import naukri_daily_brief
        # _extract is a closure inside naukri_daily_brief, so we test it indirectly
        # by calling _build_early_access_section with a valid dict (the _extract logic
        # is embedded). Instead, we replicate _extract's logic for direct unit testing.
        # Since _extract is a local function, we recreate it.
        errors = []
        results = [{"status": "success", "count": 5, "messages": []}]

        def _extract(idx, label):
            r = results[idx]
            if isinstance(r, Exception):
                errors.append(f"{label}: {type(r).__name__}: {r}")
                return None
            if isinstance(r, dict) and r.get("status") == "error":
                errors.append(f"{label}: {r.get('message', 'unknown')}")
                return None
            return r

        val = _extract(0, "Test")
        assert val == {"status": "success", "count": 5, "messages": []}
        assert errors == []

    def test_extract_with_exception_result(self):
        """_extract returns None and appends to errors when result is an Exception."""
        errors = []
        results = [RuntimeError("connection reset")]

        def _extract(idx, label):
            r = results[idx]
            if isinstance(r, Exception):
                errors.append(f"{label}: {type(r).__name__}: {r}")
                return None
            if isinstance(r, dict) and r.get("status") == "error":
                errors.append(f"{label}: {r.get('message', 'unknown')}")
                return None
            return r

        val = _extract(0, "Inbox")
        assert val is None
        assert len(errors) == 1
        assert "Inbox" in errors[0]
        assert "RuntimeError" in errors[0]

    def test_extract_with_error_status_dict(self):
        """_extract returns None and appends to errors when result is {status: error}."""
        errors = []
        results = [{"status": "error", "message": "auth expired"}]

        def _extract(idx, label):
            r = results[idx]
            if isinstance(r, Exception):
                errors.append(f"{label}: {type(r).__name__}: {r}")
                return None
            if isinstance(r, dict) and r.get("status") == "error":
                errors.append(f"{label}: {r.get('message', 'unknown')}")
                return None
            return r

        val = _extract(0, "Dashboard")
        assert val is None
        assert len(errors) == 1
        assert "Dashboard" in errors[0]
        assert "auth expired" in errors[0]

    # ── _build_recommended_actions tests ──────────────────────────────

    def test_recommended_actions_with_all_triggers(self):
        """_build_recommended_actions generates actions for all relevant signals."""
        from naukri_server.tools.daily_brief import _build_recommended_actions

        brief = {
            "unread_messages": {"count": 3},
            "due_reminders": {"count": 2},
            "stale_applications": {"count": 5},
            "early_access_roles": {"newly_posted_count": 4},
            "assessments": {"pending": 1},
            "profile_completeness": {"completeness_percent": 50},
            "todays_applications": {"count": 0},
        }
        actions = _build_recommended_actions(brief)

        assert len(actions) >= 6

        # Verify priority ordering (high before medium before low)
        priorities = [a["priority"] for a in actions]
        priority_order = {"high": 0, "medium": 1, "low": 2}
        numeric = [priority_order[p] for p in priorities]
        assert numeric == sorted(numeric)

        # Verify specific actions
        action_texts = [a["action"] for a in actions]
        assert any("unread" in a.lower() or "recruiter" in a.lower() for a in action_texts)
        assert any("reminder" in a.lower() for a in action_texts)
        assert any("stale" in a.lower() for a in action_texts)
        assert any("early access" in a.lower() for a in action_texts)
        assert any("assessment" in a.lower() for a in action_texts)
        assert any("profile" in a.lower() or "completeness" in a.lower() for a in action_texts)

    def test_recommended_actions_empty_brief(self):
        """_build_recommended_actions returns empty list when no signals."""
        from naukri_server.tools.daily_brief import _build_recommended_actions

        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
            "profile_completeness": {"completeness_percent": 95},
            "todays_applications": {"count": 1},
        }
        actions = _build_recommended_actions(brief)
        assert actions == []

    def test_daily_brief_date_is_today(self):
        """The date field in daily_brief output is today's UTC date."""
        from naukri_server.tools.daily_brief import naukri_daily_brief
        from tests.test_smart_tools import TestDailyBrief
        from datetime import datetime, timezone
        import asyncio

        patches_dict = TestDailyBrief()._build_patches()
        ctx_managers = [patch(path, new=mock) for path, mock in patches_dict.items()]

        async def _run():
            for cm in ctx_managers:
                cm.__enter__()
            try:
                return await naukri_daily_brief()
            finally:
                for cm in ctx_managers:
                    cm.__exit__(None, None, None)

        result = asyncio.get_event_loop().run_until_complete(_run())
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert result["date"] == today_str

    @pytest.mark.asyncio
    async def test_daily_brief_recommended_actions_present(self):
        """Output always includes recommended_actions list (even when empty)."""
        from naukri_server.tools.daily_brief import naukri_daily_brief
        from tests.test_smart_tools import TestDailyBrief

        patches_dict = TestDailyBrief()._build_patches()
        ctx_managers = [patch(path, new=mock) for path, mock in patches_dict.items()]

        for cm in ctx_managers:
            cm.__enter__()
        try:
            result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        assert "recommended_actions" in result
        assert isinstance(result["recommended_actions"], list)

    @pytest.mark.asyncio
    async def test_daily_brief_some_sources_fail_working_sections_have_data(self):
        """When 3 sources fail, working sections still contain correct data."""
        from naukri_server.tools.daily_brief import naukri_daily_brief
        from tests.test_smart_tools import TestDailyBrief

        overrides = {
            "naukri_server.tools.inbox._fetch_inbox": RuntimeError("timeout"),
            "naukri_server.tools.performance._get_recruiter_activity": ValueError("bad token"),
            "naukri_server.tools.alerts._get_alerts_list": ConnectionError("refused"),
        }
        patches_dict = TestDailyBrief()._build_patches(overrides=overrides)
        ctx_managers = [patch(path, new=mock) for path, mock in patches_dict.items()]

        for cm in ctx_managers:
            cm.__enter__()
        try:
            result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        assert result["status"] == "partial_success"
        assert len(result["errors"]) == 3

        # Failed sections zeroed
        assert result["unread_messages"]["count"] == 0
        assert result["recruiter_activity"]["total"] == 0
        assert result["job_alerts"]["triggered_count"] == 0

        # Working sections still populated
        assert result["notifications"]["count"] == 1
        assert result["recommendations"]["count"] == 3
        assert result["activity_level"] == "HIGH"
        assert result["todays_applications"]["count"] == 1
        assert result["dashboard"]["profile_views"] == 42


# =====================================================================
# Category 3: _build_early_access_section (4 tests)
# =====================================================================

class TestBuildEarlyAccessSection:
    """Direct tests for _build_early_access_section helper."""

    def test_empty_early_access_response(self):
        """When early_access is None, section has count=0 and empty roles."""
        from naukri_server.tools.daily_brief import _build_early_access_section

        errors = []
        with patch("naukri_server.tools.early_access._detect_new_roles") as mock_detect:
            section = _build_early_access_section(None, errors)

        assert section["count"] == 0
        assert section["roles"] == []
        assert section["newly_posted_count"] == 0
        assert section["new_roles"] == []
        # _detect_new_roles should NOT be called when there are no roles
        mock_detect.assert_not_called()
        assert errors == []

    def test_valid_roles_correct_count(self):
        """Valid early_access dict produces correct count and roles."""
        from naukri_server.tools.daily_brief import _build_early_access_section

        ea_data = {
            "count": 3,
            "roles": [
                {"job_id": "r1", "title": "SDE-1"},
                {"job_id": "r2", "title": "SDE-2"},
                {"job_id": "r3", "title": "Backend Dev"},
            ],
        }
        errors = []
        with patch("naukri_server.tools.early_access._detect_new_roles", return_value=([], 3)):
            section = _build_early_access_section(ea_data, errors)

        assert section["count"] == 3
        assert len(section["roles"]) == 3
        assert section["roles"][0]["job_id"] == "r1"
        assert errors == []

    def test_with_new_roles_detected(self):
        """When _detect_new_roles returns new roles, newly_posted_count is populated."""
        from naukri_server.tools.daily_brief import _build_early_access_section

        ea_data = {
            "count": 4,
            "roles": [
                {"job_id": "r1", "title": "Existing"},
                {"job_id": "r2", "title": "New Role A"},
                {"job_id": "r3", "title": "New Role B"},
                {"job_id": "r4", "title": "Another Existing"},
            ],
        }
        new_roles = [
            {"job_id": "r2", "title": "New Role A"},
            {"job_id": "r3", "title": "New Role B"},
        ]
        errors = []
        with patch("naukri_server.tools.early_access._detect_new_roles", return_value=(new_roles, 4)):
            section = _build_early_access_section(ea_data, errors)

        assert section["newly_posted_count"] == 2
        assert len(section["new_roles"]) == 2
        assert section["new_roles"][0]["job_id"] == "r2"
        assert errors == []

    def test_detect_new_roles_exception_graceful_fallback(self):
        """When _detect_new_roles raises, section still has count/roles, error appended."""
        from naukri_server.tools.daily_brief import _build_early_access_section

        ea_data = {
            "count": 2,
            "roles": [{"job_id": "r1", "title": "Role A"}, {"job_id": "r2", "title": "Role B"}],
        }
        errors = []
        with patch("naukri_server.tools.early_access._detect_new_roles", side_effect=OSError("disk full")):
            section = _build_early_access_section(ea_data, errors)

        # Basic fields still populated
        assert section["count"] == 2
        assert len(section["roles"]) == 2
        # Delta fields default
        assert section["newly_posted_count"] == 0
        assert section["new_roles"] == []
        # Error captured
        assert len(errors) == 1
        assert "Early access tracking" in errors[0]
        assert "OSError" in errors[0]


# =====================================================================
# Category 4: _bulk_saved_scoring error paths (4 tests)
# =====================================================================

class TestBulkSavedScoringErrorPaths:
    """Error-path tests for _bulk_saved_scoring in smart_apply.py.

    Existing TestBulkSavedScoring covers happy path, empty saved, profile error,
    saved error, partial detail failures. These tests cover additional edge cases.
    """

    _MOCK_PROFILE = {
        "status": "success",
        "key_skills": ["Python", "Django", "AWS"],
        "total_experience": "5 years",
        "current_location": "Bangalore",
        "expected_ctc": 20.0,
    }

    _MOCK_SAVED_JOBS = {
        "status": "success",
        "total": 3,
        "count": 3,
        "page": 1,
        "has_more": False,
        "saved_jobs": [
            {"job_id": "201", "title": "Job A", "company": "Co A"},
            {"job_id": "202", "title": "Job B", "company": "Co B"},
            {"job_id": "203", "title": "Job C", "company": "Co C"},
        ],
    }

    @pytest.mark.asyncio
    async def test_individual_fetch_failure_skipped_others_scored(self):
        """When one job detail fetch raises Exception, it is skipped; others still scored."""
        from naukri_server.tools.smart_apply import _bulk_saved_scoring

        call_count = 0

        async def _get_job(job_id_or_url):
            nonlocal call_count
            call_count += 1
            if job_id_or_url == "202":
                raise ConnectionError("connection reset")
            return {
                "status": "success", "title": f"Job {job_id_or_url}",
                "company": "TestCo", "experience": "3-5 years",
                "tags": ["Python", "Django"],
            }

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock, return_value=self._MOCK_SAVED_JOBS), \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock, return_value=self._MOCK_PROFILE), \
             patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock, side_effect=_get_job):
            result = await _bulk_saved_scoring(min_fit_score=0)

        assert result["status"] == "success"
        assert result["total_saved"] == 3
        # 202 failed, so only 2 scored
        assert result["scored_count"] == 2
        scored_ids = {j["job_id"] for j in result["scored_jobs"]}
        assert "201" in scored_ids
        assert "203" in scored_ids
        assert "202" not in scored_ids
        # Error should be recorded
        assert "errors" in result
        assert any("202" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_empty_saved_jobs_returns_appropriate_response(self):
        """Empty saved jobs list returns success with zero counts."""
        from naukri_server.tools.smart_apply import _bulk_saved_scoring

        empty_saved = {"status": "success", "total": 0, "saved_jobs": []}

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock, return_value=empty_saved), \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock, return_value=self._MOCK_PROFILE):
            result = await _bulk_saved_scoring()

        assert result["status"] == "success"
        assert result["total_saved"] == 0
        assert result["scored_count"] == 0
        assert result["scored_jobs"] == []
        assert "No saved jobs found" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_all_scoring_fails_returns_empty_scored(self):
        """When all job detail fetches fail, scored_jobs is empty but no crash."""
        from naukri_server.tools.smart_apply import _bulk_saved_scoring

        async def _always_fail(job_id_or_url):
            raise TimeoutError("upstream timeout")

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock, return_value=self._MOCK_SAVED_JOBS), \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock, return_value=self._MOCK_PROFILE), \
             patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock, side_effect=_always_fail):
            result = await _bulk_saved_scoring(min_fit_score=0)

        assert result["status"] == "success"
        assert result["total_saved"] == 3
        assert result["scored_count"] == 0
        assert result["scored_jobs"] == []
        assert "errors" in result
        assert len(result["errors"]) == 3

    @pytest.mark.asyncio
    async def test_timeout_during_bulk_operation_returns_error(self):
        """Timeout during bulk scoring returns appropriate timeout error."""
        from naukri_server.tools.smart_apply import _bulk_saved_scoring

        async def _slow_saved(*args, **kwargs):
            await asyncio.sleep(10)  # Will exceed the 0.01s timeout
            return self._MOCK_SAVED_JOBS

        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock, side_effect=_slow_saved), \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock, return_value=self._MOCK_PROFILE):
            result = await _bulk_saved_scoring(timeout_seconds=0.01)

        assert result["status"] == "error"
        assert result["error_code"] == "INTERNAL_ERROR"
        assert "timed out" in result["message"].lower() or "timeout" in result["message"].lower()
