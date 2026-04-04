"""Tests for tracking helper functions — record_application, _application_follow_up,
_get_match_analytics, _push_save_to_naukri, _save_job, _unsave_job.

Every test is PURE: no network, no browser, no file I/O.
We mock _load_json, _save_json, api_get, api_post, and asyncio.Lock as needed.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# =====================================================================
# 1. record_application — new, update, field preservation
# =====================================================================

class TestRecordApplication:
    """Tests for naukri_server.tools.tracking.record_application."""

    @pytest.mark.asyncio
    async def test_new_application_added(self):
        """A brand-new job_id should insert a new application via upsert."""
        from naukri_server.tools.tracking import record_application

        captured = {}

        async def fake_upsert(app):
            captured["app"] = app

        with patch("naukri_server.database.get_application", new_callable=AsyncMock,
                    return_value=None), \
             patch("naukri_server.database.upsert_application", new_callable=AsyncMock,
                    side_effect=fake_upsert):
            await record_application(job_id="J100", title="SDE", company="Acme", status="applied")

        app = captured["app"]
        assert app["job_id"] == "J100"
        assert app["title"] == "SDE"
        assert app["company"] == "Acme"
        assert app["status"] == "applied"
        assert "applied_at" in app

    @pytest.mark.asyncio
    async def test_existing_application_updated(self):
        """An existing job_id should update status and updated_at, not duplicate."""
        from naukri_server.tools.tracking import record_application

        existing = {"job_id": "J200", "title": "QA", "company": "Beta", "status": "applied",
                    "applied_at": "2026-01-01T00:00:00+00:00"}
        captured = {}

        async def fake_upsert(app):
            captured["app"] = app

        with patch("naukri_server.database.get_application", new_callable=AsyncMock,
                    return_value=existing.copy()), \
             patch("naukri_server.database.upsert_application", new_callable=AsyncMock,
                    side_effect=fake_upsert):
            await record_application(job_id="J200", status="interviewed")

        app = captured["app"]
        assert app["status"] == "interviewed"
        assert "updated_at" in app
        # Original fields preserved
        assert app["title"] == "QA"
        assert app["company"] == "Beta"
        assert app["applied_at"] == "2026-01-01T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_update_preserves_fields_not_passed(self):
        """When updating, passing title=None should NOT overwrite existing title."""
        from naukri_server.tools.tracking import record_application

        existing = {"job_id": "J300", "title": "DevOps", "company": "Gamma", "status": "applied",
                    "applied_at": "2026-02-01T00:00:00+00:00"}
        captured = {}

        async def fake_upsert(app):
            captured["app"] = app

        with patch("naukri_server.database.get_application", new_callable=AsyncMock,
                    return_value=existing.copy()), \
             patch("naukri_server.database.upsert_application", new_callable=AsyncMock,
                    side_effect=fake_upsert):
            # Pass title=None and company=None — should not overwrite
            await record_application(job_id="J300", status="rejected")

        app = captured["app"]
        assert app["title"] == "DevOps"
        assert app["company"] == "Gamma"
        assert app["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_extra_fields_merged_on_new(self):
        """Extra dict should be spread into a new application entry."""
        from naukri_server.tools.tracking import record_application

        captured = {}

        async def fake_upsert(app):
            captured["app"] = app

        with patch("naukri_server.database.get_application", new_callable=AsyncMock,
                    return_value=None), \
             patch("naukri_server.database.upsert_application", new_callable=AsyncMock,
                    side_effect=fake_upsert):
            await record_application(job_id="J400", extra={"source": "batch", "ars_score": 85})

        app = captured["app"]
        assert app["source"] == "batch"
        assert app["ars_score"] == 85


# =====================================================================
# 2. _application_follow_up — action items, recruiter detection, edge cases
# =====================================================================

class TestApplicationFollowUp:
    """Tests for naukri_server.tools.tracking._application_follow_up."""

    @pytest.mark.asyncio
    async def test_generates_action_items(self):
        """Should produce action_items list matching stale applications."""
        from naukri_server.tools.tracking import _application_follow_up

        stale_result = {
            "status": "success",
            "stale_applications": [
                {"job_id": "S1", "title": "Engineer", "company": "Alpha",
                 "applied_date": "2026-01-01", "stale_score": 75, "reasons": ["Old"]},
            ],
        }
        inbox_result = {"status": "success", "messages": []}
        reminders_result = {"status": "success", "reminders": []}

        with patch("naukri_server.services.application_service.get_stale_applications",
                    new_callable=AsyncMock, return_value=stale_result), \
             patch("naukri_server.tools.inbox._fetch_inbox",
                    new_callable=AsyncMock, return_value=inbox_result), \
             patch("naukri_server.tools.reminders._list_reminders",
                    new_callable=AsyncMock, return_value=reminders_result):
            result = await _application_follow_up(days_threshold=14, min_stale_score=40, limit=10)

        assert result["status"] == "success"
        assert result["summary"]["total_stale"] == 1
        assert len(result["action_items"]) == 1
        assert result["action_items"][0]["job_id"] == "S1"

    @pytest.mark.asyncio
    async def test_detects_recruiter_contact(self):
        """Recruiter messages from the same company should set high priority."""
        from naukri_server.tools.tracking import _application_follow_up

        stale_result = {
            "status": "success",
            "stale_applications": [
                {"job_id": "S2", "title": "Dev", "company": "BetaCorp",
                 "applied_date": "2026-01-15", "stale_score": 50, "reasons": ["Old"]},
            ],
        }
        inbox_result = {
            "status": "success",
            "messages": [
                {
                    "company_details": {"company_name": "BetaCorp"},
                    "subject": "Interview Invite",
                    "date": "2026-02-01",
                    "sender": "hr@betacorp.com",
                },
            ],
        }
        reminders_result = {"status": "success", "reminders": []}

        with patch("naukri_server.services.application_service.get_stale_applications",
                    new_callable=AsyncMock, return_value=stale_result), \
             patch("naukri_server.tools.inbox._fetch_inbox",
                    new_callable=AsyncMock, return_value=inbox_result), \
             patch("naukri_server.tools.reminders._list_reminders",
                    new_callable=AsyncMock, return_value=reminders_result):
            result = await _application_follow_up()

        assert result["status"] == "success"
        assert result["summary"]["with_recruiter_contact"] == 1
        assert result["action_items"][0]["priority"] == "high"
        assert "Recruiter contacted" in result["action_items"][0]["action"]

    @pytest.mark.asyncio
    async def test_empty_stale_apps(self):
        """No stale applications → empty action_items, zero summary."""
        from naukri_server.tools.tracking import _application_follow_up

        stale_result = {"status": "success", "stale_applications": []}
        inbox_result = {"status": "success", "messages": []}
        reminders_result = {"status": "success", "reminders": []}

        with patch("naukri_server.services.application_service.get_stale_applications",
                    new_callable=AsyncMock, return_value=stale_result), \
             patch("naukri_server.tools.inbox._fetch_inbox",
                    new_callable=AsyncMock, return_value=inbox_result), \
             patch("naukri_server.tools.reminders._list_reminders",
                    new_callable=AsyncMock, return_value=reminders_result):
            result = await _application_follow_up()

        assert result["status"] == "success"
        assert result["summary"]["total_stale"] == 0
        assert result["action_items"] == []

    @pytest.mark.asyncio
    async def test_inbox_failure_partial_success(self):
        """If inbox fetch raises, result should be partial_success with errors."""
        from naukri_server.tools.tracking import _application_follow_up

        stale_result = {
            "status": "success",
            "stale_applications": [
                {"job_id": "S3", "title": "PM", "company": "Delta",
                 "applied_date": "2026-01-01", "stale_score": 60, "reasons": ["Stale"]},
            ],
        }
        # Inbox fails (returns Exception via gather's return_exceptions=True)
        inbox_exc = RuntimeError("Inbox API down")
        reminders_result = {"status": "success", "reminders": []}

        with patch("naukri_server.services.application_service.get_stale_applications",
                    new_callable=AsyncMock, return_value=stale_result), \
             patch("naukri_server.tools.inbox._fetch_inbox",
                    new_callable=AsyncMock, side_effect=inbox_exc), \
             patch("naukri_server.tools.reminders._list_reminders",
                    new_callable=AsyncMock, return_value=reminders_result):
            result = await _application_follow_up()

        assert result["status"] == "partial_success"
        assert any("Inbox fetch failed" in e for e in result["errors"])


# =====================================================================
# 3. _get_match_analytics — computation, empty, API error
# =====================================================================

class TestGetMatchAnalytics:
    """Tests for naukri_server.tools.tracking._get_match_analytics."""

    @pytest.mark.asyncio
    async def test_computation_from_api_data(self):
        """Should map API response fields to our output format."""
        from naukri_server.tools.tracking import _get_match_analytics

        api_data = {
            "totalApplies": 42,
            "completeMatch": 10,
            "highMatch": 15,
            "mediumMatch": 12,
            "lowMatch": 5,
            "relevantFieldMatch": {"skills": 80, "experience": 90},
            "userDetails": {"name": "Sundeep"},
        }

        with patch("naukri_server.tools.analytics.api_client.get",
                    new_callable=AsyncMock, return_value=api_data):
            result = await _get_match_analytics(days=7)

        assert result["status"] == "success"
        assert result["days"] == 7
        assert result["total_applies"] == 42
        assert result["complete_match"] == 10
        assert result["high_match"] == 15
        assert result["medium_match"] == 12
        assert result["low_match"] == 5
        assert result["field_breakdown"] == {"skills": 80, "experience": 90}
        assert result["user_details"]["name"] == "Sundeep"

    @pytest.mark.asyncio
    async def test_empty_api_response(self):
        """If API returns empty-ish data, fields should be None, not crash."""
        from naukri_server.tools.tracking import _get_match_analytics

        with patch("naukri_server.tools.analytics.api_client.get",
                    new_callable=AsyncMock, return_value={}):
            result = await _get_match_analytics(days=30)

        assert result["status"] == "success"
        assert result["total_applies"] is None
        assert result["complete_match"] is None
        assert result["field_breakdown"] is None

    @pytest.mark.asyncio
    async def test_api_error_propagates(self):
        """NaukriAPIError from api_get should propagate (not silently swallowed)."""
        from naukri_server.tools.tracking import _get_match_analytics
        from naukri_server.api import NaukriAPIError

        with patch("naukri_server.tools.analytics.api_client.get",
                    new_callable=AsyncMock,
                    side_effect=NaukriAPIError(401, "Unauthorized")):
            with pytest.raises(NaukriAPIError):
                await _get_match_analytics(days=7)

    @pytest.mark.asyncio
    async def test_invalid_days_returns_error(self):
        """days < 1 should return VALIDATION_ERROR without calling API."""
        from naukri_server.tools.tracking import _get_match_analytics

        with patch("naukri_server.tools.analytics.api_client.get",
                    new_callable=AsyncMock) as mock_api:
            result = await _get_match_analytics(days=0)

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        mock_api.assert_not_awaited()


# =====================================================================
# 4. _push_save_to_naukri — API call success and failure
# =====================================================================

class TestPushSaveToNaukri:
    """Tests for naukri_server.tools.tracking._push_save_to_naukri."""

    @pytest.mark.asyncio
    async def test_successful_api_call(self):
        """Successful api_post should return True."""
        from naukri_server.tools.tracking import _push_save_to_naukri

        with patch("naukri_server.tools.saved_jobs.SAVE_JOB_API", "/save/"), \
             patch("naukri_server.tools.saved_jobs.api_client.post",
                    new_callable=AsyncMock, return_value={}) as mock_post:
            result = await _push_save_to_naukri("J500")

        assert result is True
        mock_post.assert_awaited_once_with("/save/J500", {})

    @pytest.mark.asyncio
    async def test_api_error_returns_false(self):
        """api_post exception should be caught and return False."""
        from naukri_server.tools.tracking import _push_save_to_naukri

        with patch("naukri_server.tools.saved_jobs.SAVE_JOB_API", "/save/"), \
             patch("naukri_server.tools.saved_jobs.api_client.post",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("Network error")):
            result = await _push_save_to_naukri("J501")

        assert result is False

    @pytest.mark.asyncio
    async def test_no_api_configured_returns_false(self):
        """If SAVE_JOB_API is empty/None, should skip remote call and return False."""
        from naukri_server.tools.tracking import _push_save_to_naukri

        with patch("naukri_server.tools.saved_jobs.SAVE_JOB_API", ""), \
             patch("naukri_server.tools.saved_jobs.api_client.post",
                    new_callable=AsyncMock) as mock_post:
            result = await _push_save_to_naukri("J502")

        assert result is False
        mock_post.assert_not_awaited()


# =====================================================================
# 5. _save_job / _unsave_job — local tracking + remote sync
# =====================================================================

class TestSaveUnsaveJob:
    """Tests for _save_job and _unsave_job."""

    @pytest.mark.asyncio
    async def test_save_job_new(self):
        """Saving a new job should insert via upsert and return total_saved."""
        from naukri_server.tools.tracking import _save_job

        captured = {}

        async def fake_upsert(data):
            captured["data"] = data

        with patch("naukri_server.database.get_saved_job", new_callable=AsyncMock, return_value=None), \
             patch("naukri_server.database.upsert_saved_job", new_callable=AsyncMock, side_effect=fake_upsert), \
             patch("naukri_server.database.count_saved_jobs", new_callable=AsyncMock, return_value=1), \
             patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value=None):
            result = await _save_job("J600", title="ML Eng", company="DeepCo")

        assert result["status"] == "success"
        assert result["action"] == "saved"
        assert result["job_id"] == "J600"
        assert result["total_saved"] == 1
        assert result["synced_remote"] is False
        assert captured["data"]["title"] == "ML Eng"

    @pytest.mark.asyncio
    async def test_save_job_duplicate(self):
        """Saving a job_id that already exists should return already_saved."""
        from naukri_server.tools.tracking import _save_job

        existing = {"job_id": "J700", "title": "X", "saved_at": "2026-01-01"}

        with patch("naukri_server.database.get_saved_job", new_callable=AsyncMock, return_value=existing), \
             patch("naukri_server.database.upsert_saved_job", new_callable=AsyncMock) as mock_upsert:
            result = await _save_job("J700", title="Y")

        assert result["action"] == "already_saved"
        mock_upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unsave_job_removes_entry(self):
        """Unsaving an existing job should delete it from DB and call remote unsave."""
        from naukri_server.tools.tracking import _unsave_job

        with patch("naukri_server.database.delete_saved_job", new_callable=AsyncMock, return_value=True), \
             patch("naukri_server.tools.saved_jobs.api_client.post",
                    new_callable=AsyncMock, return_value={}):
            result = await _unsave_job("J800")

        assert result["status"] == "success"
        assert result["action"] == "unsaved"

    @pytest.mark.asyncio
    async def test_unsave_job_not_found(self):
        """Unsaving a job_id not in local DB should return NOT_FOUND error."""
        from naukri_server.tools.tracking import _unsave_job

        with patch("naukri_server.database.delete_saved_job", new_callable=AsyncMock, return_value=False), \
             patch("naukri_server.tools.saved_jobs.api_client.post",
                    new_callable=AsyncMock, return_value={}):
            result = await _unsave_job("J999")

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"


# =====================================================================
# 6. _compute_follow_up_priority — scoring logic
# =====================================================================

class TestComputeFollowUpPriority:
    """Tests for naukri_server.tools.tracking._compute_follow_up_priority."""

    def test_high_activity_app_gets_high_priority(self):
        """App with recruiter activity, high ARS, and good company rating gets high priority."""
        from naukri_server.tools.tracking import _compute_follow_up_priority
        from datetime import datetime, timezone

        app = {
            "job_activity": 5,          # recruiter viewed → +20
            "ars_score": 75,            # high match → +15
            "company_rating": {"AggregateRating": 4.2},  # good company → +10
            "applied_at": datetime.now(timezone.utc).isoformat(),  # recent → no decay
        }
        priority = _compute_follow_up_priority(app)
        # baseline 50 + 20 + 15 + 10 = 95
        assert priority == 95

    def test_old_app_no_activity_gets_low_priority(self):
        """App with no recruiter activity, no ARS, applied 90 days ago gets low priority."""
        from naukri_server.tools.tracking import _compute_follow_up_priority
        from datetime import datetime, timezone, timedelta

        applied_at = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        app = {
            "job_activity": 0,          # no activity → +0 (only >0 matters)
            "applied_at": applied_at,   # 90 days → -20
        }
        priority = _compute_follow_up_priority(app)
        # baseline 50 + 0 - 20 = 30
        assert priority == 30


# =====================================================================
# interview_prep — full package and partial failure
# =====================================================================

class TestInterviewPrep:
    """Tests for naukri_server.tools.tracking._interview_prep."""

    @pytest.mark.asyncio
    async def test_full_prep_package(self):
        """All three external calls succeed — prep should contain every field."""
        from naukri_server.tools.tracking import _interview_prep

        app = {"job_id": "IP1", "title": "Backend Dev", "company": "TestCorp",
               "applied_at": "2026-03-01T10:00:00+00:00", "ars_score": 72}

        company_intel = {
            "status": "success",
            "overall_rating": 4.2,
            "difficulty_breakdown": {"easy": 30, "medium": 50, "hard": 20},
            "interview_experiences": [
                {"question": "Tell me about yourself"},
                {"question": "System design question"},
                {"question": "Behavioral question"},
                {"question": "Extra question should be trimmed"},
            ],
        }
        mock_topics = {
            "status": "success",
            "topics": ["REST APIs", "Node.js", "SQL", "Docker", "System Design", "Extra"],
        }
        fit_data = {
            "status": "success",
            "fit_assessment": {
                "skill_match": {
                    "matched": ["Node.js", "Express", "MongoDB"],
                    "missing": ["Kubernetes", "AWS"],
                },
            },
        }

        with patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value=app), \
             patch("naukri_server.services.application_service._safe_fetch_company_intel",
                   new_callable=AsyncMock, return_value=company_intel), \
             patch("naukri_server.services.application_service._safe_fetch_mock_topics",
                   new_callable=AsyncMock, return_value=mock_topics), \
             patch("naukri_server.services.application_service._safe_fetch_fit_score",
                   new_callable=AsyncMock, return_value=fit_data):
            result = await _interview_prep("IP1")

        assert result["status"] == "success"
        assert result["job_id"] == "IP1"
        assert result["company"] == "TestCorp"
        assert result["title"] == "Backend Dev"
        assert result["applied_at"] == "2026-03-01T10:00:00+00:00"
        assert result["ars_score"] == 72
        # Company intel
        assert result["company_rating"] == 4.2
        assert result["interview_difficulty"] == {"easy": 30, "medium": 50, "hard": 20}
        assert len(result["sample_questions"]) == 3  # capped at 3
        # Mock topics
        assert len(result["mock_topics"]) == 5  # capped at 5
        assert "REST APIs" in result["mock_topics"]
        # Fit data
        assert result["matched_skills"] == ["Node.js", "Express", "MongoDB"]
        assert result["missing_skills"] == ["Kubernetes", "AWS"]

    @pytest.mark.asyncio
    async def test_partial_failure_company_intel_fails(self):
        """Company intel fails, mock topics and fit succeed — prep should still work."""
        from naukri_server.tools.tracking import _interview_prep

        app = {"job_id": "IP2", "title": "Frontend Dev", "company": "FailCo",
               "applied_at": "2026-03-10T12:00:00+00:00", "ars_score": None}

        mock_topics = {
            "status": "success",
            "topics": ["React", "CSS", "JavaScript"],
        }
        fit_data = {
            "status": "success",
            "fit_assessment": {
                "skill_match": {
                    "matched": ["React", "TypeScript"],
                    "missing": ["Vue"],
                },
            },
        }

        with patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value=app), \
             patch("naukri_server.services.application_service._safe_fetch_company_intel",
                   new_callable=AsyncMock, return_value=None), \
             patch("naukri_server.services.application_service._safe_fetch_mock_topics",
                   new_callable=AsyncMock, return_value=mock_topics), \
             patch("naukri_server.services.application_service._safe_fetch_fit_score",
                   new_callable=AsyncMock, return_value=fit_data):
            result = await _interview_prep("IP2")

        assert result["status"] == "success"
        assert result["job_id"] == "IP2"
        assert result["company"] == "FailCo"
        # Company intel fields should be absent
        assert "company_rating" not in result
        assert "interview_difficulty" not in result
        assert "sample_questions" not in result
        # Mock topics and fit data should be present
        assert result["mock_topics"] == ["React", "CSS", "JavaScript"]
        assert result["matched_skills"] == ["React", "TypeScript"]
        assert result["missing_skills"] == ["Vue"]


# =====================================================================
# From test_consolidation.py — saved jobs action routing & validation
# =====================================================================

class TestSavedJobsConsolidation:
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
