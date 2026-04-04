"""Tests for naukri_server.validation — response validators."""

from naukri_server.validation import (
    validate_job_list, validate_company_list, validate_profile,
    validate_job_detail, validate_salary_data, validate_review_data,
)


class TestValidateJobList:
    def test_valid_list(self):
        jobs = [{"job_id": "1", "title": "Dev", "company": "Corp"}]
        warnings = validate_job_list(jobs, 1, "test")
        assert warnings == []

    def test_empty_with_total(self):
        warnings = validate_job_list([], 5, "test")
        assert any("empty" in w for w in warnings)

    def test_missing_fields(self):
        jobs = [{"job_id": "1"}]
        warnings = validate_job_list(jobs, 1, "test")
        assert any("title" in w for w in warnings)


class TestValidateProfile:
    def test_valid_profile(self):
        profile = {"name": "John", "total_experience": "5 years", "current_location": "Bangalore",
                    "skills_with_experience": [{"skill": "Python"}], "employment": [{}]}
        assert validate_profile(profile) == []

    def test_missing_critical(self):
        warnings = validate_profile({"skills_with_experience": [], "employment": []})
        assert any("critical" in w.lower() for w in warnings)


class TestValidateJobDetail:
    def test_valid_detail(self):
        assert validate_job_detail({"title": "Dev", "company": "Corp", "description": "..."}) == []

    def test_missing_fields(self):
        warnings = validate_job_detail({})
        assert len(warnings) > 0


class TestValidateSalaryData:
    def test_valid_salary(self):
        data = {"avg_salary": 1500000, "salaries": [{"designation": "SDE", "salary": 1500000}]}
        assert validate_salary_data(data) == []

    def test_empty_data(self):
        warnings = validate_salary_data({})
        assert any("missing" in w for w in warnings)


class TestValidateReviewData:
    def test_valid_reviews(self):
        data = {"review_count": 1, "reviews": [{"title": "Good", "likes": "Culture"}]}
        assert validate_review_data(data) == []

    def test_count_mismatch(self):
        warnings = validate_review_data({"review_count": 5, "reviews": []})
        assert any("empty" in w for w in warnings)


# =====================================================================
# From test_consolidation.py — cross-cutting helper-level validation
# =====================================================================

import pytest
from unittest.mock import AsyncMock, patch


class TestConsolidationHelperValidation:
    """Validation inside internal helpers that runs before any API/browser call."""

    @pytest.mark.asyncio
    async def test_application_insights_negative_days(self):
        """_application_insights rejects days < 1."""
        from naukri_server.tools.insights import _application_insights
        result = await _application_insights(days=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "days" in result["message"]

    @pytest.mark.asyncio
    async def test_set_reminder_invalid_days_zero(self):
        """_set_reminder rejects days < 1."""
        from naukri_server.tools.reminders import _set_reminder
        result = await _set_reminder(job_id="123", days=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "days" in result["message"]

    @pytest.mark.asyncio
    async def test_set_reminder_invalid_days_over_365(self):
        """_set_reminder rejects days > 365."""
        from naukri_server.tools.reminders import _set_reminder
        result = await _set_reminder(job_id="123", days=400)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_fetch_inbox_page_clamped(self):
        """page=0 is silently clamped to 1 by validate_page."""
        from naukri_server.tools.inbox import _fetch_inbox
        with patch("naukri_server.tools.inbox.api_client.post", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"successResponse": {"inbox": [], "total": 0, "unread": 0}}
            result = await _fetch_inbox(page=0)
            mock_api.assert_awaited()
