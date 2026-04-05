"""Tests for event subscribers — verify each handler stores correct notifications."""

import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

from naukri_server.events import (
    ApplicationSubmitted, ApplicationStatusChanged, ApplicationStale,
    ApplicationInterviewScheduled, SyncCompleted, RecruiterEngaged,
    ReminderDue, ProfileScoreChanged,
)


class TestApplicationSubmittedSubscriber:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_stores_notification(self, mock_store):
        from naukri_server.subscribers import _on_application_submitted
        event = ApplicationSubmitted(job_id="J1", company="Acme", title="SDE")
        await _on_application_submitted(event)
        mock_store.assert_called_once()
        notif = mock_store.call_args[0][0]
        assert notif["event_type"] == "ApplicationSubmitted"
        assert "Acme" in notif["title"]
        assert notif["priority"] == "medium"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_no_job_id_skips(self, mock_store):
        from naukri_server.subscribers import _on_application_submitted
        event = ApplicationSubmitted(job_id="", company="Acme", title="SDE")
        await _on_application_submitted(event)
        mock_store.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_title_in_notification(self, mock_store):
        from naukri_server.subscribers import _on_application_submitted
        event = ApplicationSubmitted(job_id="J2", company="BigCo", title="Staff Engineer")
        await _on_application_submitted(event)
        notif = mock_store.call_args[0][0]
        assert "Staff Engineer" in notif["title"]
        assert "BigCo" in notif["title"]

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_metadata_contains_job_id(self, mock_store):
        from naukri_server.subscribers import _on_application_submitted
        event = ApplicationSubmitted(job_id="J3", company="TestCo", title="SDE")
        await _on_application_submitted(event)
        notif = mock_store.call_args[0][0]
        assert notif["metadata"]["job_id"] == "J3"
        assert notif["metadata"]["company"] == "TestCo"


class TestStatusChangeSubscriber:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_positive_transition_high_priority(self, mock_store):
        from naukri_server.subscribers import _on_status_change
        event = ApplicationStatusChanged(
            job_id="J1", old_status="applied", new_status="interview", company="Acme"
        )
        await _on_status_change(event)
        mock_store.assert_called_once()
        notif = mock_store.call_args[0][0]
        assert notif["priority"] == "high"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_neutral_transition_medium_priority(self, mock_store):
        from naukri_server.subscribers import _on_status_change
        event = ApplicationStatusChanged(
            job_id="J1", old_status="applied", new_status="rejected", company="Acme"
        )
        await _on_status_change(event)
        notif = mock_store.call_args[0][0]
        assert notif["priority"] == "medium"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_no_job_id_skips(self, mock_store):
        from naukri_server.subscribers import _on_status_change
        event = ApplicationStatusChanged(
            job_id="", old_status="applied", new_status="interview", company="Acme"
        )
        await _on_status_change(event)
        mock_store.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_no_new_status_skips(self, mock_store):
        from naukri_server.subscribers import _on_status_change
        event = ApplicationStatusChanged(
            job_id="J1", old_status="applied", new_status="", company="Acme"
        )
        await _on_status_change(event)
        mock_store.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_title_contains_transition(self, mock_store):
        from naukri_server.subscribers import _on_status_change
        event = ApplicationStatusChanged(
            job_id="J1", old_status="applied", new_status="viewed", company="Acme"
        )
        await _on_status_change(event)
        notif = mock_store.call_args[0][0]
        assert "applied" in notif["title"]
        assert "viewed" in notif["title"]
        assert "Acme" in notif["title"]


