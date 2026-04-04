"""Tests for draft_follow_up and recruiter_history actions in tracking.py.

All tests are PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone, timedelta


class TestDraftFollowUp:
    """Tests for naukri_server.tools.tracking._draft_follow_up."""

    @pytest.mark.asyncio
    async def test_draft_follow_up_generates_message_with_date(self):
        """Draft follow-up should produce correct message with formatted date and days count."""
        from naukri_server.tools.tracking import _draft_follow_up

        applied_at = "2026-03-01T10:00:00+00:00"
        app = {
            "job_id": "J100",
            "title": "Backend Developer",
            "company": "Acme Corp",
            "status": "applied",
            "applied_at": applied_at,
        }

        with patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value=app):
            result = await _draft_follow_up("J100")

        assert result["status"] == "success"
        assert result["job_id"] == "J100"
        assert result["company"] == "Acme Corp"
        assert result["title"] == "Backend Developer"
        assert result["days_since_applied"] >= 0
        assert "Backend Developer" in result["draft_message"]
        assert "Acme Corp" in result["draft_message"]
        assert "March 01, 2026" in result["draft_message"]
        assert result["suggested_subject"] == "Follow-up: Backend Developer Application at Acme Corp"

    @pytest.mark.asyncio
    async def test_draft_follow_up_not_found(self):
        """Should return NOT_FOUND error when job_id does not exist."""
        from naukri_server.tools.tracking import _draft_follow_up

        with patch("naukri_server.database.get_application", new_callable=AsyncMock, return_value=None):
            result = await _draft_follow_up("NONEXISTENT")

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"
        assert "NONEXISTENT" in result["message"]

    @pytest.mark.asyncio
    async def test_draft_follow_up_dispatch_requires_job_id(self):
        """Calling via naukri_applications with action=draft_follow_up but no job_id returns VALIDATION_ERROR."""
        from naukri_server.tools.tracking import naukri_applications

        result = await naukri_applications(action="draft_follow_up")

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]


class TestRecruiterHistory:
    """Tests for naukri_server.tools.tracking._recruiter_history."""

    @pytest.mark.asyncio
    async def test_recruiter_history_groups_by_company(self):
        """Should aggregate applications per company and detect responsive ones."""
        from naukri_server.tools.tracking import _recruiter_history

        rows = [
            {"company": "Acme", "applications": 2, "first_applied": "2026-01-10T00:00:00+00:00",
             "last_applied": "2026-01-15T00:00:00+00:00", "statuses": "applied,viewed"},
            {"company": "Beta Inc", "applications": 1, "first_applied": "2026-02-01T00:00:00+00:00",
             "last_applied": "2026-02-01T00:00:00+00:00", "statuses": "applied"},
            {"company": "Gamma LLC", "applications": 1, "first_applied": "2026-02-10T00:00:00+00:00",
             "last_applied": "2026-02-10T00:00:00+00:00", "statuses": "interview"},
        ]

        with patch("naukri_server.database.get_recruiter_history", new_callable=AsyncMock, return_value=rows):
            result = await _recruiter_history()

        assert result["status"] == "success"
        assert result["total_companies"] == 3

        # Acme has 2 applications and is responsive (has "viewed" status)
        acme = next(c for c in result["companies"] if c["company"] == "Acme")
        assert acme["applications"] == 2
        assert acme["has_response"] is True
        assert acme["first_applied"] == "2026-01-10T00:00:00+00:00"
        assert acme["last_applied"] == "2026-01-15T00:00:00+00:00"
        assert "applied" in acme["statuses"]
        assert "viewed" in acme["statuses"]

        # Beta has 1 application, not responsive
        beta = next(c for c in result["companies"] if c["company"] == "Beta Inc")
        assert beta["applications"] == 1
        assert beta["has_response"] is False

        # Gamma is responsive (interview)
        gamma = next(c for c in result["companies"] if c["company"] == "Gamma LLC")
        assert gamma["has_response"] is True

        # responsive_count = 2 (Acme + Gamma), unresponsive = 1 (Beta)
        assert result["responsive_count"] == 2
        assert result["unresponsive_count"] == 1

        # Sorted by most applications first — Acme (2) should be first
        assert result["companies"][0]["company"] == "Acme"

    @pytest.mark.asyncio
    async def test_recruiter_history_empty_apps(self):
        """Empty applications list should return zero counts."""
        from naukri_server.tools.tracking import _recruiter_history

        with patch("naukri_server.database.get_recruiter_history", new_callable=AsyncMock, return_value=[]):
            result = await _recruiter_history()

        assert result["status"] == "success"
        assert result["total_companies"] == 0
        assert result["responsive_count"] == 0
        assert result["unresponsive_count"] == 0
        assert result["companies"] == []
