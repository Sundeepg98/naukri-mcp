"""Tests for EventBus + Saga wiring into sync, apply, and tracking workflows.

Every test is PURE: no network, no browser, no file I/O.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =====================================================================
# 1. Sync emits ApplicationStatusChanged events
# =====================================================================

class TestSyncEmitsStatusChangedEvents:
    """Verify _sync_applications emits ApplicationStatusChanged for each status change."""

    @pytest.mark.asyncio
    async def test_sync_emits_status_changed_events(self):
        """After merge detects status changes, events are emitted via event_bus."""
        from naukri_server.events import EventBus, ApplicationStatusChanged

        # Fresh event bus for isolation
        test_bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        test_bus.subscribe(ApplicationStatusChanged, handler)

        # Local apps with old statuses
        local_apps = [
            {"job_id": "101", "title": "SDE", "company": "Acme", "status": "applied", "applied_at": "2026-01-01T00:00:00"},
            {"job_id": "102", "title": "SSE", "company": "Beta", "status": "applied", "applied_at": "2026-01-02T00:00:00"},
        ]
        # Remote has updated statuses
        remote_rest_data = [
            {"jobId": "101", "jobTitle": "SDE", "company": "Acme", "statusMsg": "Viewed"},
            {"jobId": "102", "jobTitle": "SSE", "company": "Beta", "statusMsg": "Shortlisted"},
        ]

        with (
            patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock, return_value=remote_rest_data),
            patch("naukri_server.tools.sync._load_json", return_value=list(local_apps)),
            patch("naukri_server.tools.sync._save_json"),
            patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock, return_value={}),
            patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock),
            patch("naukri_server.tools.sync.event_bus", test_bus),
        ):
            from naukri_server.tools.sync import _sync_applications
            result = await _sync_applications()

        assert result["status"] == "success"
        assert len(result.get("status_changes", [])) == 2

        # Events should have been emitted
        assert len(received) == 2
        assert all(isinstance(e, ApplicationStatusChanged) for e in received)

        # Verify event details
        ids = {e.job_id for e in received}
        assert ids == {"101", "102"}

        evt_101 = next(e for e in received if e.job_id == "101")
        assert evt_101.old_status == "applied"
        assert evt_101.new_status == "viewed_by_recruiter"
        assert evt_101.company == "Acme"

        evt_102 = next(e for e in received if e.job_id == "102")
        assert evt_102.old_status == "applied"
        assert evt_102.new_status == "shortlisted"

    @pytest.mark.asyncio
    async def test_sync_no_events_when_no_changes(self):
        """When statuses haven't changed, no events are emitted."""
        from naukri_server.events import EventBus, ApplicationStatusChanged

        test_bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        test_bus.subscribe(ApplicationStatusChanged, handler)

        local_apps = [
            {"job_id": "101", "title": "SDE", "company": "Acme", "status": "applied", "applied_at": "2026-01-01T00:00:00"},
        ]
        # Remote has the same status
        remote_rest_data = [
            {"jobId": "101", "jobTitle": "SDE", "company": "Acme", "statusMsg": "Applied"},
        ]

        with (
            patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock, return_value=remote_rest_data),
            patch("naukri_server.tools.sync._load_json", return_value=list(local_apps)),
            patch("naukri_server.tools.sync._save_json"),
            patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock, return_value={}),
            patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock),
            patch("naukri_server.tools.sync.event_bus", test_bus),
        ):
            from naukri_server.tools.sync import _sync_applications
            result = await _sync_applications()

        assert result["status"] == "success"
        assert "status_changes" not in result
        assert len(received) == 0


# =====================================================================
# 2. Apply emits ApplicationSubmitted event
# =====================================================================

