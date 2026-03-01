"""Tests for consolidated MCP tools — action routing and parameter validation.

Every test is PURE: no network, no browser, no file I/O.
We only exercise validation / routing logic that runs BEFORE any async helper call.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. naukri_mock_interview
# =====================================================================

class TestMockInterview:
    """Tests for naukri_server.tools.mock_interview.naukri_mock_interview."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.mock_interview import naukri_mock_interview
        result = await naukri_mock_interview(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_start_requires_job_id(self):
        from naukri_server.tools.mock_interview import naukri_mock_interview
        result = await naukri_mock_interview(action="start")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_start_with_empty_job_id(self):
        from naukri_server.tools.mock_interview import naukri_mock_interview
        result = await naukri_mock_interview(action="start", job_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_answer_requires_all_params(self):
        from naukri_server.tools.mock_interview import naukri_mock_interview
        result = await naukri_mock_interview(action="answer")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "test_id" in result["message"]

    @pytest.mark.asyncio
    async def test_answer_partial_params(self):
        from naukri_server.tools.mock_interview import naukri_mock_interview
        result = await naukri_mock_interview(
            action="answer", test_id="t1", topic_id="tp1",
        )
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_topics_routes_to_helper(self):
        from naukri_server.tools.mock_interview import naukri_mock_interview
        with patch("naukri_server.tools.mock_interview._get_topics", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "topics": []}
            result = await naukri_mock_interview(action="topics")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_history_routes_to_helper(self):
        from naukri_server.tools.mock_interview import naukri_mock_interview
        with patch("naukri_server.tools.mock_interview._get_history", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "interviews": []}
            result = await naukri_mock_interview(action="history")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_start_routes_to_helper(self):
        from naukri_server.tools.mock_interview import naukri_mock_interview
        with patch("naukri_server.tools.mock_interview._start_interview", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "test_id": "123"}
            result = await naukri_mock_interview(action="start", job_id="999")
            mock_helper.assert_awaited_once_with("999")
            assert result["status"] == "success"


# =====================================================================
# 2. naukri_inbox
# =====================================================================

class TestInbox:
    """Tests for naukri_server.tools.inbox.naukri_inbox."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.inbox import naukri_inbox
        result = await naukri_inbox(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_read_requires_all_ids(self):
        from naukri_server.tools.inbox import naukri_inbox
        result = await naukri_inbox(action="read", message_id="m1")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "vcard_id" in result["message"]

    @pytest.mark.asyncio
    async def test_read_missing_message_id(self):
        from naukri_server.tools.inbox import naukri_inbox
        result = await naukri_inbox(action="read", vcard_id="v1", unique_id="u1")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_mark_interested_requires_ids(self):
        from naukri_server.tools.inbox import naukri_inbox
        result = await naukri_inbox(action="mark_interested")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "mail_id" in result["message"]

    @pytest.mark.asyncio
    async def test_accept_nvite_requires_job_id(self):
        from naukri_server.tools.inbox import naukri_inbox
        result = await naukri_inbox(action="accept_nvite")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "nvite_job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_list_routes_to_helper(self):
        from naukri_server.tools.inbox import naukri_inbox
        with patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "messages": []}
            result = await naukri_inbox(action="list", limit=10, page=2)
            mock_helper.assert_awaited_once_with(limit=10, unread_only=False, mail_type="", page=2)
            assert result["status"] == "success"


# =====================================================================
# 3. naukri_profile
# =====================================================================

class TestProfile:
    """Tests for naukri_server.tools.profile.naukri_profile."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_update_no_fields(self):
        """Update with no fields should fail validation inside _update_profile."""
        from naukri_server.tools.profile import naukri_profile
        with patch("naukri_server.tools.profile._update_profile", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {
                "status": "error",
                "message": "No fields provided. Pass at least one field to update.",
                "error_code": "VALIDATION_ERROR",
            }
            result = await naukri_profile(action="update")
            assert result["status"] == "error"
            assert "No fields" in result["message"]

    @pytest.mark.asyncio
    async def test_get_routes_to_helper(self):
        from naukri_server.tools.profile import naukri_profile
        with patch("naukri_server.tools.profile._get_profile", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "name": "Test"}
            result = await naukri_profile(action="get")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_audit_routes_to_helper(self):
        from naukri_server.tools.profile import naukri_profile
        with patch("naukri_server.tools.profile._audit_profile", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "grade": "A"}
            result = await naukri_profile(action="audit")
            mock_helper.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_boost_routes_to_helper(self):
        from naukri_server.tools.profile import naukri_profile
        with patch("naukri_server.tools.profile._boost_visibility", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "refreshed"}
            result = await naukri_profile(action="boost")
            mock_helper.assert_awaited_once_with(randomize=False)


# =====================================================================
# 4. naukri_performance
# =====================================================================

class TestPerformance:
    """Tests for naukri_server.tools.performance.naukri_performance."""

    @pytest.mark.asyncio
    async def test_invalid_metric(self):
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="invalid")
        assert result["status"] == "error"
        assert "Unknown metric" in result["message"]

    @pytest.mark.asyncio
    async def test_impressions_invalid_days(self):
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="impressions", days=15)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "7, 30, 90" in result["message"]

    @pytest.mark.asyncio
    async def test_impressions_valid_days_routes(self):
        from naukri_server.tools.performance import naukri_performance
        with patch("naukri_server.tools.performance._get_search_impressions", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "days": 30}
            result = await naukri_performance(metric="impressions", days=30)
            mock_helper.assert_awaited_once_with(days=30)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_recruiter_activity_routes(self):
        from naukri_server.tools.performance import naukri_performance
        with patch("naukri_server.tools.performance._get_recruiter_activity", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "activities": []}
            result = await naukri_performance(metric="recruiter_activity", page=2, size=10, filter_by="VIEWED")
            mock_helper.assert_awaited_once_with(page=2, size=10, filter_by="VIEWED")

    @pytest.mark.asyncio
    async def test_activity_level_routes(self):
        from naukri_server.tools.performance import naukri_performance
        with patch("naukri_server.tools.performance._get_activity_level", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "level": "HIGH"}
            result = await naukri_performance(metric="activity_level")
            mock_helper.assert_awaited_once()


# =====================================================================
# 5. naukri_company_intel
# =====================================================================

class TestCompanyIntel:
    """Tests for naukri_server.tools.ambitionbox.naukri_company_intel."""

    @pytest.mark.asyncio
    async def test_invalid_intel_type(self):
        from naukri_server.tools.ambitionbox import naukri_company_intel
        result = await naukri_company_intel(company="google", intel_type="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown intel_type" in result["message"]

    @pytest.mark.asyncio
    async def test_salary_routes_to_helper(self):
        from naukri_server.tools.ambitionbox import naukri_company_intel
        with patch("naukri_server.tools.ambitionbox._fetch_salary", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "salaries": []}
            result = await naukri_company_intel(company="Google", intel_type="salary")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_reviews_routes_to_helper(self):
        from naukri_server.tools.ambitionbox import naukri_company_intel
        with patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "reviews": []}
            result = await naukri_company_intel(company="google", intel_type="reviews", page=3)
            mock_helper.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interviews_routes_to_helper(self):
        from naukri_server.tools.ambitionbox import naukri_company_intel
        with patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "interview_experiences": []}
            result = await naukri_company_intel(company="google", intel_type="interviews")
            mock_helper.assert_awaited_once()


