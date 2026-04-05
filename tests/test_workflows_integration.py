"""Tests for workflow sagas — verify multi-step execution."""

import pytest
from unittest.mock import AsyncMock, patch


class TestInterviewLifecycleWorkflow:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.get_reminder", new_callable=AsyncMock, return_value=None)
    @patch("naukri_server.database.upsert_reminder", new_callable=AsyncMock)
    @patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value={
        "job_id": "J1", "company": "Acme", "title": "SDE",
        "applied_at": "2026-03-01T10:00:00+00:00", "ars_score": 70,
    })
    async def test_completes_all_steps(self, mock_app, mock_upsert, mock_get_rem, mock_notif):
        from naukri_server.workflows import interview_lifecycle_workflow
        with patch("naukri_server.services.application_service._safe_fetch_company_intel",
                   new_callable=AsyncMock, return_value=None), \
             patch("naukri_server.services.application_service._safe_fetch_mock_topics",
                   new_callable=AsyncMock, return_value=None), \
             patch("naukri_server.services.application_service._safe_fetch_fit_score",
                   new_callable=AsyncMock, return_value=None), \
             patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock):
            result = await interview_lifecycle_workflow("J1", "Acme", "technical")
        assert result["status"] == "success"
        assert len(result["completed_steps"]) == 3
        assert "interview_prep" in result["completed_steps"]
        assert "follow_up_reminder" in result["completed_steps"]
        assert "notification" in result["completed_steps"]

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.get_reminder", new_callable=AsyncMock, return_value=None)
    @patch("naukri_server.database.upsert_reminder", new_callable=AsyncMock)
    @patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value={
        "job_id": "J1", "company": "Acme", "title": "SDE",
        "applied_at": "2026-03-01T10:00:00+00:00", "ars_score": 70,
    })
    async def test_step_timings_recorded(self, mock_app, mock_upsert, mock_get_rem, mock_notif):
        from naukri_server.workflows import interview_lifecycle_workflow
        with patch("naukri_server.services.application_service._safe_fetch_company_intel",
                   new_callable=AsyncMock, return_value=None), \
             patch("naukri_server.services.application_service._safe_fetch_mock_topics",
                   new_callable=AsyncMock, return_value=None), \
             patch("naukri_server.services.application_service._safe_fetch_fit_score",
                   new_callable=AsyncMock, return_value=None), \
             patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock):
            result = await interview_lifecycle_workflow("J1", "Acme", "technical")
        assert "step_timings" in result
        assert len(result["step_timings"]) == 3

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value=None)
    async def test_prep_step_fails_gracefully(self, mock_app, mock_notif):
        """When get_application returns None, prep step returns error but saga records partial failure."""
        from naukri_server.workflows import interview_lifecycle_workflow
        with patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock):
            result = await interview_lifecycle_workflow("J_MISSING", "Unknown", "technical")
        # Prep step succeeds (returns error dict, not exception), so saga continues
        assert result["status"] == "success"
        assert "interview_prep" in result["completed_steps"]


class TestStaleFollowUpWorkflow:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value={
        "job_id": "J1", "company": "Acme", "title": "SDE",
        "applied_at": "2026-01-01T00:00:00+00:00",
    })
    async def test_completes_all_steps(self, mock_app, mock_notif):
        from naukri_server.workflows import stale_follow_up_workflow
        result = await stale_follow_up_workflow("J1", "Acme")
        assert result["status"] == "success"
        assert len(result["completed_steps"]) == 2
        assert "draft_follow_up" in result["completed_steps"]
        assert "notification" in result["completed_steps"]

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value={
        "job_id": "J2", "company": "BigCo", "title": "Backend Engineer",
        "applied_at": "2026-02-15T00:00:00+00:00",
    })
    async def test_draft_result_in_notification(self, mock_app, mock_notif):
        from naukri_server.workflows import stale_follow_up_workflow
        result = await stale_follow_up_workflow("J2", "BigCo")
        assert result["status"] == "success"
        # The notification step should have been called
        mock_notif.assert_called()
        # Find the StaleFollowUp notification (the one from the notify step)
        stale_calls = [
            c for c in mock_notif.call_args_list
            if c[0][0].get("event_type") == "StaleFollowUp"
        ]
        assert len(stale_calls) == 1
        notif = stale_calls[0][0][0]
        assert "BigCo" in notif["title"]
        assert notif["priority"] == "high"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value={
        "job_id": "J3", "company": "TestCo", "title": "SDE",
        "applied_at": "2026-01-01T00:00:00+00:00",
    })
    async def test_step_timings_recorded(self, mock_app, mock_notif):
        from naukri_server.workflows import stale_follow_up_workflow
        result = await stale_follow_up_workflow("J3", "TestCo")
        assert "step_timings" in result
        assert len(result["step_timings"]) == 2

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value=None)
    async def test_missing_application_draft_returns_error(self, mock_app, mock_notif):
        """When get_application returns None, draft_follow_up returns error dict (not exception)."""
        from naukri_server.workflows import stale_follow_up_workflow
        result = await stale_follow_up_workflow("J_MISSING", "Unknown")
        # draft_follow_up returns error dict, saga still continues (no exception)
        assert result["status"] == "success"
        assert "draft_follow_up" in result["completed_steps"]
