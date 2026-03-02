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
        """A brand-new job_id should be appended to the applications list."""
        from naukri_server.tools.tracking import record_application

        captured = {}

        def fake_save(path, data):
            captured["data"] = data

        with patch("naukri_server.tools.tracking._load_json", return_value=[]) as mock_load, \
             patch("naukri_server.tools.tracking._save_json", side_effect=fake_save) as mock_save:
            await record_application(job_id="J100", title="SDE", company="Acme", status="applied")

        mock_load.assert_called_once()
        mock_save.assert_called_once()
        apps = captured["data"]
        assert len(apps) == 1
        assert apps[0]["job_id"] == "J100"
        assert apps[0]["title"] == "SDE"
        assert apps[0]["company"] == "Acme"
        assert apps[0]["status"] == "applied"
        assert "applied_at" in apps[0]

    @pytest.mark.asyncio
    async def test_existing_application_updated(self):
        """An existing job_id should update status and updated_at, not duplicate."""
        from naukri_server.tools.tracking import record_application

        existing = [
            {"job_id": "J200", "title": "QA", "company": "Beta", "status": "applied",
             "applied_at": "2026-01-01T00:00:00+00:00"},
        ]
        captured = {}

        def fake_save(path, data):
            captured["data"] = data

        with patch("naukri_server.tools.tracking._load_json", return_value=existing), \
             patch("naukri_server.tools.tracking._save_json", side_effect=fake_save):
            await record_application(job_id="J200", status="interviewed")

        apps = captured["data"]
        assert len(apps) == 1  # No duplicate
        assert apps[0]["status"] == "interviewed"
        assert "updated_at" in apps[0]
        # Original fields preserved
        assert apps[0]["title"] == "QA"
        assert apps[0]["company"] == "Beta"
        assert apps[0]["applied_at"] == "2026-01-01T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_update_preserves_fields_not_passed(self):
        """When updating, passing title=None should NOT overwrite existing title."""
        from naukri_server.tools.tracking import record_application

        existing = [
            {"job_id": "J300", "title": "DevOps", "company": "Gamma", "status": "applied",
             "applied_at": "2026-02-01T00:00:00+00:00"},
        ]
        captured = {}

        def fake_save(path, data):
            captured["data"] = data

        with patch("naukri_server.tools.tracking._load_json", return_value=existing), \
             patch("naukri_server.tools.tracking._save_json", side_effect=fake_save):
            # Pass title=None and company=None — should not overwrite
            await record_application(job_id="J300", status="rejected")

        app = captured["data"][0]
        assert app["title"] == "DevOps"
        assert app["company"] == "Gamma"
        assert app["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_extra_fields_merged_on_new(self):
        """Extra dict should be spread into a new application entry."""
        from naukri_server.tools.tracking import record_application

        captured = {}

        def fake_save(path, data):
            captured["data"] = data

        with patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.tracking._save_json", side_effect=fake_save):
            await record_application(job_id="J400", extra={"source": "batch", "ars_score": 85})

        app = captured["data"][0]
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

        with patch("naukri_server.tools.tracking._get_stale_applications",
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

        with patch("naukri_server.tools.tracking._get_stale_applications",
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

        with patch("naukri_server.tools.tracking._get_stale_applications",
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

        with patch("naukri_server.tools.tracking._get_stale_applications",
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

        with patch("naukri_server.tools.tracking.api_get",
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

        with patch("naukri_server.tools.tracking.api_get",
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

        with patch("naukri_server.tools.tracking.api_get",
                    new_callable=AsyncMock,
                    side_effect=NaukriAPIError(401, "Unauthorized")):
            with pytest.raises(NaukriAPIError):
                await _get_match_analytics(days=7)

    @pytest.mark.asyncio
    async def test_invalid_days_returns_error(self):
        """days < 1 should return VALIDATION_ERROR without calling API."""
        from naukri_server.tools.tracking import _get_match_analytics

        with patch("naukri_server.tools.tracking.api_get",
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

        with patch("naukri_server.config.SAVE_JOB_API", "/save/"), \
             patch("naukri_server.api.api_post",
                    new_callable=AsyncMock, return_value={}) as mock_post:
            result = await _push_save_to_naukri("J500")

        assert result is True
        mock_post.assert_awaited_once_with("/save/J500", {})

    @pytest.mark.asyncio
    async def test_api_error_returns_false(self):
        """api_post exception should be caught and return False."""
        from naukri_server.tools.tracking import _push_save_to_naukri

        with patch("naukri_server.config.SAVE_JOB_API", "/save/"), \
             patch("naukri_server.api.api_post",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("Network error")):
            result = await _push_save_to_naukri("J501")

        assert result is False

    @pytest.mark.asyncio
    async def test_no_api_configured_returns_false(self):
        """If SAVE_JOB_API is empty/None, should skip remote call and return False."""
        from naukri_server.tools.tracking import _push_save_to_naukri

        with patch("naukri_server.config.SAVE_JOB_API", ""), \
             patch("naukri_server.api.api_post",
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
        """Saving a new job should append to list and write."""
        from naukri_server.tools.tracking import _save_job

        captured = {}

        def fake_save(path, data):
            captured["data"] = data

        with patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.tracking._save_json", side_effect=fake_save):
            result = await _save_job("J600", title="ML Eng", company="DeepCo")

        assert result["status"] == "success"
        assert result["action"] == "saved"
        assert result["job_id"] == "J600"
        assert result["total_saved"] == 1
        assert result["synced_remote"] is False
        assert captured["data"][0]["title"] == "ML Eng"

    @pytest.mark.asyncio
    async def test_save_job_duplicate(self):
        """Saving a job_id that already exists should return already_saved."""
        from naukri_server.tools.tracking import _save_job

        existing = [{"job_id": "J700", "title": "X", "saved_at": "2026-01-01"}]

        with patch("naukri_server.tools.tracking._load_json", return_value=existing), \
             patch("naukri_server.tools.tracking._save_json") as mock_save:
            result = await _save_job("J700", title="Y")

        assert result["action"] == "already_saved"
        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsave_job_removes_entry(self):
        """Unsaving an existing job should remove it and call remote unsave."""
        from naukri_server.tools.tracking import _unsave_job

        existing = [
            {"job_id": "J800", "title": "Ops"},
            {"job_id": "J801", "title": "Dev"},
        ]
        captured = {}

        def fake_save(path, data):
            captured["data"] = data

        with patch("naukri_server.tools.tracking._load_json", return_value=existing), \
             patch("naukri_server.tools.tracking._save_json", side_effect=fake_save), \
             patch("naukri_server.tools.tracking.api_post",
                    new_callable=AsyncMock, return_value={}):
            result = await _unsave_job("J800")

        assert result["status"] == "success"
        assert result["action"] == "unsaved"
        # Only J801 should remain
        assert len(captured["data"]) == 1
        assert captured["data"][0]["job_id"] == "J801"

    @pytest.mark.asyncio
    async def test_unsave_job_not_found(self):
        """Unsaving a job_id not in local list should return NOT_FOUND error."""
        from naukri_server.tools.tracking import _unsave_job

        with patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.tracking._save_json") as mock_save, \
             patch("naukri_server.tools.tracking.api_post",
                    new_callable=AsyncMock, return_value={}):
            result = await _unsave_job("J999")

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"
        mock_save.assert_not_called()