# =====================================================================
# 6. naukri_saved_jobs
# =====================================================================

class TestSavedJobs:
    """Tests for naukri_server.tools.tracking.naukri_saved_jobs."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.tracking import naukri_saved_jobs
        result = await naukri_saved_jobs(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_save_requires_job_id(self):
        from naukri_server.tools.tracking import naukri_saved_jobs
        result = await naukri_saved_jobs(action="save")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_unsave_requires_job_id(self):
        from naukri_server.tools.tracking import naukri_saved_jobs
        result = await naukri_saved_jobs(action="unsave")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]


# =====================================================================
# 7. naukri_early_access
# =====================================================================

class TestEarlyAccess:
    """Tests for naukri_server.tools.early_access.naukri_early_access."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.early_access import naukri_early_access
        result = await naukri_early_access(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_share_requires_job_id(self):
        from naukri_server.tools.early_access import naukri_early_access
        result = await naukri_early_access(action="share")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_list_routes_to_helper(self):
        from naukri_server.tools.early_access import naukri_early_access
        with patch("naukri_server.tools.early_access._list_early_access_roles", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "roles": []}
            result = await naukri_early_access(action="list", page=2, limit=10)
            mock_helper.assert_awaited_once_with(page=2, limit=10)
            assert result["status"] == "success"


# =====================================================================
# 8. naukri_profile_media
# =====================================================================

class TestProfileMedia:
    """Tests for naukri_server.tools.resume_photo.naukri_profile_media."""

    @pytest.mark.asyncio
    async def test_invalid_media_type(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="video")
        assert result["status"] == "error"
        assert "Unknown media_type" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_invalid_action(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="delete")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_photo_invalid_action(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="photo", action="download")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_download_requires_save_path(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="download")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "save_path" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_upload_requires_file_path(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="resume", action="upload")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "file_path" in result["message"]

    @pytest.mark.asyncio
    async def test_photo_upload_requires_file_path(self):
        from naukri_server.tools.resume_photo import naukri_profile_media
        result = await naukri_profile_media(media_type="photo", action="upload")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "file_path" in result["message"]


# =====================================================================
# 9. naukri_insights
# =====================================================================

class TestInsights:
    """Tests for naukri_server.tools.insights.naukri_insights."""

    @pytest.mark.asyncio
    async def test_invalid_insight_type(self):
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="invalid")
        assert result["status"] == "error"
        assert "Unknown insight_type" in result["message"]

    @pytest.mark.asyncio
    async def test_cached_answers_update_requires_key(self):
        """cached_answers update without key should fail validation inside _cached_answers."""
        from naukri_server.tools.insights import _cached_answers
        result = await _cached_answers(action="update")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "key" in result["message"]

    @pytest.mark.asyncio
    async def test_cached_answers_delete_requires_key(self):
        from naukri_server.tools.insights import _cached_answers
        result = await _cached_answers(action="delete")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "key" in result["message"]

    @pytest.mark.asyncio
    async def test_cached_answers_invalid_action(self):
        from naukri_server.tools.insights import _cached_answers
        result = await _cached_answers(action="purge")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_applications_routes_to_helper(self):
        from naukri_server.tools.insights import naukri_insights
        with patch("naukri_server.tools.insights._application_insights", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "total_applications": 10}
            result = await naukri_insights(insight_type="applications", days=7)
            mock_helper.assert_awaited_once_with(days=7)
            assert result["status"] == "success"


