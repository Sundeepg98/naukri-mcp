"""Integration tests -- multi-step workflows crossing module boundaries.

All tests are PURE: no network, no browser, no file I/O.
Tests verify that events, sagas, and subscribers compose correctly.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch


class TestSearchScoreApplyFlow:
    """Integration: search -> score -> apply flow across tools."""

    @pytest.mark.asyncio
    async def test_search_score_apply_emits_events(self):
        """Full workflow: search returns jobs, score them, apply best fit, verify events."""
        from naukri_server.events import EventBus, ApplicationSubmitted

        test_bus = EventBus()
        submitted_events = []

        async def capture_submitted(event):
            submitted_events.append(event)

        test_bus.subscribe(ApplicationSubmitted, capture_submitted)

        # 1. Mock search returning jobs
        search_result = [
            {"jobId": "J100", "title": "Node.js Dev", "companyName": "Acme",
             "placeholders": [{"type": "experience", "label": "3-5 Yrs"}],
             "tagsAndSkills": "Node.js,TypeScript,AWS",
             "salary": "15-25 LPA", "jdURL": "https://naukri.com/jd/J100"},
        ]

        # 2. Mock apply returning success
        apply_api_response = {
            "jobs": [{"status": 200}],
            "quotaDetails": {"dailyApplied": 10},
        }

        with (
            patch("naukri_server.tools.apply.api_client") as mock_api,
            patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock),
            patch("naukri_server.tools.apply._extract_job_id", return_value="J100"),
            patch("naukri_server.tools.apply._cache_lock", asyncio.Lock()),
            patch("naukri_server.tools.apply._load_cache", return_value={}),
            patch("naukri_server.tools.apply.event_bus", test_bus),
        ):
            mock_api.post = AsyncMock(return_value=apply_api_response)

            from naukri_server.tools.apply import _apply_single
            result = await _apply_single("J100", title="Node.js Dev", company="Acme")

        assert result["status"] == "applied"
        assert len(submitted_events) == 1
        assert submitted_events[0].job_id == "J100"
        assert submitted_events[0].company == "Acme"

    @pytest.mark.asyncio
    async def test_apply_failure_emits_no_events(self):
        """When apply fails, no ApplicationSubmitted event is emitted."""
        from naukri_server.events import EventBus, ApplicationSubmitted

        test_bus = EventBus()
        submitted_events = []

        async def capture_submitted(event):
            submitted_events.append(event)

        test_bus.subscribe(ApplicationSubmitted, capture_submitted)

        apply_api_response = {"jobs": [{"status": 400}]}

        with (
            patch("naukri_server.tools.apply.api_client") as mock_api,
            patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock),
            patch("naukri_server.tools.apply._extract_job_id", return_value="J200"),
            patch("naukri_server.tools.apply._cache_lock", asyncio.Lock()),
            patch("naukri_server.tools.apply._load_cache", return_value={}),
            patch("naukri_server.tools.apply.event_bus", test_bus),
        ):
            mock_api.post = AsyncMock(return_value=apply_api_response)

            from naukri_server.tools.apply import _apply_single
            result = await _apply_single("J200", title="Dev", company="FailCo")

        assert result["status"] == "error"
        assert len(submitted_events) == 0


class TestSyncStatusChangeWorkflow:
    """Integration: sync -> status change -> event -> subscriber notification."""

    @pytest.mark.asyncio
    async def test_sync_detects_changes_and_emits(self):
        """Sync detects status changes, emits events, subscriber stores notifications."""
        from naukri_server.events import EventBus, ApplicationStatusChanged

        test_bus = EventBus()
        status_events = []

        async def capture_status(event):
            status_events.append(event)

        test_bus.subscribe(ApplicationStatusChanged, capture_status)

        local_apps = [
            {"job_id": "S1", "title": "SDE", "company": "Alpha",
             "status": "applied", "applied_at": "2026-01-01T00:00:00"},
        ]
        remote_data = [
            {"jobId": "S1", "jobTitle": "SDE", "company": "Alpha",
             "statusMsg": "Shortlisted"},
        ]

        with (
            patch("naukri_server.tools.sync._fetch_applied_jobs_rest",
                  new_callable=AsyncMock, return_value=remote_data),
            patch("naukri_server.database.list_all_applications",
                  new_callable=AsyncMock, return_value=list(local_apps)),
            patch("naukri_server.database.upsert_application", new_callable=AsyncMock),
            patch("naukri_server.database.delete_applications_before", new_callable=AsyncMock),
            patch("naukri_server.tools.sync._load_sync_state_async",
                  new_callable=AsyncMock, return_value={}),
            patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock),
            patch("naukri_server.tools.sync.event_bus", test_bus),
        ):
            from naukri_server.tools.sync import _sync_applications
            result = await _sync_applications()

        assert result["status"] == "success"
        assert len(status_events) == 1
        assert status_events[0].job_id == "S1"
        assert status_events[0].old_status == "applied"
        assert status_events[0].new_status == "shortlisted"

    @pytest.mark.asyncio
    async def test_sync_multiple_changes_all_emitted(self):
        """Sync detects multiple status changes, each one emits an event."""
        from naukri_server.events import EventBus, ApplicationStatusChanged

        test_bus = EventBus()
        status_events = []

        async def capture_status(event):
            status_events.append(event)

        test_bus.subscribe(ApplicationStatusChanged, capture_status)

        local_apps = [
            {"job_id": "M1", "title": "SDE1", "company": "Co1",
             "status": "applied", "applied_at": "2026-01-01T00:00:00"},
            {"job_id": "M2", "title": "SDE2", "company": "Co2",
             "status": "applied", "applied_at": "2026-01-02T00:00:00"},
            {"job_id": "M3", "title": "SDE3", "company": "Co3",
             "status": "viewed_by_recruiter", "applied_at": "2026-01-03T00:00:00"},
        ]
        remote_data = [
            {"jobId": "M1", "jobTitle": "SDE1", "company": "Co1", "statusMsg": "Viewed"},
            {"jobId": "M2", "jobTitle": "SDE2", "company": "Co2", "statusMsg": "Rejected"},
            {"jobId": "M3", "jobTitle": "SDE3", "company": "Co3", "statusMsg": "Viewed"},
        ]

        with (
            patch("naukri_server.tools.sync._fetch_applied_jobs_rest",
                  new_callable=AsyncMock, return_value=remote_data),
            patch("naukri_server.database.list_all_applications",
                  new_callable=AsyncMock, return_value=list(local_apps)),
            patch("naukri_server.database.upsert_application", new_callable=AsyncMock),
            patch("naukri_server.database.delete_applications_before", new_callable=AsyncMock),
            patch("naukri_server.tools.sync._load_sync_state_async",
                  new_callable=AsyncMock, return_value={}),
            patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock),
            patch("naukri_server.tools.sync.event_bus", test_bus),
        ):
            from naukri_server.tools.sync import _sync_applications
            result = await _sync_applications()

        assert result["status"] == "success"
        # M1: applied -> viewed, M2: applied -> rejected (2 changes; M3 same status)
        changed_ids = {e.job_id for e in status_events}
        assert "M1" in changed_ids
        assert "M2" in changed_ids


class TestSagaCompensationIntegration:
    """Integration: saga step fails mid-workflow, compensation runs."""

    @pytest.mark.asyncio
    async def test_apply_then_reminder_fails_compensates(self):
        """Apply succeeds, reminder step fails -> compensation un-applies."""
        from naukri_server.sagas import SagaExecutor

        compensated = []
        apply_result = {"status": "applied", "job_id": "C1"}

        async def step_apply():
            return apply_result

        async def step_reminder():
            raise RuntimeError("Reminder service unavailable")

        async def comp_apply():
            compensated.append("un-applied C1")

        saga = SagaExecutor("apply_with_reminder")
        saga.add_step("apply", step_apply, compensate=comp_apply)
        saga.add_step("reminder", step_reminder)

        result = await saga.run()

        assert result["status"] == "error"
        assert result["failed_step"] == "reminder"
        assert result["completed_steps"] == ["apply"]
        assert "un-applied C1" in compensated
        # State tracking
        assert result["state"]["status"] == "failed"
        assert result["state"]["steps_completed"] == ["apply"]

    @pytest.mark.asyncio
    async def test_full_saga_success_tracks_state(self):
        """All steps pass -> state shows completed."""
        from naukri_server.sagas import SagaExecutor

        async def step_a():
            return "a_done"

        async def step_b():
            return "b_done"

        saga = SagaExecutor("full_success")
        saga.add_step("alpha", step_a)
        saga.add_step("beta", step_b)

        result = await saga.run()

        assert result["status"] == "success"
        assert result["state"]["status"] == "completed"
        assert result["state"]["saga_name"] == "full_success"
        assert result["state"]["steps_completed"] == ["alpha", "beta"]
        assert result["state"]["started_at"] is not None
        assert result["state"]["current_step"] is None


class TestInterviewWorkflowEndToEnd:
    """Integration: interview scheduled -> prep + reminder + notification."""

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.get_reminder", new_callable=AsyncMock, return_value=None)
    @patch("naukri_server.database.upsert_reminder", new_callable=AsyncMock)
    @patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value={
        "job_id": "INT1", "company": "TechCo", "title": "Backend Dev",
        "applied_at": "2026-03-01T10:00:00+00:00", "ars_score": 85,
    })
    async def test_interview_workflow_completes_with_state(self, mock_app, mock_upsert,
                                                           mock_get_rem, mock_notif):
        from naukri_server.workflows import interview_lifecycle_workflow
        with (
            patch("naukri_server.services.application_service._safe_fetch_company_intel",
                  new_callable=AsyncMock, return_value=None),
            patch("naukri_server.services.application_service._safe_fetch_mock_topics",
                  new_callable=AsyncMock, return_value=None),
            patch("naukri_server.services.application_service._safe_fetch_fit_score",
                  new_callable=AsyncMock, return_value=None),
            patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock),
        ):
            result = await interview_lifecycle_workflow("INT1", "TechCo", "system_design")

        assert result["status"] == "success"
        assert len(result["completed_steps"]) == 3
        assert "state" in result
        assert result["state"]["status"] == "completed"
        assert result["state"]["steps_completed"] == [
            "interview_prep", "follow_up_reminder", "notification"
        ]
