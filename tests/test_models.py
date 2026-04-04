"""Tests for naukri_server.models — domain objects and helpers."""

import pytest
from datetime import datetime, timezone, timedelta

from naukri_server.models import ApplicationStatus, paginate, Job, Application


# ---------------------------------------------------------------------------
# ApplicationStatus.from_string
# ---------------------------------------------------------------------------

class TestApplicationStatusFromString:
    def test_parses_known_statuses(self):
        """All canonical status strings round-trip through from_string."""
        for member in ApplicationStatus:
            assert ApplicationStatus.from_string(member.value) == member

    def test_parses_with_whitespace_and_dashes(self):
        assert ApplicationStatus.from_string("  needs-input  ") == ApplicationStatus.NEEDS_INPUT
        assert ApplicationStatus.from_string("ALREADY_APPLIED") == ApplicationStatus.ALREADY_APPLIED
        assert ApplicationStatus.from_string("Viewed By Recruiter") == ApplicationStatus.VIEWED_BY_RECRUITER

    def test_returns_unknown_for_garbage(self):
        assert ApplicationStatus.from_string("not_a_real_status") == ApplicationStatus.UNKNOWN
        assert ApplicationStatus.from_string("xyz123") == ApplicationStatus.UNKNOWN

    def test_returns_unknown_for_empty(self):
        assert ApplicationStatus.from_string("") == ApplicationStatus.UNKNOWN


# ---------------------------------------------------------------------------
# paginate
# ---------------------------------------------------------------------------

class TestPaginate:
    def test_returns_correct_has_more(self):
        items = list(range(10))
        pagination, page_items = paginate(items, page=1, limit=5)
        assert pagination["has_more"] is True
        assert pagination["total"] == 10
        assert pagination["count"] == 5
        assert pagination["page"] == 1
        assert page_items == [0, 1, 2, 3, 4]

        pagination2, page_items2 = paginate(items, page=2, limit=5)
        assert pagination2["has_more"] is False
        assert pagination2["count"] == 5
        assert page_items2 == [5, 6, 7, 8, 9]

    def test_handles_empty_list(self):
        pagination, page_items = paginate([], page=1, limit=50)
        assert pagination["total"] == 0
        assert pagination["count"] == 0
        assert pagination["page"] == 1
        assert pagination["has_more"] is False
        assert page_items == []

    def test_page_beyond_items(self):
        items = [1, 2, 3]
        pagination, page_items = paginate(items, page=5, limit=10)
        assert pagination["total"] == 3
        assert pagination["count"] == 0
        assert pagination["has_more"] is False
        assert page_items == []


# ---------------------------------------------------------------------------
# Job domain entity
# ---------------------------------------------------------------------------

class TestJobFromApiDict:
    def test_parses_correctly(self):
        """from_api_dict maps Naukri API fields to Job attributes."""
        data = {
            "jobId": "12345",
            "title": "Senior Backend Engineer",
            "companyName": "Acme Corp",
            "salary": "15-25 Lakhs",
            "salary_min_lakhs": 15.0,
            "salary_max_lakhs": 25.0,
            "location": "Bangalore",
            "experience": "3-7 Yrs",
            "experience_min": 3,
            "experience_max": 7,
            "tags": ["Node.js", "TypeScript"],
            "is_applied": False,
            "is_agent_eligible": True,
            "work_mode": "hybrid",
            "company_rating": "4.2",
        }
        job = Job.from_api_dict(data)
        assert job.job_id == "12345"
        assert job.title == "Senior Backend Engineer"
        assert job.company == "Acme Corp"
        assert job.salary_min_lakhs == 15.0
        assert job.salary_max_lakhs == 25.0
        assert job.experience_min == 3
        assert job.experience_max == 7
        assert job.tags == ["Node.js", "TypeScript"]
        assert job.is_agent_eligible is True
        assert job.work_mode == "hybrid"


