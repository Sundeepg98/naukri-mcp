"""Edge case tests — validation helpers, empty responses, salary parsing, error codes, job parsing, action dispatchers."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


# ============================================================================
# 1. Validation helper tests (from naukri_server/validation.py)
# ============================================================================


class TestValidateLimit:
    def test_normal_value(self):
        from naukri_server.validation import validate_limit
        assert validate_limit(25) == 25

    def test_zero_clamps_to_one(self):
        from naukri_server.validation import validate_limit
        assert validate_limit(0) == 1

    def test_negative_clamps_to_one(self):
        from naukri_server.validation import validate_limit
        assert validate_limit(-5) == 1

    def test_exceeds_default_max(self):
        from naukri_server.validation import validate_limit
        assert validate_limit(100) == 50

    def test_custom_max(self):
        from naukri_server.validation import validate_limit
        assert validate_limit(30, max_allowed=20) == 20

    def test_exact_max_boundary(self):
        from naukri_server.validation import validate_limit
        assert validate_limit(50) == 50

    def test_one_is_valid(self):
        from naukri_server.validation import validate_limit
        assert validate_limit(1) == 1


class TestValidatePage:
    def test_normal_value(self):
        from naukri_server.validation import validate_page
        assert validate_page(3) == 3

    def test_zero_clamps_to_one(self):
        from naukri_server.validation import validate_page
        assert validate_page(0) == 1

    def test_negative_clamps_to_one(self):
        from naukri_server.validation import validate_page
        assert validate_page(-10) == 1

    def test_large_page(self):
        from naukri_server.validation import validate_page
        assert validate_page(9999) == 9999


class TestValidateEnum:
    def test_exact_match(self):
        from naukri_server.validation import validate_enum
        assert validate_enum("json", ["json", "csv"]) == "json"

    def test_case_insensitive_match(self):
        from naukri_server.validation import validate_enum
        assert validate_enum("JSON", ["json", "csv"]) == "json"

    def test_no_match_returns_none(self):
        from naukri_server.validation import validate_enum
        assert validate_enum("xml", ["json", "csv"]) is None

    def test_mixed_case_in_allowed(self):
        from naukri_server.validation import validate_enum
        assert validate_enum("fulltime", ["FullTime", "PartTime"]) == "FullTime"


# ============================================================================
# 2. Empty/malformed response tests
# ============================================================================


class TestEmptyResponses:
    @pytest.mark.asyncio
    async def test_search_no_results_rest_fallback_to_browser_empty(self):
        """When REST returns no jobDetails and browser also fails, error is returned."""
        from naukri_server.tools.search import naukri_search_jobs
        with patch("naukri_server.tools.search.api_client.get", new_callable=AsyncMock) as mock_api, \
             patch("naukri_server.tools.search.browser") as mock_browser:
            # REST returns empty (no jobDetails key triggers fallback)
            mock_api.return_value = {"noOfJobs": 0, "jobDetails": []}
            # Browser path - mock page_pool.acquire
            mock_page = AsyncMock()
            mock_browser.page_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_page)
            mock_browser.page_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("naukri_server.tools.search.page_intercept_json", new_callable=AsyncMock, return_value=None):
                result = await naukri_search_jobs(keywords="nonexistent_xyz_12345")
                assert result["status"] == "error"
                assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    async def test_applications_list_empty(self):
        from naukri_server.tools.tracking import naukri_applications
        with patch("naukri_server.database.list_applications", new_callable=AsyncMock,
                    return_value=([], 0)), \
             patch("naukri_server.database.count_applications_by_status", new_callable=AsyncMock,
                    return_value={}):
            result = await naukri_applications(action="list")
            assert result["status"] == "success"
            assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_saved_jobs_list_empty(self):
        from naukri_server.tools.tracking import naukri_saved_jobs
        with patch("naukri_server.database.list_saved_jobs", new_callable=AsyncMock,
                    return_value=([], 0)):
            result = await naukri_saved_jobs(action="list")
            assert result["status"] == "success"
            assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_insights_no_applications(self):
        from naukri_server.tools.insights import naukri_insights
        with patch("naukri_server.database.list_all_applications", new_callable=AsyncMock, return_value=[]):
            result = await naukri_insights(insight_type="applications")
            assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_insights_salary_no_applications(self):
        from naukri_server.tools.insights import naukri_insights
        with patch("naukri_server.database.list_all_applications", new_callable=AsyncMock, return_value=[]):
            result = await naukri_insights(insight_type="salary")
            assert result["status"] == "error"


# ============================================================================
# 3. Salary parsing edge cases
# ============================================================================


class TestSalaryParsingEdge:
    def test_not_disclosed(self):
        from naukri_server.tools.insights import _parse_salary_str
        assert _parse_salary_str("Not Disclosed") == (None, None)

    def test_confidential(self):
        from naukri_server.tools.insights import _parse_salary_str
        assert _parse_salary_str("Confidential") == (None, None)

    def test_none_input(self):
        from naukri_server.tools.insights import _parse_salary_str
        assert _parse_salary_str(None) == (None, None)

    def test_empty_string(self):
        from naukri_server.tools.insights import _parse_salary_str
        assert _parse_salary_str("") == (None, None)

    def test_numeric_range(self):
        from naukri_server.tools.insights import _parse_salary_str
        min_s, max_s = _parse_salary_str("10-15 Lacs PA")
        assert min_s == 10.0
        assert max_s == 15.0

    def test_single_value(self):
        from naukri_server.tools.insights import _parse_salary_str
        min_s, max_s = _parse_salary_str("12 LPA")
        # Single value returns (val, val) per implementation
        assert min_s == 12.0
        assert max_s == 12.0

    def test_high_values_converted_to_lakhs(self):
        from naukri_server.tools.insights import _parse_salary_str
        min_s, max_s = _parse_salary_str("1000000-1500000")
        # Values > 200 get divided by LAKHS_MULTIPLIER (100000)
        assert min_s == 10.0
        assert max_s == 15.0

    def test_non_string_input(self):
        from naukri_server.tools.insights import _parse_salary_str
        assert _parse_salary_str(12345) == (None, None)

    def test_no_numbers_in_string(self):
        from naukri_server.tools.insights import _parse_salary_str
        assert _parse_salary_str("Competitive salary") == (None, None)


# ============================================================================
# 4. Error code presence tests
# ============================================================================


class TestErrorCodePresence:
    @pytest.mark.asyncio
    async def test_applications_unknown_action(self):
        from naukri_server.tools.tracking import naukri_applications
        result = await naukri_applications(action="invalid_xyz")
        assert result["status"] == "error"
        assert "error_code" in result
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_jobs_unknown_action(self):
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="invalid_xyz")
        assert result["status"] == "error"
        assert "error_code" in result
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_insights_unknown_type(self):
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="invalid_xyz")
        assert result["status"] == "error"
        assert "error_code" in result
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_sync_unknown_entity(self):
        from naukri_server.tools.sync import naukri_sync
        result = await naukri_sync(entity="invalid_xyz")
        assert result["status"] == "error"
        assert "error_code" in result
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_saved_jobs_unknown_action(self):
        from naukri_server.tools.tracking import naukri_saved_jobs
        result = await naukri_saved_jobs(action="invalid_xyz")
        assert result["status"] == "error"
        assert "error_code" in result
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_performance_unknown_metric(self):
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="invalid_xyz")
        assert result["status"] == "error"
        assert "error_code" in result
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_jobs_get_missing_job_id(self):
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="get")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_applications_detail_missing_id(self):
        from naukri_server.tools.tracking import naukri_applications
        result = await naukri_applications(action="detail")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_saved_jobs_save_missing_id(self):
        from naukri_server.tools.tracking import naukri_saved_jobs
        result = await naukri_saved_jobs(action="save")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_saved_jobs_unsave_missing_id(self):
        from naukri_server.tools.tracking import naukri_saved_jobs
        result = await naukri_saved_jobs(action="unsave")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"


# ============================================================================
# 5. Job parsing edge cases
# ============================================================================


class TestJobParsingEdgeCases:
    def test_parse_job_list_empty_input(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        assert _parse_job_list([], 10) == []

    def test_parse_job_list_missing_fields(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        result = _parse_job_list([{"jobId": "123"}], 10)
        assert len(result) == 1
        assert result[0]["job_id"] == "123"
        assert result[0]["title"] is None
        assert result[0]["company"] is None

    def test_parse_job_list_respects_limit(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        items = [{"jobId": str(i), "title": f"Job {i}"} for i in range(10)]
        result = _parse_job_list(items, 3)
        assert len(result) == 3

    def test_extract_job_id_from_url(self):
        from naukri_server.tools.jobs import _extract_job_id
        assert _extract_job_id("https://www.naukri.com/job-listing-python-developer-123456789") == "123456789"

    def test_extract_job_id_numeric(self):
        from naukri_server.tools.jobs import _extract_job_id
        assert _extract_job_id("123456789") == "123456789"

    def test_extract_job_id_invalid_raises(self):
        from naukri_server.tools.jobs import _extract_job_id
        with pytest.raises(ValueError):
            _extract_job_id("no-numbers-here")

    def test_extract_skills_dict_format(self):
        from naukri_server.tools.jobs import _extract_skills
        raw = {"preferred": [{"label": "Python"}, {"label": "Django"}], "other": [{"label": "SQL"}]}
        result = _extract_skills(raw)
        assert "Python" in result
        assert "Django" in result
        assert "SQL" in result

    def test_extract_skills_list_format(self):
        from naukri_server.tools.jobs import _extract_skills
        raw = [{"label": "React"}, {"label": "Node.js"}]
        result = _extract_skills(raw)
        assert "React" in result
        assert "Node.js" in result

    def test_extract_skills_empty(self):
        from naukri_server.tools.jobs import _extract_skills
        assert _extract_skills([]) == []
        assert _extract_skills({}) == []
        assert _extract_skills(None) == []


# ============================================================================
# 6. Action dispatcher validation tests
# ============================================================================


class TestActionDispatchers:
    @pytest.mark.asyncio
    async def test_applications_apply_needs_job_id(self):
        from naukri_server.tools.tracking import naukri_applications
        result = await naukri_applications(action="apply")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_applications_batch_apply_needs_keywords(self):
        from naukri_server.tools.tracking import naukri_applications
        result = await naukri_applications(action="batch_apply")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_applications_purge_needs_before_date(self):
        from naukri_server.tools.tracking import naukri_applications
        result = await naukri_applications(action="purge")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_jobs_report_fraud_needs_job_id(self):
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="report_fraud")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_jobs_report_fraud_needs_reason(self):
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="report_fraud", job_id="123456")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "reason" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_performance_impressions_invalid_days(self):
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="impressions", days=5)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_insights_cached_answers_update_needs_key(self):
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="cached_answers", action="update")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_insights_cached_answers_delete_needs_key(self):
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="cached_answers", action="delete")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_insights_application_invalid_days(self):
        from naukri_server.tools.insights import _application_insights
        result = await _application_insights(days=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