class TestStaleSubscriber:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.workflows.stale_follow_up_workflow", new_callable=AsyncMock)
    async def test_high_priority_triggers_workflow(self, mock_workflow, mock_store):
        from naukri_server.subscribers import _on_application_stale
        event = ApplicationStale(job_id="J1", company="Acme", follow_up_priority=80)
        await _on_application_stale(event)
        mock_store.assert_called_once()
        mock_workflow.assert_called_once_with(job_id="J1", company="Acme")

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.workflows.stale_follow_up_workflow", new_callable=AsyncMock)
    async def test_low_priority_no_workflow(self, mock_workflow, mock_store):
        from naukri_server.subscribers import _on_application_stale
        event = ApplicationStale(job_id="J1", company="Acme", follow_up_priority=50)
        await _on_application_stale(event)
        mock_store.assert_called_once()
        mock_workflow.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.workflows.stale_follow_up_workflow", new_callable=AsyncMock)
    async def test_boundary_70_triggers_workflow(self, mock_workflow, mock_store):
        from naukri_server.subscribers import _on_application_stale
        event = ApplicationStale(job_id="J1", company="Acme", follow_up_priority=70)
        await _on_application_stale(event)
        mock_workflow.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.workflows.stale_follow_up_workflow", new_callable=AsyncMock)
    async def test_boundary_69_no_workflow(self, mock_workflow, mock_store):
        from naukri_server.subscribers import _on_application_stale
        event = ApplicationStale(job_id="J1", company="Acme", follow_up_priority=69)
        await _on_application_stale(event)
        mock_workflow.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.workflows.stale_follow_up_workflow", new_callable=AsyncMock)
    async def test_high_priority_notification_priority(self, mock_workflow, mock_store):
        from naukri_server.subscribers import _on_application_stale
        event = ApplicationStale(job_id="J1", company="Acme", follow_up_priority=80)
        await _on_application_stale(event)
        notif = mock_store.call_args[0][0]
        assert notif["priority"] == "high"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.workflows.stale_follow_up_workflow", new_callable=AsyncMock)
    async def test_low_priority_notification_priority(self, mock_workflow, mock_store):
        from naukri_server.subscribers import _on_application_stale
        event = ApplicationStale(job_id="J1", company="Acme", follow_up_priority=50)
        await _on_application_stale(event)
        notif = mock_store.call_args[0][0]
        assert notif["priority"] == "medium"


class TestInterviewSubscriber:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.workflows.interview_lifecycle_workflow", new_callable=AsyncMock)
    async def test_triggers_workflow(self, mock_workflow, mock_store):
        from naukri_server.subscribers import _on_interview_scheduled
        event = ApplicationInterviewScheduled(
            job_id="J1", company="Acme", round_type="technical"
        )
        await _on_interview_scheduled(event)
        mock_store.assert_called_once()
        mock_workflow.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.workflows.interview_lifecycle_workflow", new_callable=AsyncMock)
    async def test_notification_is_high_priority(self, mock_workflow, mock_store):
        from naukri_server.subscribers import _on_interview_scheduled
        event = ApplicationInterviewScheduled(
            job_id="J1", company="Acme", round_type="HR"
        )
        await _on_interview_scheduled(event)
        notif = mock_store.call_args[0][0]
        assert notif["priority"] == "high"
        assert "HR" in notif["title"]

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.workflows.interview_lifecycle_workflow", new_callable=AsyncMock)
    async def test_workflow_receives_correct_args(self, mock_workflow, mock_store):
        from naukri_server.subscribers import _on_interview_scheduled
        event = ApplicationInterviewScheduled(
            job_id="J5", company="BigCo", round_type="system_design"
        )
        await _on_interview_scheduled(event)
        mock_workflow.assert_called_once_with(
            job_id="J5", company="BigCo", round_type="system_design"
        )