# =====================================================================
# 10. naukri_company_follow
# =====================================================================

class TestCompanyFollow:
    """Tests for naukri_server.tools.companies.naukri_company_follow."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.companies import naukri_company_follow
        result = await naukri_company_follow(action="invalid", group_ids=["123"])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_group_ids(self):
        from naukri_server.tools.companies import naukri_company_follow
        result = await naukri_company_follow(action="status", group_ids=[])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_ids" in result["message"]

    @pytest.mark.asyncio
    async def test_status_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_company_follow
        with patch("naukri_server.tools.companies._get_follow_status", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "followed": ["123"]}
            result = await naukri_company_follow(action="status", group_ids=["123"])
            mock_helper.assert_awaited_once_with(["123"])
            assert result["status"] == "success"


# =====================================================================
# 11. naukri_reminders
# =====================================================================

class TestReminders:
    """Tests for naukri_server.tools.reminders.naukri_reminders."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.reminders import naukri_reminders
        result = await naukri_reminders(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_set_requires_job_id(self):
        from naukri_server.tools.reminders import naukri_reminders
        result = await naukri_reminders(action="set")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_list_routes_to_helper(self):
        from naukri_server.tools.reminders import naukri_reminders
        with patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "total": 0, "reminders": []}
            result = await naukri_reminders(action="list")
            mock_helper.assert_awaited_once_with(include_past=True)
            assert result["status"] == "success"