class TestApplyEmitsSubmittedEvent:
    """Verify _apply_single emits ApplicationSubmitted on successful application."""

    @pytest.mark.asyncio
    async def test_apply_emits_submitted_on_success(self):
        """Successful apply (status 200) should emit ApplicationSubmitted."""
        from naukri_server.events import EventBus, ApplicationSubmitted

        test_bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        test_bus.subscribe(ApplicationSubmitted, handler)

        api_response = {
            "jobs": [{"status": 200}],
            "quotaDetails": {"dailyApplied": 5},
        }

        with (
            patch("naukri_server.tools.apply.api_client") as mock_api,
            patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock),
            patch("naukri_server.tools.apply._extract_job_id", return_value="12345"),
            patch("naukri_server.tools.apply._cache_lock", asyncio.Lock()),
            patch("naukri_server.tools.apply._load_cache", return_value={}),
            patch("naukri_server.tools.apply.event_bus", test_bus),
        ):
            mock_api.post = AsyncMock(return_value=api_response)
            from naukri_server.tools.apply import _apply_single
            result = await _apply_single("12345", title="SDE", company="Acme")

        assert result["status"] == "applied"
        assert len(received) == 1
        evt = received[0]
        assert isinstance(evt, ApplicationSubmitted)
        assert evt.job_id == "12345"
        assert evt.company == "Acme"
        assert evt.title == "SDE"

    @pytest.mark.asyncio
    async def test_apply_no_event_on_error(self):
        """Failed apply should NOT emit ApplicationSubmitted."""
        from naukri_server.events import EventBus, ApplicationSubmitted

        test_bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        test_bus.subscribe(ApplicationSubmitted, handler)

        api_response = {
            "jobs": [{"status": 400}],
        }

        with (
            patch("naukri_server.tools.apply.api_client") as mock_api,
            patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock),
            patch("naukri_server.tools.apply._extract_job_id", return_value="12345"),
            patch("naukri_server.tools.apply._cache_lock", asyncio.Lock()),
            patch("naukri_server.tools.apply._load_cache", return_value={}),
            patch("naukri_server.tools.apply.event_bus", test_bus),
        ):
            mock_api.post = AsyncMock(return_value=api_response)
            from naukri_server.tools.apply import _apply_single
            result = await _apply_single("12345", title="SDE", company="Acme")

        assert result["status"] == "error"
        assert len(received) == 0


# =====================================================================
# 3. Apply saga reports completed steps
# =====================================================================

class TestApplySagaSteps:
    """Verify _do_apply saga wrapper reports completed_steps in result."""

    @pytest.mark.asyncio
    async def test_saga_reports_apply_step(self):
        """Successful apply via _do_apply should include saga_steps in result."""
        apply_result = {"status": "applied", "job_id": "999"}

        with (
            patch("naukri_server.tools.tracking._load_json", return_value=[]),
            patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock, return_value=apply_result),
            patch("naukri_server.tools.jobs._extract_job_id", return_value="999"),
        ):
            from naukri_server.tools.tracking import _do_apply
            result = await _do_apply(job_id="999")

        assert result["status"] == "applied"
        assert "saga_steps" in result
        assert "apply" in result["saga_steps"]

    @pytest.mark.asyncio
    async def test_saga_reports_apply_and_reminder_steps(self):
        """Apply + reminder via _do_apply should report both saga steps."""
        apply_result = {"status": "applied", "job_id": "999", "company": "TestCorp"}

        async def mock_set_reminder(**kwargs):
            pass

        with (
            patch("naukri_server.tools.tracking._load_json", return_value=[]),
            patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock, return_value=apply_result),
            patch("naukri_server.tools.jobs._extract_job_id", return_value="999"),
            patch("naukri_server.tools.reminders._set_reminder", new_callable=AsyncMock, side_effect=mock_set_reminder),
        ):
            from naukri_server.tools.tracking import _do_apply
            result = await _do_apply(job_id="999", set_reminder_days=7)

        assert result["status"] == "applied"
        assert "saga_steps" in result
        assert result["saga_steps"] == ["apply", "reminder"]
        assert result.get("reminder_set") is True
        assert result.get("reminder_days") == 7

    @pytest.mark.asyncio
    async def test_saga_apply_only_when_no_reminder(self):
        """Without set_reminder_days, saga has only the apply step."""
        apply_result = {"status": "applied", "job_id": "999"}

        with (
            patch("naukri_server.tools.tracking._load_json", return_value=[]),
            patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock, return_value=apply_result),
            patch("naukri_server.tools.jobs._extract_job_id", return_value="999"),
        ):
            from naukri_server.tools.tracking import _do_apply
            result = await _do_apply(job_id="999")

        assert result["status"] == "applied"
        assert result["saga_steps"] == ["apply"]