class TestJobSalaryDisclosed:
    def test_returns_true_when_salary_present(self):
        job = Job(job_id="1", salary_min_lakhs=10.0, salary_max_lakhs=20.0)
        assert job.salary_disclosed is True

    def test_returns_false_when_salary_none(self):
        job = Job(job_id="2", salary_min_lakhs=None)
        assert job.salary_disclosed is False

    def test_returns_false_when_salary_zero(self):
        job = Job(job_id="3", salary_min_lakhs=0.0)
        assert job.salary_disclosed is False


class TestJobMatchesExperience:
    def test_within_range(self):
        job = Job(job_id="1", experience_min=2, experience_max=6)
        assert job.matches_experience(4) is True

    def test_below_range(self):
        job = Job(job_id="1", experience_min=3, experience_max=7)
        assert job.matches_experience(1) is False

    def test_above_range(self):
        job = Job(job_id="1", experience_min=1, experience_max=3)
        assert job.matches_experience(5) is False

    def test_no_range_always_matches(self):
        job = Job(job_id="1")
        assert job.matches_experience(10) is True


# ---------------------------------------------------------------------------
# Application domain entity
# ---------------------------------------------------------------------------

class TestApplicationFromDict:
    def test_parses_with_status_enum(self):
        """from_dict converts status string to ApplicationStatus enum."""
        data = {
            "job_id": "999",
            "title": "Frontend Dev",
            "company": "Widgets Inc",
            "status": "shortlisted",
            "applied_at": "2026-03-01T10:00:00+00:00",
            "source": "smart_apply",
            "ars_score": 85,
            "view_count": 3,
        }
        app = Application.from_dict(data)
        assert app.job_id == "999"
        assert app.title == "Frontend Dev"
        assert app.status == ApplicationStatus.SHORTLISTED
        assert app.source == "smart_apply"
        assert app.ars_score == 85
        assert app.view_count == 3


class TestApplicationIsStale:
    def test_stale_when_old_and_still_applied(self):
        """Application applied >14 days ago with no status change is stale."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        app = Application(
            job_id="1",
            status=ApplicationStatus.APPLIED,
            applied_at=old_date,
        )
        assert app.is_stale is True

    def test_not_stale_when_recent(self):
        recent_date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        app = Application(
            job_id="2",
            status=ApplicationStatus.APPLIED,
            applied_at=recent_date,
        )
        assert app.is_stale is False

    def test_not_stale_when_status_progressed(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        app = Application(
            job_id="3",
            status=ApplicationStatus.SHORTLISTED,
            applied_at=old_date,
        )
        assert app.is_stale is False


class TestApplicationDaysSinceApplied:
    def test_calculates_days(self):
        days_ago = 10
        applied_date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        app = Application(job_id="1", applied_at=applied_date)
        assert app.days_since_applied == days_ago

    def test_returns_zero_for_empty(self):
        app = Application(job_id="2", applied_at="")
        assert app.days_since_applied == 0


class TestApplicationHasRecruiterInterest:
    def test_true_when_views_positive(self):
        app = Application(job_id="1", view_count=2, ars_score=30)
        assert app.has_recruiter_interest is True

    def test_true_when_ars_high(self):
        app = Application(job_id="2", view_count=0, ars_score=70)
        assert app.has_recruiter_interest is True

    def test_false_when_no_signal(self):
        app = Application(job_id="3", view_count=0, ars_score=50)
        assert app.has_recruiter_interest is False

    def test_false_when_none_values(self):
        app = Application(job_id="4")
        assert app.has_recruiter_interest is False


class TestApplicationToDict:
    def test_serializes_status_as_string(self):
        app = Application(
            job_id="42",
            title="Backend Dev",
            company="TestCo",
            status=ApplicationStatus.VIEWED_BY_RECRUITER,
            applied_at="2026-03-15T12:00:00+00:00",
        )
        d = app.to_dict()
        assert d["status"] == "viewed_by_recruiter"
        assert d["job_id"] == "42"
        assert isinstance(d["status"], str)
        # None fields should be excluded
        assert "ars_score" not in d
        assert "view_count" not in d