# =====================================================================
# 12. naukri_sync
# =====================================================================

class TestSync:
    """Tests for naukri_server.tools.sync.naukri_sync."""

    @pytest.mark.asyncio
    async def test_invalid_entity(self):
        from naukri_server.tools.sync import naukri_sync
        result = await naukri_sync(entity="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown entity" in result["message"]

    @pytest.mark.asyncio
    async def test_applications_routes_to_helper(self):
        from naukri_server.tools.sync import naukri_sync
        with patch("naukri_server.tools.sync._sync_applications", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "method": "api"}
            result = await naukri_sync(entity="applications", days_back=30)
            mock_helper.assert_awaited_once_with(force_browser=False, days_back=30)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_saved_jobs_routes_to_helper(self):
        from naukri_server.tools.sync import naukri_sync
        with patch("naukri_server.tools.sync._sync_saved_jobs", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "method": "api"}
            result = await naukri_sync(entity="saved_jobs")
            mock_helper.assert_awaited_once_with(force_browser=False)


# =====================================================================
# 13. naukri_resume_builder
# =====================================================================

class TestResumeBuilder:
    """Tests for naukri_server.tools.resume_builder.naukri_resume_builder."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.resume_builder import naukri_resume_builder
        result = await naukri_resume_builder(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_templates_routes_to_helper(self):
        from naukri_server.tools.resume_builder import naukri_resume_builder
        with patch("naukri_server.tools.resume_builder._get_templates", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "templates": []}
            result = await naukri_resume_builder(action="templates")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_status_routes_to_helper(self):
        from naukri_server.tools.resume_builder import naukri_resume_builder
        with patch("naukri_server.tools.resume_builder._get_status", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "attempts_left": 3}
            result = await naukri_resume_builder(action="status")
            mock_helper.assert_awaited_once()


# =====================================================================
# 14. naukri_assessments
# =====================================================================

class TestAssessments:
    """Tests for naukri_server.tools.assessments.naukri_assessments."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.assessments import naukri_assessments
        result = await naukri_assessments(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_list_routes_to_helper(self):
        from naukri_server.tools.assessments import naukri_assessments
        with patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "assessments": []}
            result = await naukri_assessments(action="list")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_completeness_routes_to_helper(self):
        from naukri_server.tools.assessments import naukri_assessments
        with patch("naukri_server.tools.assessments._get_profile_completeness", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "completeness_percent": 85}
            result = await naukri_assessments(action="completeness")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"


# =====================================================================
# 15. Cross-cutting: helper-level validation (no action routing)
# =====================================================================

class TestHelperValidation:
    """Validation inside internal helpers that runs before any API/browser call."""

    @pytest.mark.asyncio
    async def test_application_insights_negative_days(self):
        """_application_insights rejects days < 1."""
        from naukri_server.tools.insights import _application_insights
        result = await _application_insights(days=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "days" in result["message"]

    @pytest.mark.asyncio
    async def test_set_reminder_invalid_days_zero(self):
        """_set_reminder rejects days < 1."""
        from naukri_server.tools.reminders import _set_reminder
        result = await _set_reminder(job_id="123", days=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "days" in result["message"]

    @pytest.mark.asyncio
    async def test_set_reminder_invalid_days_over_365(self):
        """_set_reminder rejects days > 365."""
        from naukri_server.tools.reminders import _set_reminder
        result = await _set_reminder(job_id="123", days=400)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_recruiter_activity_invalid_filter(self):
        """_get_recruiter_activity rejects invalid filter_by values."""
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity(filter_by="INVALID_FILTER")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "INVALID_FILTER" in result["message"]

    @pytest.mark.asyncio
    async def test_recruiter_activity_page_validation(self):
        """_get_recruiter_activity rejects page < 1."""
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity(page=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]

    @pytest.mark.asyncio
    async def test_fetch_inbox_page_validation(self):
        """_fetch_inbox rejects page < 1."""
        from naukri_server.tools.inbox import _fetch_inbox
        result = await _fetch_inbox(page=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]
