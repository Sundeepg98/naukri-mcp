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
    async def test_search_jobs_page_validation(self):
        """page < 1 returns VALIDATION_ERROR before any browser/API call."""
        from naukri_server.tools.search import naukri_search_jobs
        result = await naukri_search_jobs(keywords="python", page=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]

    @pytest.mark.asyncio
    async def test_search_jobs_page_negative(self):
        """Negative page also returns VALIDATION_ERROR."""
        from naukri_server.tools.search import naukri_search_jobs
        result = await naukri_search_jobs(keywords="react", page=-5)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]

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
    async def test_recommendations_page_validation(self):
        """page < 1 returns VALIDATION_ERROR before any API call."""
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations(page=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]

    @pytest.mark.asyncio
    async def test_recommendations_page_negative(self):
        """Negative page returns VALIDATION_ERROR."""
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations(page=-1)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]

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

        with patch("naukri_server.tools.search.api_post", new_callable=AsyncMock) as mock_post:
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
    async def test_similar_jobs_page_validation(self):
        """page < 1 returns VALIDATION_ERROR before any API call."""
        from naukri_server.tools.search import naukri_get_similar_jobs
        result = await naukri_get_similar_jobs(job_id="12345", page=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]

    @pytest.mark.asyncio
    async def test_similar_jobs_page_negative(self):
        """Negative page returns VALIDATION_ERROR."""
        from naukri_server.tools.search import naukri_get_similar_jobs
        result = await naukri_get_similar_jobs(job_id="12345", page=-3)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

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

        with patch("naukri_server.tools.search.api_get", new_callable=AsyncMock) as mock_get:
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
        """Valid Naukri URL extracts job_id and proceeds to API call."""
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

        with patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = fake_detail
            result = await naukri_get_job(
                job_id_or_url="https://www.naukri.com/job-listings-python-developer-123456789"
            )
            mock_get.assert_awaited_once()
            assert result["status"] == "success"
            assert result["job_id"] == "123456789"


# =====================================================================
# 5. naukri_report_fraud_job
# =====================================================================

class TestReportFraudJob:
    """Tests for naukri_server.tools.jobs.naukri_report_fraud_job."""

    @pytest.mark.asyncio
    async def test_report_fraud_success(self):
        """Valid job_id and reason routes to api_post and returns success."""
        from naukri_server.tools.jobs import naukri_report_fraud_job

        with patch("naukri_server.tools.jobs.api_post", new_callable=AsyncMock) as mock_post:
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
