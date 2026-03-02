"""Deep unit tests for naukri_server.tools.daily_brief.

Covers:
- _build_recommended_actions: each priority level, combined, empty brief
- _build_early_access_section: with roles, without roles, tracking error
- naukri_daily_brief: all 17 tasks succeed, partial failures, IST date
- Fit scoring enrichment: scores present when profile available, graceful degradation
- Match quality section: present when available, null when not
- Activity level fallback to "UNKNOWN" when missing
- Pending assessment count calculation

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ===========================================================================
# Helpers — shared fixtures
# ===========================================================================

def _make_inbox(count=0, messages=None):
    return {"status": "success", "count": count, "messages": messages or []}


def _make_notifs(count=0, notifications=None):
    return {"status": "success", "count": count, "notifications": notifications or []}


def _make_recs(count=0, jobs=None):
    return {"status": "success", "count": count, "jobs": jobs or []}


def _make_recruiter(total=0, change=None, activities=None):
    return {
        "status": "success",
        "total_actions": total,
        "percentage_change": change,
        "activities": activities or [],
    }


def _make_activity(level="ACTIVE"):
    return {"status": "success", "level": level}


def _make_apps(count=0, applications=None):
    return {"status": "success", "count": count, "applications": applications or []}


def _make_dashboard(views=0, matches=0, invites=0):
    return {
        "status": "success",
        "profile_views": views,
        "total_matches": matches,
        "unread_invites": invites,
    }


def _make_early_access(count=0, roles=None):
    return {"status": "success", "count": count, "roles": roles or []}


def _make_subscription():
    return {"status": "success", "plan": "Free"}


def _make_reminders(due_count=0, reminders=None):
    return {"status": "success", "due_count": due_count, "reminders": reminders or []}


def _make_stale(total=0, apps=None):
    return {"status": "success", "total": total, "stale_applications": apps or []}


def _make_alerts(alerts=None):
    return {"status": "success", "alerts": alerts or []}


def _make_completeness(pct=90):
    return {"status": "success", "completeness_percent": pct}


def _make_saved(total=0):
    return {"status": "success", "total": total}


def _make_impressions():
    return {"status": "success", "total_appearances": 100}


def _make_assessments(items=None):
    return {"status": "success", "assessments": items or []}


def _make_match_quality():
    return {"status": "success", "match_score": 75}


def _all_good_results(
    inbox_count=0,
    recs_jobs=None,
    activity_level="ACTIVE",
    apps_count=0,
    early_roles=None,
    reminders_due=0,
    stale_total=0,
    completeness_pct=90,
    assessment_items=None,
    match_quality=None,
):
    """Return a 17-element list matching asyncio.gather order in naukri_daily_brief."""
    return [
        _make_inbox(count=inbox_count),                            # 0
        _make_notifs(count=0),                                     # 1
        _make_recs(count=len(recs_jobs or []), jobs=recs_jobs),    # 2
        _make_recruiter(),                                         # 3
        _make_activity(level=activity_level),                      # 4
        _make_apps(count=apps_count),                              # 5
        _make_dashboard(),                                         # 6
        _make_early_access(count=len(early_roles or []),
                           roles=early_roles),                     # 7
        _make_subscription(),                                      # 8
        _make_reminders(due_count=reminders_due),                  # 9
        _make_stale(total=stale_total),                            # 10
        _make_alerts(),                                            # 11
        _make_completeness(pct=completeness_pct),                  # 12
        _make_saved(),                                             # 13
        _make_impressions(),                                       # 14
        _make_assessments(items=assessment_items or []),           # 15
        match_quality if match_quality is not None
            else _make_match_quality(),                            # 16
    ]


# Patch target — all 17 helpers live inside naukri_daily_brief's local imports,
# so we patch them at their source module locations.
GATHER_PATCHES = [
    ("naukri_server.tools.inbox._fetch_inbox",),
    ("naukri_server.tools.notifications._fetch_notifications",),
    ("naukri_server.tools.search.naukri_get_recommendations",),
    ("naukri_server.tools.performance._get_recruiter_activity",),
    ("naukri_server.tools.performance._get_activity_level",),
    ("naukri_server.tools.tracking._list_applications",),
    ("naukri_server.tools.profile.get_cached_dashboard",),
    ("naukri_server.tools.early_access._list_early_access_roles",),
    ("naukri_server.tools.subscription._get_subscription_status",),
    ("naukri_server.tools.reminders._list_reminders",),
    ("naukri_server.tools.tracking._get_stale_applications",),
    ("naukri_server.tools.alerts._get_alerts_list",),
    ("naukri_server.tools.assessments._get_profile_completeness",),
    ("naukri_server.tools.tracking._list_saved_jobs",),
    ("naukri_server.tools.performance._get_search_impressions",),
    ("naukri_server.tools.assessments._list_assessments",),
    ("naukri_server.tools.insights._match_quality",),
]


def _apply_gather_patches(results_list):
    """
    Return a list of patch context managers that make asyncio.gather
    return results_list in the correct order.

    We replace asyncio.gather itself with an AsyncMock that returns
    the predetermined list, then layer per-function patches on top so
    the import-inside-function pattern works correctly.
    """
    # Build per-function AsyncMocks that return their indexed result
    mocks = []
    for i, (target,) in enumerate(GATHER_PATCHES):
        m = AsyncMock(return_value=results_list[i])
        mocks.append((target, m))
    return mocks


# ===========================================================================
# 1. _build_recommended_actions — sync, no mocking needed
# ===========================================================================

class TestBuildRecommendedActionsHighPriority:
    """High-priority actions: unread messages and due reminders."""

    def test_unread_messages_produces_high_priority_action(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 3},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
        }
        actions = _build_recommended_actions(brief)
        assert any(a["priority"] == "high" and "3" in a["action"] for a in actions)

    def test_due_reminders_produces_high_priority_action(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 2},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
        }
        actions = _build_recommended_actions(brief)
        assert any(a["priority"] == "high" and "2" in a["action"] for a in actions)

    def test_high_priority_actions_sorted_before_others(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 1},
            "due_reminders": {"count": 1},
            "stale_applications": {"count": 5},
            "early_access_roles": {"newly_posted_count": 3},
            "assessments": {"pending": 2},
            "profile_completeness": {"completeness_percent": 50},
        }
        actions = _build_recommended_actions(brief)
        priorities = [a["priority"] for a in actions]
        # All high items must appear before all medium/low items
        seen_non_high = False
        for p in priorities:
            if p != "high":
                seen_non_high = True
            if seen_non_high and p == "high":
                pytest.fail("High priority action appears after non-high priority action")


class TestBuildRecommendedActionsMediumPriority:
    """Medium-priority actions: stale apps, early access, assessments."""

    def test_stale_applications_produces_medium_action(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 4},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
        }
        actions = _build_recommended_actions(brief)
        assert any(a["priority"] == "medium" and "stale" in a["action"].lower() for a in actions)

    def test_early_access_new_roles_produces_medium_action(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 2},
            "assessments": {"pending": 0},
        }
        actions = _build_recommended_actions(brief)
        assert any(a["priority"] == "medium" and "early access" in a["action"].lower() for a in actions)

    def test_pending_assessments_produces_medium_action(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 3},
        }
        actions = _build_recommended_actions(brief)
        assert any(a["priority"] == "medium" and "assessment" in a["action"].lower() for a in actions)


class TestBuildRecommendedActionsLowPriority:
    """Low-priority actions: profile completeness < 80% and no applications."""

    def test_low_completeness_produces_low_action(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
            "profile_completeness": {"completeness_percent": 60},
        }
        actions = _build_recommended_actions(brief)
        assert any(a["priority"] == "low" and "completeness" in a["action"].lower() for a in actions)

    def test_completeness_at_80_does_not_produce_action(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
            "profile_completeness": {"completeness_percent": 80},
        }
        actions = _build_recommended_actions(brief)
        assert not any("completeness" in a["action"].lower() for a in actions)

    def test_no_applications_today_produces_low_action(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
            "todays_applications": {"count": 0},
        }
        actions = _build_recommended_actions(brief)
        assert any(a["priority"] == "low" and "apply" in a["action"].lower() for a in actions)

    def test_todays_applications_none_does_not_produce_action(self):
        """When todays_applications key is absent, no 'apply today' low action added."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
        }
        actions = _build_recommended_actions(brief)
        assert not any("apply" in a["action"].lower() for a in actions)


