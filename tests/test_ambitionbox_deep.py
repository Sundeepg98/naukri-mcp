"""Deep unit tests for AmbitionBox company intel tools.

Tests _fetch_salary, _fetch_reviews, _fetch_interviews, _extract_next_data,
_scrape_salary_table fallback, naukri_company_intel routing,
_extract_company_id, _enrich_with_rest, and REST enrichment integration.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Mock data matching real AmbitionBox __NEXT_DATA__ shapes
# ---------------------------------------------------------------------------

SALARY_PAGE_PROPS = {
    "companyData": {"CompanyName": "Google"},
    "salaryData": {
        "data": {
            "summaryData": {
                "totalSalaryAverage": 2500000,
                "minCtc": 800000,
                "maxCtc": 5000000,
                "totalSalaryDataPoints": 150,
                "percentiles": {"p25": 1200000, "p50": 2500000, "p75": 4000000},
            },
        }
    },
    "latestSalaries": {
        "latestSalaries": [
            {"jobProfileName": "Software Engineer", "ctc": 2000000, "experience": "3 years"},
            {"jobProfileName": "Senior Engineer", "ctc": 3500000, "experience": "7 years"},
        ]
    },
}

REVIEWS_PAGE_PROPS = {
    "companyName": "Google",
    "ratingsData": {
        "overallCompanyRating": 4.2,
        "workSatisfactionRating": 4.5,
        "careerGrowthRating": 4.0,
    },
    "fixedReviewCount": 500,
    "ratingDistribution": {"5": 200, "4": 150, "3": 80, "2": 40, "1": 30},
    "reviewsData": [
        {
            "reviewTitle": "Great workplace",
            "overallCompanyRating": 4.5,
            "jobProfile": {"name": "Software Engineer"},
            "jobLocation": {"name": "Bangalore"},
            "likesText": "Good culture",
            "disLikesText": "Long hours",
            "currentJob": True,
            "verified": True,
        }
    ],
}

INTERVIEWS_PAGE_PROPS = {
    "companyData": {"CompanyName": "Google"},
    "interviewOverview": {
        "totalInterviews": 100,
        "difficultyPercentage": {"easy": 20, "moderate": 50, "hard": 30},
    },
    "interviewReviews": [
        {
            "jobProfile": {"name": "SDE-2"},
            "difficultyLevel": "Moderate",
            "offerStatus": "Selected",
            "experienceType": "Campus",
            "interviewQuestions": ["Tell me about yourself", "System design question"],
            "created": "2024-01-15",
            "likesText": "Well structured",
        }
    ],
}


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_page():
    """A mock Playwright page with common async methods."""
    page = AsyncMock()
    page.title = AsyncMock(return_value="Some Page Title")
    page.evaluate = AsyncMock()
    page.wait_for_selector = AsyncMock()
    return page


@pytest.fixture
def mock_pool(mock_page):
    """A mock PagePool whose acquire() yields mock_page."""
    pool = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_page)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool


def _next_data_wrapper(page_props: dict) -> dict:
    """Wrap pageProps in the __NEXT_DATA__ envelope."""
    return {"props": {"pageProps": page_props}}


# =====================================================================
# _fetch_salary tests
# =====================================================================


class TestFetchSalary:
    """Tests for _fetch_salary with mocked browser."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_salary_success(self, mock_browser, mock_goto, mock_pool, mock_page):
        """Full salary data with __NEXT_DATA__ returns success with all fields."""
        mock_browser.page_pool = mock_pool
        mock_page.evaluate.return_value = _next_data_wrapper(SALARY_PAGE_PROPS)

        from naukri_server.tools.ambitionbox import _fetch_salary

        result = await _fetch_salary("google")

        assert result["status"] == "success"
        assert result["company"] == "Google"
        assert result["avg_salary"] == 2500000
        assert result["min_salary"] == 800000
        assert result["max_salary"] == 5000000
        assert result["total_salaries_reported"] == 150
        assert result["percentiles"] == {"p25": 1200000, "p50": 2500000, "p75": 4000000}
        assert len(result["salaries"]) == 2
        assert result["salaries"][0]["designation"] == "Software Engineer"
        assert result["salaries"][1]["ctc"] == 3500000

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_salary_with_designation(self, mock_browser, mock_goto, mock_pool, mock_page):
        """Designation param is included in the URL and result."""
        mock_browser.page_pool = mock_pool
        mock_page.evaluate.return_value = _next_data_wrapper(SALARY_PAGE_PROPS)

        from naukri_server.tools.ambitionbox import _fetch_salary

        result = await _fetch_salary("google", designation="software-engineer")

        assert result["status"] == "success"
        assert result["designation"] == "software-engineer"
        # Verify URL passed to page_goto includes the designation
        call_args = mock_goto.call_args
        url_arg = call_args[0][1]  # second positional arg is URL
        assert "software-engineer" in url_arg

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_salary_404(self, mock_browser, mock_goto, mock_pool, mock_page):
        """Page title containing '404' returns NOT_FOUND error."""
        mock_browser.page_pool = mock_pool
        mock_page.title = AsyncMock(return_value="404 - Page Not Found")

        from naukri_server.tools.ambitionbox import _fetch_salary

        result = await _fetch_salary("nonexistent-company")

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._scrape_salary_table", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._extract_next_data", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_salary_no_next_data_fallback(
        self, mock_browser, mock_goto, mock_extract, mock_scrape, mock_pool, mock_page
    ):
        """When __NEXT_DATA__ is absent, falls back to _scrape_salary_table."""
        mock_browser.page_pool = mock_pool
        mock_extract.return_value = None
        mock_scrape.return_value = [
            {"designation": "SDE-1", "experience_info": "2-4 exp.", "avg_salary": "12 L/yr", "salary_range": "8-16 L/yr"},
        ]
        mock_page.title = AsyncMock(return_value="Google Salaries")

        from naukri_server.tools.ambitionbox import _fetch_salary

        result = await _fetch_salary("google")

        assert result["status"] == "success"
        assert result["source"] == "dom_scrape"
        assert len(result["salaries"]) == 1
        assert result["salaries"][0]["designation"] == "SDE-1"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_salary_empty_page_props(self, mock_browser, mock_goto, mock_pool, mock_page):
        """__NEXT_DATA__ present but pageProps is empty returns BROWSER_ERROR."""
        mock_browser.page_pool = mock_pool
        # __NEXT_DATA__ exists but pageProps is empty dict
        mock_page.evaluate.return_value = {"props": {"pageProps": {}}}

        from naukri_server.tools.ambitionbox import _fetch_salary

        result = await _fetch_salary("google")

        assert result["status"] == "error"
        assert result["error_code"] == "BROWSER_ERROR"
        assert "pageProps is empty" in result["message"]


