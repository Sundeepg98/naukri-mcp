"""Tests for workflow automation tools (daily brief actions, auto-reminders, etc.)."""

import pytest
from unittest.mock import AsyncMock, patch


class TestDailyBriefActions:
    """Tests for _build_recommended_actions in daily_brief.py."""

    def test_empty_brief_no_actions(self):
        """Empty brief should produce no actions."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        result = _build_recommended_actions({})
        assert result == []

    def test_unread_messages_high_priority(self):
        """Unread messages should produce high priority action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {"unread_messages": {"count": 3}}
        result = _build_recommended_actions(brief)
        assert len(result) == 1
        assert result[0]["priority"] == "high"
        assert "3" in result[0]["action"]

    def test_due_reminders_high_priority(self):
        """Due reminders should produce high priority action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {"due_reminders": {"count": 2}}
        result = _build_recommended_actions(brief)
        assert len(result) == 1
        assert result[0]["priority"] == "high"

    def test_stale_apps_medium_priority(self):
        """Stale applications should produce medium priority action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {"stale_applications": {"count": 5}}
        result = _build_recommended_actions(brief)
        assert len(result) == 1
        assert result[0]["priority"] == "medium"

    def test_low_completeness_action(self):
        """Low profile completeness should produce low priority action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {"profile_completeness": {"completeness_percent": 60}}
        result = _build_recommended_actions(brief)
        assert len(result) == 1
        assert result[0]["priority"] == "low"
        assert "60" in result[0]["action"]

    def test_high_completeness_no_action(self):
        """High profile completeness should not produce action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {"profile_completeness": {"completeness_percent": 95}}
        result = _build_recommended_actions(brief)
        assert result == []

    def test_priority_ordering(self):
        """Actions should be sorted high > medium > low."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {
            "stale_applications": {"count": 3},  # medium
            "unread_messages": {"count": 1},  # high
            "todays_applications": {"count": 0},  # low (no apps today)
        }
        result = _build_recommended_actions(brief)
        priorities = [a["priority"] for a in result]
        assert priorities == ["high", "medium", "low"]

    def test_no_applications_today_action(self):
        """No applications today should produce low priority action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {"todays_applications": {"count": 0}}
        result = _build_recommended_actions(brief)
        assert any("Apply to some jobs" in a["action"] for a in result)

    def test_pending_assessments_action(self):
        """Pending assessments should produce medium priority action."""
        from naukri_server.tools.daily_brief import _build_recommended_actions
        brief = {"assessments": {"pending": 2}}
        result = _build_recommended_actions(brief)
        assert len(result) == 1
        assert result[0]["priority"] == "medium"
        assert "assessment" in result[0]["action"]


