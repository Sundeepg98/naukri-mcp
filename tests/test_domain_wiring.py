"""Tests for domain entity wiring — models used by tools, event subscribers registered.

Covers:
- _list_applications returns enriched fields (is_stale, days_since_applied, has_recruiter_interest)
- auto_hunt results include salary_disclosed and experience_match
- EventBus default subscribers are registered at module load
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# 1. _list_applications returns enriched fields
# ---------------------------------------------------------------------------

class TestListApplicationsEnrichment:

    @pytest.mark.asyncio
    async def test_enriched_fields_present_in_response(self):
        """_list_applications adds is_stale, days_since_applied, has_recruiter_interest."""
        from naukri_server.tools.tracking import _list_applications

        old_date = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

        mock_apps = [
            {
                "job_id": "stale1",
                "title": "Old Job",
                "company": "OldCo",
                "status": "applied",
                "applied_at": old_date,
                "view_count": 0,
                "ars_score": 30,
            },
            {
                "job_id": "recent1",
                "title": "New Job",
                "company": "NewCo",
                "status": "shortlisted",
                "applied_at": recent_date,
                "view_count": 5,
                "ars_score": 80,
            },
        ]

        with patch("naukri_server.tools.tracking._load_json", return_value=mock_apps), \
             patch("naukri_server.tools.tracking._applications_lock",
                   new_callable=asyncio.Lock):
            result = await _list_applications()

        assert result["status"] == "success"
        apps = result["applications"]
        assert len(apps) == 2

        # Stale job: applied >14 days ago, still status=applied
        stale_app = next(a for a in apps if a["job_id"] == "stale1")
        assert stale_app["is_stale"] is True
        assert stale_app["days_since_applied"] == 20
        assert stale_app["has_recruiter_interest"] is False

        # Recent job: shortlisted, has views and high ARS
        recent_app = next(a for a in apps if a["job_id"] == "recent1")
        assert recent_app["is_stale"] is False
        assert recent_app["days_since_applied"] == 2
        assert recent_app["has_recruiter_interest"] is True


# ---------------------------------------------------------------------------
# 2. auto_hunt results include salary_disclosed and experience_match
# ---------------------------------------------------------------------------

_GOOD_PROFILE = {
    "status": "success",
    "key_skills": ["Python", "Django", "AWS"],
    "total_experience": "4 years 0 months",
    "current_location": "Bangalore",
    "expected_ctc": 20.0,
}


def _make_job(job_id, salary_min_lakhs=None, salary_max_lakhs=None,
              experience_min=None, experience_max=None):
    return {
        "job_id": str(job_id),
        "title": "Dev",
        "company": "Acme",
        "is_applied": False,
        "tags": ["python", "django"],
        "skills": None,
        "experience": "3-5 years",
        "location": "Bangalore",
        "work_mode": None,
        "salary": "10-20 Lakhs" if salary_min_lakhs else "Not Disclosed",
        "salary_min_lakhs": salary_min_lakhs,
        "salary_max_lakhs": salary_max_lakhs,
        "experience_min": experience_min,
        "experience_max": experience_max,
    }


class TestAutoHuntSalaryDisclosed:

    @pytest.mark.asyncio
    async def test_ranked_jobs_include_salary_disclosed(self):
        """auto_hunt results include salary_disclosed from Job entity."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        jobs = [
            _make_job("J1", salary_min_lakhs=10.0, salary_max_lakhs=20.0,
                      experience_min=3, experience_max=6),
            _make_job("J2", salary_min_lakhs=None, salary_max_lakhs=None,
                      experience_min=2, experience_max=5),
        ]
        search_result = {"status": "success", "jobs": jobs}

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.tracking._applications_lock",
                   new_callable=asyncio.Lock):
            mock_search.return_value = search_result
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", min_fit_score=0)

        assert result["status"] == "success"
        ranked = result["ranked_jobs"]
        assert len(ranked) == 2

        j1 = next(j for j in ranked if j["job_id"] == "J1")
        j2 = next(j for j in ranked if j["job_id"] == "J2")

        assert j1["salary_disclosed"] is True
        assert j2["salary_disclosed"] is False

        # experience_match: profile is 4 years, J1 range 3-6, J2 range 2-5
        assert j1["experience_match"] is True
        assert j2["experience_match"] is True


# ---------------------------------------------------------------------------
# 3. EventBus default subscribers are registered
# ---------------------------------------------------------------------------

class TestEventBusDefaultSubscribers:

    def test_default_subscribers_registered_at_import(self):
        """Importing events module registers _log_status_change and _log_application_submitted."""
        from naukri_server.events import (
            event_bus, ApplicationStatusChanged, ApplicationSubmitted,
            _log_status_change, _log_application_submitted,
        )

        assert event_bus.subscriber_count >= 2

        status_handlers = event_bus._subscribers.get(ApplicationStatusChanged, [])
        assert _log_status_change in status_handlers

        submitted_handlers = event_bus._subscribers.get(ApplicationSubmitted, [])
        assert _log_application_submitted in submitted_handlers

    @pytest.mark.asyncio
    async def test_default_subscriber_logs_status_change(self, caplog):
        """_log_status_change logs when an ApplicationStatusChanged is emitted."""
        import logging
        from naukri_server.events import (
            event_bus, ApplicationStatusChanged,
        )

        event = ApplicationStatusChanged(
            job_id="test123", company="TestCo",
            old_status="applied", new_status="shortlisted",
        )

        with caplog.at_level(logging.INFO, logger="naukri_server.events"):
            await event_bus.emit(event)

        assert "Status changed" in caplog.text
        assert "test123" in caplog.text
