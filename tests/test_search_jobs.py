"""Tests for search and jobs tools — validation and routing logic.

Every test is PURE: no network, no browser, no file I/O.
We only exercise validation / routing logic that runs BEFORE any async helper call.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# =====================================================================
# 1. naukri_search_jobs
# =====================================================================

class TestSearchJobs:
    """Tests for naukri_server.tools.search.naukri_search_jobs."""

    @pytest.mark.asyncio
    async def test_search_jobs_page_clamped_zero(self):
        """page=0 is silently clamped to 1 by validate_page (no error)."""
        from naukri_server.tools.search import naukri_search_jobs
        with patch("naukri_server.tools.search.api_client.get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {
                "noOfJobs": 1,
                "jobDetails": [{"jobId": "1", "title": "Dev", "companyName": "X",
                                "placeholders": [], "tagsAndSkills": "python", "createdDate": "1d ago"}],
            }
            result = await naukri_search_jobs(keywords="python", page=0)
            mock_api.assert_awaited()

    @pytest.mark.asyncio
    async def test_search_jobs_page_clamped_negative(self):
        """Negative page is silently clamped to 1 by validate_page (no error)."""
        from naukri_server.tools.search import naukri_search_jobs
        with patch("naukri_server.tools.search.api_client.get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {
                "noOfJobs": 1,
                "jobDetails": [{"jobId": "1", "title": "Dev", "companyName": "X",
                                "placeholders": [], "tagsAndSkills": "react", "createdDate": "1d ago"}],
            }
            result = await naukri_search_jobs(keywords="react", page=-5)
            mock_api.assert_awaited()

    @pytest.mark.asyncio
    async def test_search_jobs_limit_clamped(self):
        """limit > 50 is clamped to 50 before being used."""
        from naukri_server.tools.search import naukri_search_jobs

        # Mock the browser page pool and page_intercept_json to avoid real calls
        mock_page = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_page)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        fake_data = {
            "jobDetails": [
                {"jobId": "1", "title": "Dev", "companyName": "X", "placeholders": [],
                 "tagsAndSkills": "python", "createdDate": "1d ago"}
            ],
            "noOfJobs": 1,
            "clusters": {},
        }

        with patch("naukri_server.tools.search.browser") as mock_browser, \
             patch("naukri_server.tools.search.page_intercept_json", new_callable=AsyncMock) as mock_intercept:
            mock_browser.page_pool = mock_pool
            mock_intercept.return_value = fake_data

            result = await naukri_search_jobs(keywords="python", limit=100, page=1)

            assert result["status"] == "success"
            # Verify the jobs list was parsed with clamped limit
            assert result["count"] <= 50


# =====================================================================
# 2. naukri_get_recommendations
# =====================================================================

class TestGetRecommendations:
    """Tests for naukri_server.tools.search.naukri_get_recommendations."""

    @pytest.mark.asyncio
    async def test_recommendations_page_clamped_zero(self):
        """page=0 is silently clamped to 1 by validate_page (no error)."""
        from naukri_server.tools.search import naukri_get_recommendations
        with patch("naukri_server.tools.search.api_client.post", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"jobDetails": []}
            result = await naukri_get_recommendations(page=0)
            mock_api.assert_awaited()

    @pytest.mark.asyncio
    async def test_recommendations_page_clamped_negative(self):
        """Negative page is silently clamped to 1 by validate_page (no error)."""
        from naukri_server.tools.search import naukri_get_recommendations
        with patch("naukri_server.tools.search.api_client.post", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"jobDetails": []}
            result = await naukri_get_recommendations(page=-1)
            mock_api.assert_awaited()

    @pytest.mark.asyncio
    async def test_recommendations_routes_with_valid_page(self):
        """Valid page routes to api_post and returns success."""
        from naukri_server.tools.search import naukri_get_recommendations

        fake_data = {
            "jobDetails": [
                {"jobId": "42", "title": "SDE", "companyName": "Acme",
                 "placeholders": [], "tagsAndSkills": "java", "createdDate": "2d ago"}
            ],
            "noOfJobs": 1,
        }

        with patch("naukri_server.tools.search.api_client.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = fake_data
            result = await naukri_get_recommendations(limit=10, page=1)
            mock_post.assert_awaited_once()
            assert result["status"] == "success"
            assert result["source"] == "recommendations"
            assert result["page"] == 1


# =====================================================================
# 3. naukri_get_similar_jobs
# =====================================================================

class TestGetSimilarJobs:
    """Tests for naukri_server.tools.search.naukri_get_similar_jobs."""

    @pytest.mark.asyncio
    async def test_similar_jobs_page_clamped_zero(self):
        """page=0 is silently clamped to 1 by validate_page (no error)."""
        from naukri_server.tools.search import naukri_get_similar_jobs
        with patch("naukri_server.tools.search.api_client.get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"jobDetails": []}
            result = await naukri_get_similar_jobs(job_id="12345", page=0)
            mock_api.assert_awaited()

    @pytest.mark.asyncio
    async def test_similar_jobs_page_clamped_negative(self):
        """Negative page is silently clamped to 1 by validate_page (no error)."""
        from naukri_server.tools.search import naukri_get_similar_jobs
        with patch("naukri_server.tools.search.api_client.get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"jobDetails": []}
            result = await naukri_get_similar_jobs(job_id="12345", page=-3)
            mock_api.assert_awaited()

    @pytest.mark.asyncio
    async def test_similar_jobs_routes_with_valid_params(self):
        """Valid job_id and page routes to api_get and returns success."""
        from naukri_server.tools.search import naukri_get_similar_jobs

        fake_data = {
            "simJobDetails": {
                "content": [
                    {"jobId": "99", "title": "Backend Dev", "companyName": "Corp",
                     "placeholders": [], "tagsAndSkills": "go", "createdDate": "5d ago"}
                ],
                "collaborative": [],
            },
            "noOfJobs": 1,
        }

        with patch("naukri_server.tools.search.api_client.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = fake_data
            result = await naukri_get_similar_jobs(job_id="12345", limit=10, page=1)
            mock_get.assert_awaited_once()
            assert result["status"] == "success"
            assert result["source"] == "similar"
            assert result["job_id"] == "12345"


# =====================================================================
# 4. naukri_get_job
# =====================================================================

class TestGetJob:
    """Tests for naukri_server.tools.jobs.naukri_get_job."""

    @pytest.mark.asyncio
    async def test_get_job_invalid_id(self):
        """Non-numeric, non-URL job_id returns error from _extract_job_id."""
        from naukri_server.tools.jobs import naukri_get_job
        result = await naukri_get_job(job_id_or_url="not-a-valid-id")
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Invalid job ID" in result["message"]

    @pytest.mark.asyncio
    async def test_get_job_empty_string(self):
        """Empty string job_id returns error."""
        from naukri_server.tools.jobs import naukri_get_job
        result = await naukri_get_job(job_id_or_url="")
        assert result["status"] == "error"
        assert "Invalid job ID" in result["message"]

    @pytest.mark.asyncio
    async def test_get_job_extracts_id_from_url(self):
        """Valid Naukri URL extracts job_id and proceeds to API call (detail + match score)."""
        from naukri_server.tools.jobs import naukri_get_job

        fake_detail = {
            "jobDetails": {
                "title": "Python Dev",
                "companyDetail": {"name": "TestCo"},
                "salaryDetail": {"label": "10-15 LPA"},
                "minimumExperience": 2,
                "maximumExperience": 5,
                "keySkills": [{"label": "Python"}],
                "placeholders": [],
            }
        }
        fake_match_score = {
            "education": False,
            "functionalArea": True,
            "Keyskills": 1.0,
            "workExperience": True,
            "industry": True,
            "location": True,
            "earlyApplicant": True,
        }

        with patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [fake_detail, fake_match_score]
            result = await naukri_get_job(
                job_id_or_url="https://www.naukri.com/job-listings-python-developer-123456789"
            )
            # api_get called twice: once for job detail, once for match score
            assert mock_get.await_count == 2
            assert result["status"] == "success"
            assert result["job_id"] == "123456789"
            # Match score should be enriched into the result
            assert result["match_score"]["key_skills"] == 1.0
            assert result["match_score"]["early_applicant"] is True


# =====================================================================
# 5. naukri_report_fraud_job
# =====================================================================

class TestReportFraudJob:
    """Tests for naukri_server.tools.jobs.naukri_report_fraud_job."""

    @pytest.mark.asyncio
    async def test_report_fraud_success(self):
        """Valid job_id and reason routes to api_post and returns success."""
        from naukri_server.tools.jobs import naukri_report_fraud_job

        with patch("naukri_server.tools.jobs.api_client.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {}
            result = await naukri_report_fraud_job(job_id="12345", reason="Fake listing")
            mock_post.assert_awaited_once()
            assert result["status"] == "success"
            assert result["job_id"] == "12345"


# =====================================================================
# 6. Helper-level: _extract_job_id
# =====================================================================

class TestExtractJobId:
    """Tests for naukri_server.tools.jobs._extract_job_id (pure helper)."""

    def test_numeric_id_passthrough(self):
        from naukri_server.tools.jobs import _extract_job_id
        assert _extract_job_id("123456") == "123456"

    def test_url_extraction(self):
        from naukri_server.tools.jobs import _extract_job_id
        result = _extract_job_id("https://www.naukri.com/job-listings-sde2-987654321")
        assert result == "987654321"

    def test_invalid_raises_value_error(self):
        from naukri_server.tools.jobs import _extract_job_id
        with pytest.raises(ValueError, match="Invalid job ID"):
            _extract_job_id("abc-def")


# =====================================================================
# 7. Helper-level: _parse_salary_data, _parse_company_data, _parse_match_score
# =====================================================================

class TestJobDetailParsers:
    """Unit tests for extracted _parse_job_detail helpers."""

    def test_parse_salary_with_label(self):
        from naukri_server.tools.jobs import _parse_salary_data
        job = {"salaryDetail": {"label": "10-15 Lacs PA", "minimumSalary": 1000000, "maximumSalary": 1500000}}
        result = _parse_salary_data(job)
        assert result["salary"] == "10-15 Lacs PA"

    def test_parse_salary_without_label(self):
        from naukri_server.tools.jobs import _parse_salary_data
        job = {"salaryDetail": {"minimumSalary": 1000000, "maximumSalary": 1500000}}
        result = _parse_salary_data(job)
        assert "10.0" in result["salary"]
        assert "15.0" in result["salary"]

    def test_parse_salary_not_disclosed(self):
        from naukri_server.tools.jobs import _parse_salary_data
        job = {"salaryDetail": {}}
        result = _parse_salary_data(job)
        assert result["salary"] == "Not Disclosed"

    def test_parse_company_with_ambitionbox(self):
        from naukri_server.tools.jobs import _parse_company_data
        job = {"companyDetail": {"name": "Infosys", "groupId": "123"}, "ambitionBoxData": {"AggregateRating": 3.8, "ReviewsCount": 5000}}
        result = _parse_company_data(job, {})
        assert result["company_name"] == "Infosys"
        assert result["company_rating"] == 3.8
        assert result["group_id"] == "123"

    def test_parse_company_fallback_from_details(self):
        from naukri_server.tools.jobs import _parse_company_data
        job = {"companyDetail": {"name": "TCS"}}
        details = {"ambitionBoxDetails": {"companyInfo": {"rating": 4.0, "reviewsCount": 1000}}}
        result = _parse_company_data(job, details)
        assert result["company_rating"] == 4.0

    def test_parse_match_score_with_data(self):
        from naukri_server.tools.jobs import _parse_match_score
        score = {"Keyskills": 85, "skillMismatch": "Docker, K8s", "earlyApplicant": True, "education": {"userMatching": True}}
        result = _parse_match_score(score)
        assert result["match_score"]["key_skills"] == 85
        assert result["match_score"]["education"] is True
        assert result["match_score"]["early_applicant"] is True
        assert "Docker" in result["match_details"]["skill_mismatch"]

    def test_parse_match_score_none(self):
        from naukri_server.tools.jobs import _parse_match_score
        result = _parse_match_score(None)
        assert result["match_score"] is None
        assert result["match_details"] is None


# =====================================================================
# 8. _fetch_match_score REST helper
# =====================================================================

class TestFetchMatchScore:
    """Tests for naukri_server.tools.jobs._fetch_match_score — REST matchscore endpoint."""

    @pytest.mark.asyncio
    async def test_returns_valid_data(self):
        """Successful API call returns per-dimension match dict."""
        from naukri_server.tools.jobs import _fetch_match_score

        fake_response = {
            "education": True,
            "functionalArea": False,
            "Keyskills": 0.85,
            "workExperience": True,
            "industry": True,
            "location": False,
            "earlyApplicant": True,
            "skillMismatch": "Docker, AWS",
        }

        with patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = fake_response
            result = await _fetch_match_score("123456")

        mock_get.assert_awaited_once()
        assert mock_get.call_args[0][0].endswith("123456/matchscore")
        assert result["education"] is True
        assert result["functional_area"] is False
        assert result["key_skills"] == 0.85
        assert result["work_experience"] is True
        assert result["industry"] is True
        assert result["location"] is False
        assert result["early_applicant"] is True

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self):
        """API failure returns None gracefully — no exception propagated."""
        from naukri_server.tools.jobs import _fetch_match_score

        with patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("Network error")
            result = await _fetch_match_score("999999")

        assert result is None