# =====================================================================
# _fetch_reviews tests
# =====================================================================


class TestFetchReviews:
    """Tests for _fetch_reviews with mocked browser."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_reviews_success(self, mock_browser, mock_goto, mock_pool, mock_page):
        """Full review data with __NEXT_DATA__ returns success with all fields."""
        mock_browser.page_pool = mock_pool
        mock_page.evaluate.return_value = _next_data_wrapper(REVIEWS_PAGE_PROPS)

        from naukri_server.tools.ambitionbox import _fetch_reviews

        result = await _fetch_reviews("google")

        assert result["status"] == "success"
        assert result["company"] == "Google"
        assert result["overall_rating"] == 4.2
        assert result["review_count"] == 500
        assert len(result["reviews"]) == 1

        review = result["reviews"][0]
        assert review["title"] == "Great workplace"
        assert review["rating"] == 4.5
        assert review["designation"] == "Software Engineer"
        assert review["location"] == "Bangalore"
        assert review["likes"] == "Good culture"
        assert review["dislikes"] == "Long hours"
        assert review["is_current_employee"] is True
        assert review["verified"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_reviews_category_ratings(self, mock_browser, mock_goto, mock_pool, mock_page):
        """Category ratings are extracted from ratingsData into human-readable keys."""
        mock_browser.page_pool = mock_pool
        mock_page.evaluate.return_value = _next_data_wrapper(REVIEWS_PAGE_PROPS)

        from naukri_server.tools.ambitionbox import _fetch_reviews

        result = await _fetch_reviews("google")

        assert "category_ratings" in result
        cats = result["category_ratings"]
        assert cats["Work Satisfaction"] == 4.5
        assert cats["Career Growth"] == 4.0

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_reviews_rating_distribution(self, mock_browser, mock_goto, mock_pool, mock_page):
        """Rating distribution dict is included when present in pageProps."""
        mock_browser.page_pool = mock_pool
        mock_page.evaluate.return_value = _next_data_wrapper(REVIEWS_PAGE_PROPS)

        from naukri_server.tools.ambitionbox import _fetch_reviews

        result = await _fetch_reviews("google")

        assert "rating_distribution" in result
        dist = result["rating_distribution"]
        assert dist["5"] == 200
        assert dist["1"] == 30

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_reviews_404(self, mock_browser, mock_goto, mock_pool, mock_page):
        """Page title containing '404' returns NOT_FOUND error."""
        mock_browser.page_pool = mock_pool
        mock_page.title = AsyncMock(return_value="404 Error Page")

        from naukri_server.tools.ambitionbox import _fetch_reviews

        result = await _fetch_reviews("nonexistent")

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._extract_next_data", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_reviews_no_next_data(self, mock_browser, mock_goto, mock_extract, mock_pool, mock_page):
        """When __NEXT_DATA__ is None, returns BROWSER_ERROR (no DOM fallback for reviews)."""
        mock_browser.page_pool = mock_pool
        mock_extract.return_value = None

        from naukri_server.tools.ambitionbox import _fetch_reviews

        result = await _fetch_reviews("google")

        assert result["status"] == "error"
        assert result["error_code"] == "BROWSER_ERROR"
        assert "Could not extract review data" in result["message"]


# =====================================================================
# _fetch_interviews tests
# =====================================================================


class TestFetchInterviews:
    """Tests for _fetch_interviews with mocked browser."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_interviews_success(self, mock_browser, mock_goto, mock_pool, mock_page):
        """Full interview data with __NEXT_DATA__ returns success with parsed experiences."""
        mock_browser.page_pool = mock_pool
        mock_page.evaluate.return_value = _next_data_wrapper(INTERVIEWS_PAGE_PROPS)

        from naukri_server.tools.ambitionbox import _fetch_interviews

        result = await _fetch_interviews("google")

        assert result["status"] == "success"
        assert result["company_name"] == "Google"
        assert result["total_interviews"] == 100
        assert result["overall_difficulty"] == {"easy": 20, "moderate": 50, "hard": 30}
        assert result["count"] == 1

        exp = result["interview_experiences"][0]
        assert exp["designation"] == "SDE-2"
        assert exp["difficulty"] == "Moderate"
        assert exp["outcome"] == "Selected"
        assert exp["experience_type"] == "Campus"
        assert len(exp["questions"]) == 2
        assert "System design question" in exp["questions"]
        assert exp["date"] == "2024-01-15"
        assert exp["likes"] == "Well structured"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._scrape_interview_data", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._extract_next_data", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_interviews_fallback_dom_scrape(
        self, mock_browser, mock_goto, mock_extract, mock_scrape, mock_pool, mock_page
    ):
        """When __NEXT_DATA__ is absent, falls back to _scrape_interview_data."""
        mock_browser.page_pool = mock_pool
        mock_extract.return_value = None
        mock_scrape.return_value = {
            "company_name": "Google",
            "total_interviews": "50",
            "rating": "4.2",
            "difficulty": {"easy": "30%", "moderate": "50%", "hard": "20%"},
            "duration": {"under_2_weeks": "60%", "two_to_four_weeks": "40%"},
            "questions": [{"designation": "SDE", "question": "What is OOP?"}],
            "experiences": [
                {"designation": "Software Engineer", "date": "2d ago", "text_preview": "Good experience"}
            ],
        }

        from naukri_server.tools.ambitionbox import _fetch_interviews

        result = await _fetch_interviews("google")

        assert result["status"] == "success"
        assert result["source"] == "dom_scrape"
        assert result["company_name"] == "Google"
        assert result["count"] == 1
        assert len(result["interview_experiences"]) == 1
        assert len(result["sample_questions"]) == 1

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._scrape_interview_data", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._extract_next_data", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_interviews_no_data(
        self, mock_browser, mock_goto, mock_extract, mock_scrape, mock_pool, mock_page
    ):
        """When both __NEXT_DATA__ and DOM scrape return nothing, returns NOT_FOUND."""
        mock_browser.page_pool = mock_pool
        mock_extract.return_value = None
        # DOM scrape returns empty result
        mock_scrape.return_value = {
            "company_name": None,
            "total_interviews": None,
            "questions": [],
            "experiences": [],
        }

        from naukri_server.tools.ambitionbox import _fetch_interviews

        result = await _fetch_interviews("google")

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"
        assert "no __NEXT_DATA__" in result["message"].lower() or "could not extract" in result["message"].lower()