class TestApplyTopFits:
    """Tests for naukri_smart_apply action=apply_top_fits."""

    @pytest.mark.asyncio
    async def test_apply_top_fits_no_saved_jobs(self):
        """No saved jobs should return success with 0 applied."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        with patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock) as mock_score:
            mock_score.return_value = {"status": "success", "total_saved": 0, "scored_count": 0, "scored_jobs": []}
            result = await naukri_smart_apply(action="apply_top_fits")
            assert result["applied"] == 0

    @pytest.mark.asyncio
    async def test_apply_top_fits_applies_to_scored(self):
        """Should apply to jobs above min_fit_score."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        scored = [
            {"job_id": "1", "title": "Dev", "company": "A", "fit_score": 80},
            {"job_id": "2", "title": "SDE", "company": "B", "fit_score": 70},
        ]
        with patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock) as mock_score, \
             patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock) as mock_apply:
            mock_score.return_value = {"status": "success", "total_saved": 5, "scored_count": 2, "scored_jobs": scored}
            mock_apply.return_value = {"status": "applied", "job_id": "1"}
            result = await naukri_smart_apply(action="apply_top_fits", min_fit_score=60, limit=5)
            assert result["applied"] == 2
            assert mock_apply.await_count == 2

    @pytest.mark.asyncio
    async def test_apply_top_fits_scoring_error(self):
        """If scoring fails, should return error."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        with patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock) as mock_score:
            mock_score.return_value = {"status": "error", "message": "Profile fetch failed"}
            result = await naukri_smart_apply(action="apply_top_fits")
            assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_apply_top_fits_unknown_action(self):
        """Unknown action should return VALIDATION_ERROR."""
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(action="invalid_action")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"


class TestAutoReminder:
    """Tests for set_reminder_days in naukri_applications."""

    @pytest.mark.asyncio
    async def test_apply_with_reminder_success(self):
        """Successful apply + set_reminder_days should call _set_reminder."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock) as mock_apply, \
             patch("naukri_server.tools.reminders._set_reminder", new_callable=AsyncMock) as mock_reminder, \
             patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.jobs._extract_job_id", return_value="123"):
            mock_apply.return_value = {"status": "applied", "job_id": "123", "company": "TestCo"}
            mock_reminder.return_value = {"status": "success"}
            result = await naukri_applications(action="apply", job_id="123", set_reminder_days=7)
            assert result["status"] == "applied"
            assert result["reminder_set"] is True
            assert result["reminder_days"] == 7
            mock_reminder.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_apply_without_reminder(self):
        """Apply without set_reminder_days should NOT call _set_reminder."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock) as mock_apply, \
             patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.jobs._extract_job_id", return_value="123"):
            mock_apply.return_value = {"status": "applied", "job_id": "123"}
            result = await naukri_applications(action="apply", job_id="123")
            assert result["status"] == "applied"
            assert "reminder_set" not in result

    @pytest.mark.asyncio
    async def test_apply_failed_no_reminder(self):
        """Failed apply should NOT set reminder even if set_reminder_days provided."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock) as mock_apply, \
             patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.jobs._extract_job_id", return_value="123"):
            mock_apply.return_value = {"status": "error", "message": "Failed"}
            result = await naukri_applications(action="apply", job_id="123", set_reminder_days=7)
            assert result["status"] == "error"
            # Saga runs the reminder step but it returns reminder_set=False since apply failed
            assert result.get("reminder_set") is False or "reminder_set" not in result

    @pytest.mark.asyncio
    async def test_reminder_failure_doesnt_block_apply(self):
        """Reminder failure should not block apply success."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock) as mock_apply, \
             patch("naukri_server.tools.reminders._set_reminder", new_callable=AsyncMock) as mock_reminder, \
             patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.jobs._extract_job_id", return_value="123"):
            mock_apply.return_value = {"status": "applied", "job_id": "123", "company": "TestCo"}
            mock_reminder.side_effect = Exception("Reminder DB error")
            result = await naukri_applications(action="apply", job_id="123", set_reminder_days=7)
            # Apply still succeeds even when reminder saga step fails
            assert result["status"] == "applied"
            # Saga reports the error via saga_errors (compensation path)
            assert "saga_errors" in result
            assert any("Reminder DB error" in err for err in result["saga_errors"])

    @pytest.mark.asyncio
    async def test_batch_apply_with_reminders(self):
        """Batch apply with set_reminder_days should set reminders for successful applications."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.apply._batch_apply", new_callable=AsyncMock) as mock_batch, \
             patch("naukri_server.tools.reminders._set_reminder", new_callable=AsyncMock) as mock_reminder:
            mock_batch.return_value = {
                "status": "partial_success",
                "results": [
                    {"status": "applied", "job_id": "101", "company": "AlphaCo"},
                    {"status": "error", "job_id": "102", "company": "BetaCo"},
                    {"status": "applied", "job_id": "103", "company": "GammaCo"},
                ],
            }
            mock_reminder.return_value = {"status": "success"}
            result = await naukri_applications(
                action="batch_apply", keywords="python developer", set_reminder_days=5,
            )
            assert result["reminders_set"] == 2
            assert result["reminder_days"] == 5
            assert mock_reminder.await_count == 2

    @pytest.mark.asyncio
    async def test_batch_apply_without_reminders(self):
        """Batch apply without set_reminder_days should NOT set any reminders."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.apply._batch_apply", new_callable=AsyncMock) as mock_batch:
            mock_batch.return_value = {
                "status": "success",
                "results": [
                    {"status": "applied", "job_id": "101", "company": "AlphaCo"},
                ],
            }
            result = await naukri_applications(action="batch_apply", keywords="python developer")
            assert "reminders_set" not in result

    @pytest.mark.asyncio
    async def test_batch_apply_reminder_failures_dont_block(self):
        """Batch apply reminder failures should be silently ignored."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.apply._batch_apply", new_callable=AsyncMock) as mock_batch, \
             patch("naukri_server.tools.reminders._set_reminder", new_callable=AsyncMock) as mock_reminder:
            mock_batch.return_value = {
                "status": "success",
                "results": [
                    {"status": "applied", "job_id": "101", "company": "AlphaCo"},
                    {"status": "applied", "job_id": "102", "company": "BetaCo"},
                ],
            }
            # First call succeeds, second fails
            mock_reminder.side_effect = [{"status": "success"}, Exception("DB error")]
            result = await naukri_applications(
                action="batch_apply", keywords="python developer", set_reminder_days=3,
            )
            # Only one reminder succeeded
            assert result["reminders_set"] == 1
            assert result["reminder_days"] == 3

    @pytest.mark.asyncio
    async def test_batch_apply_no_successful_apps_no_reminders(self):
        """Batch apply with all failures should not set any reminders."""
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.tools.apply._batch_apply", new_callable=AsyncMock) as mock_batch:
            mock_batch.return_value = {
                "status": "error",
                "results": [
                    {"status": "error", "job_id": "101"},
                    {"status": "needs_input", "job_id": "102"},
                ],
            }
            result = await naukri_applications(
                action="batch_apply", keywords="python developer", set_reminder_days=7,
            )
            assert "reminders_set" not in result