class TestBuildRecommendedActionsEmpty:
    """Empty brief returns empty action list."""

    def test_all_zeros_returns_empty_list(self):
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 0},
            "due_reminders": {"count": 0},
            "stale_applications": {"count": 0},
            "early_access_roles": {"newly_posted_count": 0},
            "assessments": {"pending": 0},
            "profile_completeness": {"completeness_percent": 85},
        }
        actions = _build_recommended_actions(brief)
        assert actions == []

    def test_combined_all_priorities_present(self):
        """When all trigger conditions met, actions span all three priority levels."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "unread_messages": {"count": 1},
            "due_reminders": {"count": 1},
            "stale_applications": {"count": 1},
            "early_access_roles": {"newly_posted_count": 1},
            "assessments": {"pending": 1},
            "profile_completeness": {"completeness_percent": 50},
            "todays_applications": {"count": 0},
        }
        actions = _build_recommended_actions(brief)
        priority_levels = {a["priority"] for a in actions}
        assert "high" in priority_levels
        assert "medium" in priority_levels
        assert "low" in priority_levels


# ===========================================================================
# 2. _build_early_access_section — sync, mocking _detect_new_roles
# ===========================================================================

class TestBuildEarlyAccessSection:
    """Tests for _build_early_access_section."""

    def test_with_roles_calls_detect_new_roles(self):
        roles = [{"id": "r1", "title": "SDE"}, {"id": "r2", "title": "PM"}]
        with patch(
            "naukri_server.tools.early_access._detect_new_roles",
            return_value=(roles[:1], 2),
        ):
            from naukri_server.tools.daily_brief import _build_early_access_section
            errors = []
            section = _build_early_access_section(
                {"count": 2, "roles": roles}, errors
            )
        assert section["count"] == 2
        assert section["newly_posted_count"] == 1
        assert section["new_roles"] == roles[:1]
        assert errors == []

    def test_new_roles_capped_at_5(self):
        """new_roles in the section is capped at 5 even if _detect_new_roles returns more."""
        many_roles = [{"id": str(i)} for i in range(10)]
        with patch(
            "naukri_server.tools.early_access._detect_new_roles",
            return_value=(many_roles, 10),
        ):
            from naukri_server.tools.daily_brief import _build_early_access_section
            errors = []
            section = _build_early_access_section(
                {"count": 10, "roles": many_roles}, errors
            )
        assert len(section["new_roles"]) == 5

    def test_without_roles_returns_zero_counts(self):
        from naukri_server.tools.daily_brief import _build_early_access_section
        errors = []
        section = _build_early_access_section(None, errors)
        assert section["count"] == 0
        assert section["newly_posted_count"] == 0
        assert section["new_roles"] == []
        assert errors == []

    def test_empty_roles_list_skips_detect(self):
        from naukri_server.tools.daily_brief import _build_early_access_section
        errors = []
        section = _build_early_access_section({"count": 0, "roles": []}, errors)
        assert section["newly_posted_count"] == 0
        assert errors == []

    def test_tracking_error_appended_to_errors(self):
        with patch(
            "naukri_server.tools.early_access._detect_new_roles",
            side_effect=RuntimeError("file missing"),
        ):
            from naukri_server.tools.daily_brief import _build_early_access_section
            errors = []
            section = _build_early_access_section(
                {"count": 1, "roles": [{"id": "r1"}]}, errors
            )
        assert section["newly_posted_count"] == 0
        assert len(errors) == 1
        assert "Early access tracking" in errors[0]
        assert "RuntimeError" in errors[0]


# ===========================================================================
# 3. naukri_daily_brief — main async function
# ===========================================================================

def _patch_all_17(results_list):
    """Return list of (target_path, AsyncMock) tuples for all 17 gather helpers."""
    return [
        (GATHER_PATCHES[i][0], AsyncMock(return_value=results_list[i]))
        for i in range(17)
    ]


async def _run_brief_with_mocked_results(results_list, *, profile_data=None):
    """
    Run naukri_daily_brief() with all 17 gather helpers mocked.
    Optionally also mock get_cached_profile for fit scoring.
    """
    patches_spec = _patch_all_17(results_list)

    # Also patch _detect_new_roles so early_access section doesn't touch disk
    with patch("naukri_server.tools.early_access._detect_new_roles", return_value=([], 0)):
        # Patch get_cached_profile for fit scoring branch
        with patch(
            "naukri_server.tools.profile.get_cached_profile",
            new=AsyncMock(return_value=profile_data),
        ):
            # Apply all 17 patches
            active_patches = []
            for target, mock in patches_spec:
                p = patch(target, new=mock)
                p.start()
                active_patches.append(p)
            try:
                from naukri_server.tools.daily_brief import naukri_daily_brief
                result = await naukri_daily_brief()
            finally:
                for p in active_patches:
                    p.stop()
    return result


class TestNaukriDailyBriefAllSuccess:
    """All 17 tasks succeed — verify key fields are populated correctly."""

    @pytest.mark.asyncio
    async def test_status_is_success_when_all_tasks_succeed(self):
        results = _all_good_results()
        result = await _run_brief_with_mocked_results(results)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_date_field_is_present(self):
        """date field must be a non-empty string (IST date)."""
        results = _all_good_results()
        result = await _run_brief_with_mocked_results(results)
        assert "date" in result
        assert isinstance(result["date"], str)
        assert len(result["date"]) == 10  # YYYY-MM-DD

    @pytest.mark.asyncio
    async def test_unread_messages_count_propagated(self):
        results = _all_good_results(inbox_count=5)
        result = await _run_brief_with_mocked_results(results)
        assert result["unread_messages"]["count"] == 5

    @pytest.mark.asyncio
    async def test_activity_level_propagated(self):
        results = _all_good_results(activity_level="HIGH")
        result = await _run_brief_with_mocked_results(results)
        assert result["activity_level"] == "HIGH"

    @pytest.mark.asyncio
    async def test_match_quality_present_when_returned(self):
        mq = _make_match_quality()
        results = _all_good_results(match_quality=mq)
        result = await _run_brief_with_mocked_results(results)
        assert result["match_quality"] == mq

    @pytest.mark.asyncio
    async def test_match_quality_null_when_task_returns_none(self):
        results = _all_good_results()
        # Replace index 16 with None
        results[16] = None
        result = await _run_brief_with_mocked_results(results)
        assert result["match_quality"] is None

    @pytest.mark.asyncio
    async def test_recommended_actions_list_present(self):
        results = _all_good_results()
        result = await _run_brief_with_mocked_results(results)
        assert "recommended_actions" in result
        assert isinstance(result["recommended_actions"], list)


class TestNaukriDailyBriefActivityLevelFallback:
    """activity_level falls back to 'UNKNOWN' when task fails or field absent."""

    @pytest.mark.asyncio
    async def test_activity_level_unknown_when_task_raises(self):
        results = _all_good_results()
        results[4] = Exception("network error")  # _get_activity_level fails
        result = await _run_brief_with_mocked_results(results)
        assert result["activity_level"] == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_activity_level_unknown_when_level_missing_from_result(self):
        results = _all_good_results()
        # Return a dict without 'level' key
        results[4] = {"status": "success"}
        result = await _run_brief_with_mocked_results(results)
        assert result["activity_level"] == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_activity_level_unknown_when_task_returns_error_dict(self):
        results = _all_good_results()
        results[4] = {"status": "error", "message": "auth failed"}
        result = await _run_brief_with_mocked_results(results)
        assert result["activity_level"] == "UNKNOWN"


class TestNaukriDailyBriefPartialFailures:
    """Some tasks raise exceptions — status becomes partial_success, errors list populated."""

    @pytest.mark.asyncio
    async def test_one_exception_yields_partial_success(self):
        results = _all_good_results()
        results[0] = Exception("inbox unavailable")  # _fetch_inbox fails
        result = await _run_brief_with_mocked_results(results)
        assert result["status"] == "partial_success"
        assert any("Inbox" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_error_dict_from_task_yields_partial_success(self):
        results = _all_good_results()
        results[1] = {"status": "error", "message": "token expired"}  # notifications error
        result = await _run_brief_with_mocked_results(results)
        assert result["status"] == "partial_success"
        assert any("Notifications" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_multiple_failures_all_recorded_in_errors(self):
        results = _all_good_results()
        results[0] = Exception("inbox down")
        results[3] = Exception("recruiter API down")
        results[8] = Exception("subscription API down")
        result = await _run_brief_with_mocked_results(results)
        assert result["status"] == "partial_success"
        error_text = " ".join(result["errors"])
        assert "Inbox" in error_text
        assert "Recruiter activity" in error_text
        assert "Subscription" in error_text

    @pytest.mark.asyncio
    async def test_unread_messages_zero_when_inbox_fails(self):
        results = _all_good_results()
        results[0] = Exception("inbox error")
        result = await _run_brief_with_mocked_results(results)
        assert result["unread_messages"]["count"] == 0
        assert result["unread_messages"]["messages"] == []


class TestNaukriDailyBriefISTDate:
    """The date field uses IST timezone (+05:30)."""

    @pytest.mark.asyncio
    async def test_date_uses_ist_timezone(self):
        """Verify date field is formatted as YYYY-MM-DD from IST timezone."""
        from datetime import datetime, timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        expected_date = datetime.now(IST).strftime("%Y-%m-%d")

        results = _all_good_results()
        result = await _run_brief_with_mocked_results(results)
        assert result["date"] == expected_date


# ===========================================================================
# 4. Fit scoring enrichment
# ===========================================================================

class TestFitScoringEnrichment:
    """Fit scores are computed for top 3 recommendations when profile is available."""

    @pytest.mark.asyncio
    async def test_fit_score_added_when_profile_and_recs_present(self):
        jobs = [
            {"title": "SDE", "tags": ["python", "django"], "experience": "3-5 years"},
            {"title": "Backend Eng", "tags": ["java"], "experience": "2-4 years"},
        ]
        results = _all_good_results(recs_jobs=jobs)
        profile_data = {
            "status": "success",
            "key_skills": ["python", "django", "rest"],
            "total_experience": 4,
        }
        # Patch compute_fit_score to return a predictable value
        with patch(
            "naukri_server.tools.daily_brief.compute_fit_score",
            return_value={"overall_score": 85},
        ):
            result = await _run_brief_with_mocked_results(results, profile_data=profile_data)

        for job in result["recommendations"]["jobs"][:2]:
            assert "fit_score" in job
            assert job["fit_score"] == 85

    @pytest.mark.asyncio
    async def test_fit_score_absent_when_no_profile(self):
        """When get_cached_profile returns None, fit scoring is skipped gracefully."""
        jobs = [{"title": "SDE", "tags": ["python"], "experience": "3 years"}]
        results = _all_good_results(recs_jobs=jobs)
        # profile_data=None means get_cached_profile returns None
        result = await _run_brief_with_mocked_results(results, profile_data=None)
        for job in result["recommendations"]["jobs"]:
            assert "fit_score" not in job

    @pytest.mark.asyncio
    async def test_fit_scoring_error_added_to_errors(self):
        """If get_cached_profile raises, fit scoring error is recorded."""
        jobs = [{"title": "SDE", "tags": ["python"], "experience": "3 years"}]
        results = _all_good_results(recs_jobs=jobs)

        with patch(
            "naukri_server.tools.early_access._detect_new_roles", return_value=([], 0)
        ):
            patches_spec = _patch_all_17(results)
            active_patches = []
            for target, mock in patches_spec:
                p = patch(target, new=mock)
                p.start()
                active_patches.append(p)
            try:
                with patch(
                    "naukri_server.tools.profile.get_cached_profile",
                    new=AsyncMock(side_effect=RuntimeError("profile broken")),
                ):
                    from naukri_server.tools.daily_brief import naukri_daily_brief
                    result = await naukri_daily_brief()
            finally:
                for p in active_patches:
                    p.stop()

        assert result["status"] == "partial_success"
        assert any("Fit scoring" in e for e in result["errors"])

    @pytest.mark.asyncio
    async def test_fit_scoring_only_applies_to_top_3_jobs(self):
        """Only the first 3 jobs get fit_score even if there are more."""
        jobs = [
            {"title": f"Job {i}", "tags": ["python"], "experience": "2 years"}
            for i in range(5)
        ]
        results = _all_good_results(recs_jobs=jobs)
        profile_data = {
            "status": "success",
            "key_skills": ["python"],
            "total_experience": 3,
        }
        with patch(
            "naukri_server.tools.daily_brief.compute_fit_score",
            return_value={"overall_score": 70},
        ):
            result = await _run_brief_with_mocked_results(results, profile_data=profile_data)

        scored = [j for j in result["recommendations"]["jobs"] if "fit_score" in j]
        assert len(scored) == 3


# ===========================================================================
# 5. Pending assessment count
# ===========================================================================

class TestPendingAssessmentCount:
    """pending count excludes assessments with status passed/completed/failed."""

    @pytest.mark.asyncio
    async def test_pending_count_excludes_terminal_statuses(self):
        items = [
            {"title": "A1", "status": "pending"},
            {"title": "A2", "status": "passed"},
            {"title": "A3", "status": "completed"},
            {"title": "A4", "status": "failed"},
            {"title": "A5", "status": "in_progress"},
        ]
        results = _all_good_results(assessment_items=items)
        result = await _run_brief_with_mocked_results(results)
        # Only "pending" and "in_progress" are non-terminal
        assert result["assessments"]["pending"] == 2

    @pytest.mark.asyncio
    async def test_pending_count_case_insensitive(self):
        items = [
            {"title": "A1", "status": "Passed"},
            {"title": "A2", "status": "COMPLETED"},
            {"title": "A3", "status": "FAILED"},
            {"title": "A4", "status": "Pending"},
        ]
        results = _all_good_results(assessment_items=items)
        result = await _run_brief_with_mocked_results(results)
        assert result["assessments"]["pending"] == 1

    @pytest.mark.asyncio
    async def test_pending_count_zero_when_no_assessments(self):
        results = _all_good_results(assessment_items=[])
        result = await _run_brief_with_mocked_results(results)
        assert result["assessments"]["pending"] == 0
        assert result["assessments"]["total"] == 0

    @pytest.mark.asyncio
    async def test_pending_count_zero_when_assessments_task_fails(self):
        results = _all_good_results()
        results[15] = Exception("assessments API down")
        result = await _run_brief_with_mocked_results(results)
        assert result["assessments"]["pending"] == 0