# =====================================================================
# naukri_company_intel routing tests
# =====================================================================


class TestCompanyIntelRouting:
    """Tests for naukri_company_intel action routing."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._fetch_salary", new_callable=AsyncMock)
    async def test_company_intel_routing_salary(self, mock_fetch):
        """intel_type='salary' routes to _fetch_salary with correct args."""
        mock_fetch.return_value = {"status": "success", "company": "Google"}

        from naukri_server.tools.ambitionbox import naukri_company_intel

        result = await naukri_company_intel(company="Google", intel_type="salary", designation="sde")

        assert result["status"] == "success"
        mock_fetch.assert_awaited_once()
        call_kwargs = mock_fetch.call_args
        assert call_kwargs[1]["company_slug"] == "google"
        assert call_kwargs[1]["designation"] == "sde"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock)
    async def test_company_intel_routing_reviews(self, mock_fetch):
        """intel_type='reviews' routes to _fetch_reviews with correct args."""
        mock_fetch.return_value = {"status": "success", "company": "Google"}

        from naukri_server.tools.ambitionbox import naukri_company_intel

        result = await naukri_company_intel(company="Google", intel_type="reviews", page=2)

        assert result["status"] == "success"
        mock_fetch.assert_awaited_once()
        call_kwargs = mock_fetch.call_args
        assert call_kwargs[1]["company_slug"] == "google"
        assert call_kwargs[1]["page"] == 2

    @pytest.mark.asyncio
    async def test_company_intel_routing_invalid(self):
        """Invalid intel_type returns VALIDATION_ERROR without calling any fetch."""
        from naukri_server.tools.ambitionbox import naukri_company_intel

        result = await naukri_company_intel(company="Google", intel_type="culture")

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "culture" in result["message"]
        assert "salary" in result["message"]
        assert "reviews" in result["message"]
        assert "interviews" in result["message"]


# =====================================================================
# _extract_company_id tests
# =====================================================================


class TestExtractCompanyId:
    """Tests for _extract_company_id helper."""

    def test_from_metadata(self):
        """Primary path: pageProps.metaData.companyId."""
        from naukri_server.tools.ambitionbox import _extract_company_id

        page_props = {"metaData": {"companyId": "41"}}
        assert _extract_company_id(page_props) == "41"

    def test_from_metadata_int(self):
        """metaData.companyId as int gets stringified."""
        from naukri_server.tools.ambitionbox import _extract_company_id

        page_props = {"metaData": {"companyId": 42}}
        assert _extract_company_id(page_props) == "42"

    def test_from_company_data_fallback(self):
        """Fallback: companyData.CompanyId."""
        from naukri_server.tools.ambitionbox import _extract_company_id

        page_props = {"companyData": {"CompanyId": 99, "CompanyName": "TCS"}}
        assert _extract_company_id(page_props) == "99"

    def test_from_company_header_data(self):
        """Fallback: companyHeaderData.CompanyId."""
        from naukri_server.tools.ambitionbox import _extract_company_id

        page_props = {"companyHeaderData": {"CompanyId": 55}}
        assert _extract_company_id(page_props) == "55"

    def test_none_when_empty(self):
        """Returns None for empty pageProps."""
        from naukri_server.tools.ambitionbox import _extract_company_id

        assert _extract_company_id({}) is None
        assert _extract_company_id(None) is None

    def test_none_when_no_id_field(self):
        """Returns None when containers exist but have no ID fields."""
        from naukri_server.tools.ambitionbox import _extract_company_id

        page_props = {"companyData": {"CompanyName": "Google"}}
        assert _extract_company_id(page_props) is None

    def test_metadata_takes_priority(self):
        """metaData.companyId is preferred over companyData.CompanyId."""
        from naukri_server.tools.ambitionbox import _extract_company_id

        page_props = {
            "metaData": {"companyId": "41"},
            "companyData": {"CompanyId": 99},
        }
        assert _extract_company_id(page_props) == "41"


# =====================================================================
# _enrich_with_rest tests
# =====================================================================


class TestEnrichWithRest:
    """Tests for _enrich_with_rest enrichment helper."""

    @pytest.mark.asyncio
    async def test_skips_when_no_company_id(self):
        """No _ab_company_id means no REST calls, result unchanged."""
        from naukri_server.tools.ambitionbox import _enrich_with_rest

        result = {"status": "success", "company": "Google"}
        enriched = await _enrich_with_rest(result)
        assert enriched is result
        assert "ab_rest" not in enriched

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.ab_get_competitors", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.ab_get_benefits", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.ab_get_work_culture", new_callable=AsyncMock)
    async def test_enriches_with_rest_data(self, mock_culture, mock_benefits, mock_competitors):
        """When _ab_company_id is present, REST data is merged under ab_rest."""
        from naukri_server.tools.ambitionbox import _enrich_with_rest

        mock_culture.return_value = {
            "status": "success",
            "reviews_count": 500,
            "work_timing": {"labels": ["Flexible"], "values": [80]},
        }
        mock_benefits.return_value = {
            "status": "success",
            "total_benefits": 30,
            "benefits": [{"name": "Health Insurance"}],
        }
        mock_competitors.return_value = {
            "status": "success",
            "count": 5,
            "competitors": [{"CompanyName": "Microsoft"}],
        }

        result = {"status": "success", "company": "Google", "_ab_company_id": "41"}
        enriched = await _enrich_with_rest(result)

        assert "ab_rest" in enriched
        assert "work_culture" in enriched["ab_rest"]
        assert "benefits" in enriched["ab_rest"]
        assert "competitors" in enriched["ab_rest"]
        assert enriched["ab_rest"]["work_culture"]["reviews_count"] == 500
        assert enriched["ab_rest"]["benefits"]["total_benefits"] == 30
        assert enriched["ab_rest"]["competitors"]["count"] == 5

        # Verify all three REST functions were called with the company ID
        mock_culture.assert_awaited_once_with("41")
        mock_benefits.assert_awaited_once_with("41")
        mock_competitors.assert_awaited_once_with("41")

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.ab_get_competitors", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.ab_get_benefits", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.ab_get_work_culture", new_callable=AsyncMock)
    async def test_partial_rest_failure(self, mock_culture, mock_benefits, mock_competitors):
        """If one REST call fails, other successful ones are still included."""
        from naukri_server.tools.ambitionbox import _enrich_with_rest

        mock_culture.return_value = {
            "status": "success",
            "reviews_count": 100,
            "work_timing": {"labels": [], "values": []},
        }
        mock_benefits.side_effect = RuntimeError("connection refused")
        mock_competitors.return_value = {
            "status": "success",
            "count": 3,
            "competitors": [],
        }

        result = {"status": "success", "company": "TCS", "_ab_company_id": "42"}
        enriched = await _enrich_with_rest(result)

        assert "ab_rest" in enriched
        assert "work_culture" in enriched["ab_rest"]
        assert "competitors" in enriched["ab_rest"]
        assert "benefits" not in enriched["ab_rest"]
        assert "_enrichment_errors" in enriched
        assert any("benefits" in e for e in enriched["_enrichment_errors"])

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.ab_get_competitors", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.ab_get_benefits", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.ab_get_work_culture", new_callable=AsyncMock)
    async def test_all_rest_fail_no_ab_rest_key(self, mock_culture, mock_benefits, mock_competitors):
        """If all REST calls fail, ab_rest key is not added."""
        from naukri_server.tools.ambitionbox import _enrich_with_rest

        mock_culture.side_effect = RuntimeError("fail")
        mock_benefits.side_effect = RuntimeError("fail")
        mock_competitors.side_effect = RuntimeError("fail")

        result = {"status": "success", "company": "X", "_ab_company_id": "99"}
        enriched = await _enrich_with_rest(result)

        assert "ab_rest" not in enriched
        assert "_enrichment_errors" in enriched
        assert len(enriched["_enrichment_errors"]) == 3


# =====================================================================
# naukri_company_intel REST enrichment integration tests
# =====================================================================


class TestCompanyIntelEnrichment:
    """Tests for naukri_company_intel calling _enrich_with_rest."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._enrich_with_rest", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_salary", new_callable=AsyncMock)
    async def test_enrichment_called_on_success(self, mock_fetch, mock_enrich):
        """On successful scrape, _enrich_with_rest is called."""
        from naukri_server.tools.ambitionbox import naukri_company_intel

        fetch_result = {"status": "success", "company": "Google", "_ab_company_id": "41"}
        mock_fetch.return_value = fetch_result
        mock_enrich.return_value = {**fetch_result, "ab_rest": {"work_culture": {}}}

        result = await naukri_company_intel(company="Google", intel_type="salary")

        assert result["status"] == "success"
        assert "ab_rest" in result
        mock_enrich.assert_awaited_once_with(fetch_result)

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._enrich_with_rest", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_salary", new_callable=AsyncMock)
    async def test_enrichment_skipped_on_error(self, mock_fetch, mock_enrich):
        """On failed scrape, _enrich_with_rest is NOT called."""
        from naukri_server.tools.ambitionbox import naukri_company_intel

        mock_fetch.return_value = {"status": "error", "message": "404", "error_code": "NOT_FOUND"}

        result = await naukri_company_intel(company="nonexistent", intel_type="salary")

        assert result["status"] == "error"
        mock_enrich.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._enrich_with_rest", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock)
    async def test_enrichment_called_for_reviews(self, mock_fetch, mock_enrich):
        """Reviews intel_type also triggers enrichment."""
        from naukri_server.tools.ambitionbox import naukri_company_intel

        fetch_result = {"status": "success", "company": "TCS", "_ab_company_id": "42"}
        mock_fetch.return_value = fetch_result
        mock_enrich.return_value = fetch_result

        result = await naukri_company_intel(company="TCS", intel_type="reviews", page=1)

        mock_enrich.assert_awaited_once_with(fetch_result)

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._enrich_with_rest", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock)
    async def test_enrichment_called_for_interviews(self, mock_fetch, mock_enrich):
        """Interviews intel_type also triggers enrichment."""
        from naukri_server.tools.ambitionbox import naukri_company_intel

        fetch_result = {"status": "success", "company_name": "Infosys", "_ab_company_id": "41"}
        mock_fetch.return_value = fetch_result
        mock_enrich.return_value = fetch_result

        result = await naukri_company_intel(company="Infosys", intel_type="interviews")

        mock_enrich.assert_awaited_once_with(fetch_result)


