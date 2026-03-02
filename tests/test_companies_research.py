"""Tests for company discovery, company follow, company intel, and research tools.

Every test is PURE: no network, no browser, no file I/O.
We only exercise validation / routing logic that runs BEFORE any async helper call.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. naukri_company — unified company discovery
# =====================================================================

class TestCompany:
    """Tests for naukri_server.tools.companies.naukri_company."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_search_requires_keyword(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="search")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keyword" in result["message"]

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="search", keyword="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keyword" in result["message"]

    @pytest.mark.asyncio
    async def test_jobs_requires_group_id(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="jobs")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_jobs_empty_group_id(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="jobs", group_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_slug_requires_group_id(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="slug")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_slug_empty_group_id(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="slug", group_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_search_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_company
        with patch("naukri_server.tools.companies._search_companies", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "companies": [], "total": 0}
            result = await naukri_company(action="search", keyword="google", page=2, limit=10)
            mock_helper.assert_awaited_once_with(keyword="google", page=2, limit=10)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_jobs_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_company
        with patch("naukri_server.tools.companies._get_company_jobs", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "jobs": [], "total": 0}
            result = await naukri_company(action="jobs", group_id="12345", page=1, limit=5)
            mock_helper.assert_awaited_once_with(group_id="12345", page=1, limit=5)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_slug_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_company
        with patch("naukri_server.tools.companies._get_company_slug", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "group_id": "12345", "company_slug": "google"}
            result = await naukri_company(action="slug", group_id="12345")
            mock_helper.assert_awaited_once_with(group_id="12345")
            assert result["status"] == "success"


# =====================================================================
# 2. naukri_company_follow — edge cases beyond test_consolidation.py
# =====================================================================

class TestCompanyFollowEdgeCases:
    """Edge-case tests for naukri_server.tools.companies.naukri_company_follow."""

    @pytest.mark.asyncio
    async def test_empty_group_ids_list(self):
        """An empty list [] should fail validation, same as missing."""
        from naukri_server.tools.companies import naukri_company_follow
        result = await naukri_company_follow(action="follow", group_ids=[])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_ids" in result["message"]

    @pytest.mark.asyncio
    async def test_follow_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_company_follow
        with patch("naukri_server.tools.companies._follow_or_unfollow", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "action": "followed", "followed": ["111"]}
            result = await naukri_company_follow(action="follow", group_ids=["111"])
            mock_helper.assert_awaited_once_with(["111"], "follow")
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_unfollow_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_company_follow
        with patch("naukri_server.tools.companies._follow_or_unfollow", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "action": "unfollowed", "unfollowed": ["222"]}
            result = await naukri_company_follow(action="unfollow", group_ids=["222"])
            mock_helper.assert_awaited_once_with(["222"], "unfollow")
            assert result["status"] == "success"


# =====================================================================
# 3. naukri_company_intel — validation beyond test_consolidation.py
# =====================================================================

class TestCompanyIntelExtended:
    """Extended tests for naukri_server.tools.ambitionbox.naukri_company_intel."""

    @pytest.mark.asyncio
    async def test_salary_routes_with_designation(self):
        """Salary action with a designation param should forward slug + designation."""
        from naukri_server.tools.ambitionbox import naukri_company_intel
        with patch("naukri_server.tools.ambitionbox._fetch_salary", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "salaries": []}
            result = await naukri_company_intel(company="Google", intel_type="salary", designation="software-engineer")
            mock_helper.assert_awaited_once_with(company_slug="google", designation="software-engineer")
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_interviews_routes_with_page(self):
        """Interviews action should forward the page parameter."""
        from naukri_server.tools.ambitionbox import naukri_company_intel
        with patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "interview_experiences": []}
            result = await naukri_company_intel(company="tcs", intel_type="interviews", page=3)
            mock_helper.assert_awaited_once_with(company_slug="tcs", page=3)
            assert result["status"] == "success"


# =====================================================================
# 4. naukri_research_company — validation and routing
# =====================================================================

class TestResearchCompany:
    """Tests for naukri_server.tools.research.naukri_research_company."""

    @pytest.mark.asyncio
    async def test_returns_slug_from_keyword(self):
        """Research should derive a slug and include it in the result."""
        from naukri_server.tools.research import naukri_research_company
        # Helpers are late-imported inside the function — patch at their source modules
        with patch("naukri_server.tools.ambitionbox._fetch_salary", new_callable=AsyncMock) as mock_sal, \
             patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock) as mock_rev, \
             patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock) as mock_int, \
             patch("naukri_server.tools.companies._search_companies", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.companies._get_company_jobs", new_callable=AsyncMock) as mock_jobs:
            mock_search.return_value = {"status": "success", "companies": [{"group_id": "1", "name": "Google"}]}
            mock_jobs.return_value = {"status": "success", "total": 0, "count": 0, "jobs": []}
            mock_sal.return_value = {"status": "success", "avg_salary": 2000000}
            mock_rev.return_value = {"status": "success", "overall_rating": 4.2}
            mock_int.return_value = {"status": "success", "interview_experiences": []}
            result = await naukri_research_company(keyword="Google")
            assert result["status"] == "success"
            assert result["slug"] == "google"
            assert result["company_name"] == "Google"

    @pytest.mark.asyncio
    async def test_skips_jobs_when_disabled(self):
        """include_jobs=False should skip the jobs section entirely."""
        from naukri_server.tools.research import naukri_research_company
        with patch("naukri_server.tools.ambitionbox._fetch_salary", new_callable=AsyncMock) as mock_sal, \
             patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock) as mock_rev, \
             patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock) as mock_int:
            mock_sal.return_value = {"status": "success", "avg_salary": 1500000}
            mock_rev.return_value = {"status": "success", "overall_rating": 3.8}
            mock_int.return_value = {"status": "success", "interview_experiences": []}
            result = await naukri_research_company(keyword="TCS", include_jobs=False)
            assert result["status"] == "success"
            assert "jobs" not in result

    @pytest.mark.asyncio
    async def test_skips_reviews_and_interviews_when_disabled(self):
        """include_reviews=False, include_interviews=False should skip AmbitionBox calls."""
        from naukri_server.tools.research import naukri_research_company
        with patch("naukri_server.tools.companies._search_companies", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.companies._get_company_jobs", new_callable=AsyncMock) as mock_jobs:
            mock_search.return_value = {"status": "success", "companies": [{"group_id": "2", "name": "Infosys"}]}
            mock_jobs.return_value = {"status": "success", "total": 5, "count": 3, "jobs": [{"title": "Dev"}]}
            result = await naukri_research_company(
                keyword="Infosys", include_reviews=False, include_interviews=False,
            )
            assert result["status"] == "success"
            assert "salary" not in result
            assert "reviews" not in result
            assert "interviews" not in result

    @pytest.mark.asyncio
    async def test_partial_failure_collects_errors(self):
        """If salary fetch raises an exception, it should appear in errors list."""
        from naukri_server.tools.research import naukri_research_company
        with patch("naukri_server.tools.ambitionbox._fetch_salary", new_callable=AsyncMock) as mock_sal, \
             patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock) as mock_rev, \
             patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock) as mock_int:
            mock_sal.side_effect = RuntimeError("connection timeout")
            mock_rev.return_value = {"status": "success", "overall_rating": 4.0}
            mock_int.return_value = {"status": "success", "interview_experiences": []}
            result = await naukri_research_company(keyword="Wipro", include_jobs=False)
            assert result["status"] == "success"
            assert "errors" in result
            assert any("Salary" in e and "connection timeout" in e for e in result["errors"])


# =====================================================================
# 5. Helper-level validation
# =====================================================================

class TestHelperValidation:
    """Validation inside internal helpers that runs before any API/browser call."""

    @pytest.mark.asyncio
    async def test_search_companies_page_below_one(self):
        """_search_companies rejects page < 1."""
        from naukri_server.tools.companies import _search_companies
        result = await _search_companies(keyword="test", page=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]

    @pytest.mark.asyncio
    async def test_fetch_reviews_page_below_one(self):
        """_fetch_reviews rejects page < 1."""
        from naukri_server.tools.ambitionbox import _fetch_reviews
        result = await _fetch_reviews(company_slug="google", page=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]

    @pytest.mark.asyncio
    async def test_fetch_interviews_page_below_one(self):
        """_fetch_interviews rejects page < 1."""
        from naukri_server.tools.ambitionbox import _fetch_interviews
        result = await _fetch_interviews(company_slug="google", page=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]