class TestSyncCompletedSubscriber:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_no_changes_no_notification(self, mock_store):
        from naukri_server.subscribers import _on_sync_completed
        event = SyncCompleted(
            entity="applications", new_added=0, updated=0, status_changes_count=0
        )
        await _on_sync_completed(event)
        mock_store.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_changes_stores_notification(self, mock_store):
        from naukri_server.subscribers import _on_sync_completed
        event = SyncCompleted(
            entity="applications", new_added=3, updated=2, status_changes_count=1
        )
        await _on_sync_completed(event)
        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_only_new_added_stores_notification(self, mock_store):
        from naukri_server.subscribers import _on_sync_completed
        event = SyncCompleted(
            entity="applications", new_added=5, updated=0, status_changes_count=0
        )
        await _on_sync_completed(event)
        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_only_status_changes_stores_notification(self, mock_store):
        from naukri_server.subscribers import _on_sync_completed
        event = SyncCompleted(
            entity="applications", new_added=0, updated=0, status_changes_count=2
        )
        await _on_sync_completed(event)
        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_notification_priority_is_low(self, mock_store):
        from naukri_server.subscribers import _on_sync_completed
        event = SyncCompleted(
            entity="applications", new_added=1, updated=0, status_changes_count=0
        )
        await _on_sync_completed(event)
        notif = mock_store.call_args[0][0]
        assert notif["priority"] == "low"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_only_updated_no_notification(self, mock_store):
        """updated > 0 but new_added == 0 and status_changes_count == 0 => no notification."""
        from naukri_server.subscribers import _on_sync_completed
        event = SyncCompleted(
            entity="applications", new_added=0, updated=5, status_changes_count=0
        )
        await _on_sync_completed(event)
        mock_store.assert_not_called()


class TestRecruiterSubscriber:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_stores_high_priority(self, mock_store):
        from naukri_server.subscribers import _on_recruiter_engaged
        event = RecruiterEngaged(job_id="J1", company="Acme")
        await _on_recruiter_engaged(event)
        notif = mock_store.call_args[0][0]
        assert notif["priority"] == "high"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_metadata_has_job_id(self, mock_store):
        from naukri_server.subscribers import _on_recruiter_engaged
        event = RecruiterEngaged(job_id="J2", company="BigCo")
        await _on_recruiter_engaged(event)
        notif = mock_store.call_args[0][0]
        assert notif["metadata"]["job_id"] == "J2"
        assert notif["metadata"]["company"] == "BigCo"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_title_mentions_company(self, mock_store):
        from naukri_server.subscribers import _on_recruiter_engaged
        event = RecruiterEngaged(job_id="J1", company="TechInc")
        await _on_recruiter_engaged(event)
        notif = mock_store.call_args[0][0]
        assert "TechInc" in notif["title"]


class TestReminderDueSubscriber:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_stores_notification(self, mock_store):
        from naukri_server.subscribers import _on_reminder_due
        event = ReminderDue(job_id="J1", company="Acme", note="Follow up")
        await _on_reminder_due(event)
        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_priority_is_high(self, mock_store):
        from naukri_server.subscribers import _on_reminder_due
        event = ReminderDue(job_id="J1", company="Acme", note="Follow up")
        await _on_reminder_due(event)
        notif = mock_store.call_args[0][0]
        assert notif["priority"] == "high"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_body_uses_note(self, mock_store):
        from naukri_server.subscribers import _on_reminder_due
        event = ReminderDue(job_id="J1", company="Acme", note="Check status update")
        await _on_reminder_due(event)
        notif = mock_store.call_args[0][0]
        assert notif["body"] == "Check status update"


class TestProfileScoreChangedSubscriber:
    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_stores_notification(self, mock_store):
        from naukri_server.subscribers import _on_profile_score_changed
        event = ProfileScoreChanged(old_score=60, new_score=85)
        await _on_profile_score_changed(event)
        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_priority_is_low(self, mock_store):
        from naukri_server.subscribers import _on_profile_score_changed
        event = ProfileScoreChanged(old_score=50, new_score=70)
        await _on_profile_score_changed(event)
        notif = mock_store.call_args[0][0]
        assert notif["priority"] == "low"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_title_contains_new_score(self, mock_store):
        from naukri_server.subscribers import _on_profile_score_changed
        event = ProfileScoreChanged(old_score=50, new_score=85)
        await _on_profile_score_changed(event)
        notif = mock_store.call_args[0][0]
        assert "85%" in notif["title"]

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    async def test_event_type_correct(self, mock_store):
        from naukri_server.subscribers import _on_profile_score_changed
        event = ProfileScoreChanged(old_score=40, new_score=90)
        await _on_profile_score_changed(event)
        notif = mock_store.call_args[0][0]
        assert notif["event_type"] == "ProfileScoreChanged"