# =====================================================================
# _fetch_* functions include _ab_company_id when available
# =====================================================================


class TestFetchFunctionsExtractCompanyId:
    """Tests that _fetch_salary/reviews/interviews include _ab_company_id."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_salary_includes_company_id(self, mock_browser, mock_goto, mock_pool, mock_page):
        """_fetch_salary includes _ab_company_id when metaData.companyId is in pageProps."""
        mock_browser.page_pool = mock_pool
        props_with_id = {**SALARY_PAGE_PROPS, "metaData": {"companyId": "41"}}
        mock_page.evaluate.return_value = _next_data_wrapper(props_with_id)

        from naukri_server.tools.ambitionbox import _fetch_salary

        result = await _fetch_salary("google")

        assert result["status"] == "success"
        assert result["_ab_company_id"] == "41"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_salary_no_company_id_when_absent(self, mock_browser, mock_goto, mock_pool, mock_page):
        """_fetch_salary omits _ab_company_id when not in pageProps."""
        mock_browser.page_pool = mock_pool
        mock_page.evaluate.return_value = _next_data_wrapper(SALARY_PAGE_PROPS)

        from naukri_server.tools.ambitionbox import _fetch_salary

        result = await _fetch_salary("google")

        assert result["status"] == "success"
        assert "_ab_company_id" not in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_reviews_includes_company_id(self, mock_browser, mock_goto, mock_pool, mock_page):
        """_fetch_reviews includes _ab_company_id when available."""
        mock_browser.page_pool = mock_pool
        props_with_id = {**REVIEWS_PAGE_PROPS, "metaData": {"companyId": "55"}}
        mock_page.evaluate.return_value = _next_data_wrapper(props_with_id)

        from naukri_server.tools.ambitionbox import _fetch_reviews

        result = await _fetch_reviews("google")

        assert result["status"] == "success"
        assert result["_ab_company_id"] == "55"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox.browser")
    async def test_fetch_interviews_includes_company_id(self, mock_browser, mock_goto, mock_pool, mock_page):
        """_fetch_interviews includes _ab_company_id when available."""
        mock_browser.page_pool = mock_pool
        props_with_id = {**INTERVIEWS_PAGE_PROPS, "metaData": {"companyId": "10"}}
        mock_page.evaluate.return_value = _next_data_wrapper(props_with_id)

        from naukri_server.tools.ambitionbox import _fetch_interviews

        result = await _fetch_interviews("google")

        assert result["status"] == "success"
        assert result["_ab_company_id"] == "10"
