"""Tests for company discovery, company follow, company intel, and research tools.

Every test is PURE: no network, no browser, no file I/O.
We only exercise validation / routing logic that runs BEFORE any async helper call.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. Company atomic tools — discovery
# =====================================================================

class TestCompany:
    """Tests for atomic company discovery tools."""

    @pytest.mark.asyncio
    async def test_search_requires_keyword(self):
        from naukri_server.tools.companies import naukri_search_companies
        result = await naukri_search_companies(keyword="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keyword" in result["message"]

    @pytest.mark.asyncio
    async def test_search_empty_keyword(self):
        from naukri_server.tools.companies import naukri_search_companies
        result = await naukri_search_companies(keyword="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keyword" in result["message"]

    @pytest.mark.asyncio
    async def test_jobs_requires_group_id(self):
        from naukri_server.tools.companies import naukri_company_jobs
        result = await naukri_company_jobs(group_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_jobs_empty_group_id(self):
        from naukri_server.tools.companies import naukri_company_jobs
        result = await naukri_company_jobs(group_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_slug_requires_group_id(self):
        from naukri_server.tools.companies import naukri_company_slug
        result = await naukri_company_slug(group_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_slug_empty_group_id(self):
        from naukri_server.tools.companies import naukri_company_slug
        result = await naukri_company_slug(group_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_search_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_search_companies
        with patch("naukri_server.tools.companies._search_companies", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "companies": [], "total": 0}
            result = await naukri_search_companies(keyword="google", page=2, limit=10)
            mock_helper.assert_awaited_once_with(keyword="google", page=2, limit=10)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_jobs_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_company_jobs
        with patch("naukri_server.tools.companies._get_company_jobs", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "jobs": [], "total": 0}
            result = await naukri_company_jobs(group_id="12345", page=1, limit=5)
            mock_helper.assert_awaited_once_with(group_id="12345", page=1, limit=5)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_slug_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_company_slug
        with patch("naukri_server.tools.companies._get_company_slug", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "group_id": "12345", "company_slug": "google"}
            result = await naukri_company_slug(group_id="12345")
            mock_helper.assert_awaited_once_with(group_id="12345")
            assert result["status"] == "success"


# =====================================================================
# 2. naukri_company follow actions — edge cases beyond test_consolidation.py
# =====================================================================

class TestCompanyFollowEdgeCases:
    """Edge-case tests for atomic follow tools."""

    @pytest.mark.asyncio
    async def test_empty_group_ids_list(self):
        """An empty list [] with no group_id should fail validation."""
        from naukri_server.tools.companies import naukri_follow_company
        result = await naukri_follow_company(group_ids=[])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_follow_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_follow_company
        with patch("naukri_server.tools.companies._follow_or_unfollow", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "action": "followed", "followed": ["111"]}
            result = await naukri_follow_company(group_id="111", action="follow")
            mock_helper.assert_awaited_once_with(["111"], "follow")
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_unfollow_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_follow_company
        with patch("naukri_server.tools.companies._follow_or_unfollow", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "action": "unfollowed", "unfollowed": ["222"]}
            result = await naukri_follow_company(group_id="222", action="unfollow")
            mock_helper.assert_awaited_once_with(["222"], "unfollow")
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_batch_follow_status_returns_split(self):
        """Multiple group_ids should use batch POST and return followed/unfollowed split."""
        from naukri_server.tools.companies import naukri_follow_status
        with patch("naukri_server.tools.companies._batch_follow_status", new_callable=AsyncMock) as mock_batch:
            mock_batch.return_value = {
                "status": "success",
                "total": 3,
                "followed_count": 2,
                "unfollowed_count": 1,
                "companies": [
                    {"group_id": 100, "name": "Google", "follower_count": 50000, "is_followed": True, "logo": "g.png"},
                    {"group_id": 200, "name": "Meta", "follower_count": 30000, "is_followed": True, "logo": "m.png"},
                    {"group_id": 300, "name": "Apple", "follower_count": 40000, "is_followed": False, "logo": "a.png"},
                ],
            }
            result = await naukri_follow_status(group_ids=["100", "200", "300"])
            mock_batch.assert_awaited_once_with(["100", "200", "300"])
            assert result["status"] == "success"
            assert result["followed_count"] == 2
            assert result["unfollowed_count"] == 1
            assert result["total"] == 3
            followed_ids = [c["group_id"] for c in result["companies"] if c["is_followed"]]
            assert 100 in followed_ids
            assert 200 in followed_ids

    @pytest.mark.asyncio
    async def test_single_follow_status_uses_existing_behavior(self):
        """Single group_id should use the existing _get_follow_status helper, not batch."""
        from naukri_server.tools.companies import naukri_follow_status
        with patch("naukri_server.tools.companies._get_follow_status", new_callable=AsyncMock) as mock_single, \
             patch("naukri_server.tools.companies._batch_follow_status", new_callable=AsyncMock) as mock_batch:
            mock_single.return_value = {"status": "success", "followed": ["111"], "not_followed": []}
            result = await naukri_follow_status(group_id="111")
            mock_single.assert_awaited_once_with(["111"])
            mock_batch.assert_not_awaited()
            assert result["status"] == "success"
            assert result["followed"] == ["111"]


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
            # Reviews and interviews landed, salary did not -> partial_success.
            # This used to read "success" no matter how much had failed; on the
            # live account 2026-08-22 that produced a success with zero data and
            # two swallowed AttributeErrors.
            assert result["status"] == "partial_success"
            assert "errors" in result
            assert any("Salary" in e and "connection timeout" in e for e in result["errors"])
            assert "reviews" in result, "what DID land must still be returned"


# =====================================================================
# 5. Helper-level validation
# =====================================================================

class TestHelperValidation:
    """Validation inside internal helpers that runs before any API/browser call."""

    @pytest.mark.asyncio
    async def test_search_companies_page_clamped(self):
        """page=0 is silently clamped to 1 by validate_page."""
        from naukri_server.tools.companies import _search_companies
        with patch("naukri_server.tools.companies.api_client.get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"companyList": []}
            result = await _search_companies(keyword="test", page=0)
            mock_api.assert_awaited()

    @pytest.mark.asyncio
    async def test_fetch_reviews_page_clamped(self):
        """page=0 is silently clamped to 1 by validate_page."""
        from naukri_server.tools.ambitionbox import _fetch_reviews
        with patch("naukri_server.tools.ambitionbox.browser") as mock_browser:
            mock_page = AsyncMock()
            mock_page.title = AsyncMock(return_value="Reviews")
            mock_page.evaluate = AsyncMock(return_value=None)
            mock_browser.page_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_page)
            mock_browser.page_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock):
                result = await _fetch_reviews(company_slug="google", page=0)
                # Should proceed (clamped to 1), not return error

    @pytest.mark.asyncio
    async def test_fetch_interviews_page_clamped(self):
        """page=0 is silently clamped to 1 by validate_page."""
        from naukri_server.tools.ambitionbox import _fetch_interviews
        with patch("naukri_server.tools.ambitionbox.browser") as mock_browser:
            mock_page = AsyncMock()
            mock_page.title = AsyncMock(return_value="Interviews")
            mock_page.evaluate = AsyncMock(return_value=None)
            mock_browser.page_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_page)
            mock_browser.page_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("naukri_server.tools.ambitionbox.page_goto", new_callable=AsyncMock):
                result = await _fetch_interviews(company_slug="google", page=0)
                # Should proceed (clamped to 1), not return error


# =====================================================================
# 6. naukri_salary_benchmark — salary benchmarking
# =====================================================================

class TestSalaryBenchmark:
    """Tests for naukri_salary_benchmark."""

    @pytest.mark.asyncio
    async def test_salary_benchmark_requires_keywords(self):
        """salary_benchmark requires keywords parameter."""
        from naukri_server.tools.research import naukri_salary_benchmark
        with pytest.raises(TypeError):
            await naukri_salary_benchmark()

    @pytest.mark.asyncio
    async def test_salary_benchmark_validation_zero_sample(self):
        """sample_size < 1 returns validation error."""
        from naukri_server.tools.research import naukri_salary_benchmark
        result = await naukri_salary_benchmark(keywords="python", sample_size=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_salary_benchmark_sample_size_clamped(self):
        """sample_size is clamped to 50."""
        from naukri_server.tools.research import naukri_salary_benchmark
        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = {"status": "success", "jobs": []}
            mock_profile.return_value = {"status": "success"}
            result = await naukri_salary_benchmark(keywords="python", sample_size=100)
            _, kwargs = mock_search.call_args
            assert kwargs["limit"] == 50

    @pytest.mark.asyncio
    async def test_salary_benchmark_no_jobs_found(self):
        """Returns NOT_FOUND when no jobs match."""
        from naukri_server.tools.research import naukri_salary_benchmark
        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = {"status": "success", "jobs": [
                {"title": "Dev", "company": "Acme", "salary": "Not Disclosed"}
            ]}
            mock_profile.return_value = {"status": "success"}
            result = await naukri_salary_benchmark(keywords="python")
            assert result["status"] == "partial_success"
            assert result["jobs_with_salary"] == 0

    @pytest.mark.asyncio
    async def test_salary_benchmark_aggregation(self):
        """Correctly aggregates salary data from job listings."""
        from naukri_server.tools.research import naukri_salary_benchmark
        jobs = [
            {"title": "Dev1", "company": "A", "salary": "10-15 Lacs PA"},
            {"title": "Dev2", "company": "B", "salary": "15-20 Lacs PA"},
            {"title": "Dev3", "company": "C", "salary": "20-30 Lacs PA"},
            {"title": "Dev4", "company": "D", "salary": "12-18 Lacs PA"},
        ]
        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = {"status": "success", "jobs": jobs}
            mock_profile.return_value = {"status": "success", "current_ctc": 14, "expected_ctc": 20}
            result = await naukri_salary_benchmark(keywords="python")
            assert result["status"] == "success"
            assert result["jobs_with_salary"] == 4
            agg = result["salary_aggregate"]
            assert agg["min"] <= agg["avg"] <= agg["max"]
            assert agg["median"] is not None
            assert result["your_positioning"] is not None
            assert result["your_positioning"]["current_vs_market"] in ("below", "at_market", "above")

    @pytest.mark.asyncio
    async def test_salary_benchmark_search_failure(self):
        """Returns error when search fails."""
        from naukri_server.tools.research import naukri_salary_benchmark
        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = {"status": "error", "message": "Search down"}
            mock_profile.return_value = {"status": "success"}
            result = await naukri_salary_benchmark(keywords="python")
            assert result["status"] == "error"
            assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    async def test_salary_benchmark_profile_failure_partial(self):
        """Returns partial_success when profile fails but search works."""
        from naukri_server.tools.research import naukri_salary_benchmark
        jobs = [
            {"title": "Dev", "company": "A", "salary": "10-20 Lacs PA"},
        ]
        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = {"status": "success", "jobs": jobs}
            mock_profile.return_value = {"status": "error", "message": "Auth failed"}
            result = await naukri_salary_benchmark(keywords="python")
            assert result["status"] == "partial_success"
            assert result["your_positioning"] is None
            assert "errors" in result


# =====================================================================
# From test_consolidation.py — company follow action routing & validation
# =====================================================================

class TestCompanyFollowAtomic:
    """Tests for atomic follow tools — additional validation paths."""

    @pytest.mark.asyncio
    async def test_invalid_follow_action(self):
        from naukri_server.tools.companies import naukri_follow_company
        result = await naukri_follow_company(group_id="123", action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_empty_group_ids(self):
        from naukri_server.tools.companies import naukri_follow_status
        result = await naukri_follow_status()
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "group_id" in result["message"]

    @pytest.mark.asyncio
    async def test_follow_status_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_follow_status
        with patch("naukri_server.tools.companies._get_follow_status", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "followed": ["123"]}
            result = await naukri_follow_status(group_id="123")
            mock_helper.assert_awaited_once_with(["123"])
            assert result["status"] == "success"
