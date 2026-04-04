"""Tests for Tier 17 consolidation routing — 8 merged action branches.

Every test is PURE: no network, no browser, no file I/O.
We mock the private helper at its source module and verify the dispatcher routes correctly.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. naukri_profile(action="dashboard") → _get_dashboard
# =====================================================================

class TestProfileDashboard:
    """Profile dispatcher routes action='dashboard' to _get_dashboard."""

    @pytest.mark.asyncio
    async def test_dashboard_routes_to_helper(self):
        from naukri_server.tools.profile import naukri_profile
        with patch("naukri_server.tools.profile._get_dashboard", new_callable=AsyncMock) as mock:
            mock.return_value = {"status": "success", "profile_views": 42}
            result = await naukri_profile(action="dashboard")
            assert result["status"] == "success"
            assert result["profile_views"] == 42
            mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dashboard_propagates_helper_return(self):
        """Verify the dispatcher returns the helper's dict verbatim (no wrapping)."""
        from naukri_server.tools.profile import naukri_profile
        with patch("naukri_server.tools.profile._get_dashboard", new_callable=AsyncMock) as mock:
            mock.return_value = {"status": "success", "profile_views": 0, "extra_field": "kept"}
            result = await naukri_profile(action="dashboard")
            assert result["extra_field"] == "kept"


# =====================================================================
# 2. naukri_jobs(action="similar") → _get_similar_jobs
# =====================================================================

class TestJobsSimilar:
    """Jobs dispatcher routes action='similar' — REST-first, then fallback to search helper."""

    @pytest.mark.asyncio
    async def test_similar_requires_job_id(self):
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="similar")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_similar_rest_returns_parsed_list(self):
        """REST v2 similar jobs returns lightweight parsed result."""
        from naukri_server.tools.jobs import naukri_jobs
        fake_rest = {
            "simJobDetails": {
                "content": [],
                "collaborative": [
                    {
                        "jobId": "99",
                        "title": "Backend Dev",
                        "companyName": "Corp",
                        "salaryDetail": {"label": "10-15 LPA"},
                        "placeholders": [{"type": "location", "label": "Bangalore"}],
                        "experienceText": "2-5 Yrs",
                        "tagsAndSkills": "Python, Django, REST",
                        "jdURL": "/job/backend-dev-99",
                    }
                ],
            },
            "noOfJobs": 1,
        }
        with patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock, return_value=fake_rest):
            result = await naukri_jobs(action="similar", job_id="12345", limit=10)
            assert result["status"] == "success"
            assert result["source"] == "rest_api"
            assert result["count"] == 1
            job = result["jobs"][0]
            assert job["job_id"] == "99"
            assert job["title"] == "Backend Dev"
            assert job["company"] == "Corp"
            assert job["salary"] == "10-15 LPA"
            assert job["location"] == "Bangalore"
            assert "Python" in job["tags"]

    @pytest.mark.asyncio
    async def test_similar_rest_failure_falls_back(self):
        """When REST v2 fails, dispatcher falls back to search._get_similar_jobs."""
        from naukri_server.tools.jobs import naukri_jobs
        with patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock, side_effect=Exception("timeout")) as mock_rest, \
             patch("naukri_server.tools.search._get_similar_jobs", new_callable=AsyncMock) as mock_fallback:
            mock_fallback.return_value = {"status": "success", "job_id": "12345", "source": "similar", "total": 5, "count": 5, "jobs": []}
            result = await naukri_jobs(action="similar", job_id="12345", limit=10, page=1)
            assert result["status"] == "success"
            assert result["source"] == "similar"
            mock_rest.assert_awaited_once()
            mock_fallback.assert_awaited_once_with(job_id="12345", limit=10, page=1)


# =====================================================================
# 3. naukri_jobs(action="compare") → _compare_jobs
# =====================================================================

class TestJobsCompare:
    """Jobs dispatcher routes action='compare' to _compare_jobs in compare module."""

    @pytest.mark.asyncio
    async def test_compare_requires_job_ids(self):
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="compare")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_ids" in result["message"]

    @pytest.mark.asyncio
    async def test_compare_routes_to_helper(self):
        from naukri_server.tools.jobs import naukri_jobs
        with patch("naukri_server.tools.compare._compare_jobs", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "status": "success",
                "count": 2,
                "jobs": [],
                "common_skills": ["python"],
                "all_skills": ["python", "java"],
            }
            result = await naukri_jobs(action="compare", job_ids=["111", "222"], timeout_seconds=60)
            assert result["status"] == "success"
            assert result["count"] == 2
            mock.assert_awaited_once_with(job_ids=["111", "222"], timeout_seconds=60)


# =====================================================================
# 4. naukri_settings(action="subscription") → _get_subscription_status
# =====================================================================

class TestSettingsSubscription:
    """Settings dispatcher routes action='subscription' to _get_subscription_status."""

    @pytest.mark.asyncio
    async def test_subscription_routes_to_helper(self):
        from naukri_server.tools.settings import naukri_settings
        with patch("naukri_server.tools.subscription._get_subscription_status", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "status": "success",
                "is_paid": False,
                "has_active_subscription": False,
                "promo_code": "DEAL50",
            }
            result = await naukri_settings(action="subscription")
            assert result["status"] == "success"
            assert result["is_paid"] is False
            assert result["promo_code"] == "DEAL50"
            mock.assert_awaited_once()


# =====================================================================
# 5. naukri_company(action="research") → _research_company
# =====================================================================

class TestCompanyResearch:
    """Company dispatcher routes action='research' to _research_company."""

    @pytest.mark.asyncio
    async def test_research_requires_keyword(self):
        from naukri_server.tools.companies import naukri_company
        result = await naukri_company(action="research")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keyword" in result["message"]

    @pytest.mark.asyncio
    async def test_research_routes_to_helper(self):
        from naukri_server.tools.companies import naukri_company
        with patch("naukri_server.tools.research._research_company", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "status": "success",
                "company_name": "Google",
                "slug": "google",
                "jobs": {"total": 100, "matching": 5, "sample": []},
            }
            result = await naukri_company(
                action="research", keyword="Google",
                include_jobs=True, include_reviews=False,
                include_interviews=False, jobs_limit=3, timeout_seconds=60,
            )
            assert result["status"] == "success"
            assert result["company_name"] == "Google"
            mock.assert_awaited_once_with(
                keyword="Google", include_jobs=True,
                include_reviews=False, include_interviews=False,
                jobs_limit=3, timeout_seconds=60,
            )


# =====================================================================
# 6. naukri_resume_builder(action="tailor") → _tailor_resume
# =====================================================================

class TestResumeBuilderTailor:
    """Resume builder dispatcher routes action='tailor' to _tailor_resume."""

    @pytest.mark.asyncio
    async def test_tailor_requires_job_id(self):
        from naukri_server.tools.resume_builder import naukri_resume_builder
        result = await naukri_resume_builder(action="tailor")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_tailor_empty_job_id_rejected(self):
        """Empty string for job_id is treated as falsy and fails validation."""
        from naukri_server.tools.resume_builder import naukri_resume_builder
        result = await naukri_resume_builder(action="tailor", job_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_tailor_helper_exception_caught(self):
        """If _tailor_resume raises, the dispatcher catches and returns error."""
        from naukri_server.tools.resume_builder import naukri_resume_builder
        with patch("naukri_server.tools.resume_tailor._tailor_resume", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("network down")
            result = await naukri_resume_builder(action="tailor", job_id="12345")
            assert result["status"] == "error"
            assert "RuntimeError" in result["message"]
            assert result["error_code"] == "INTERNAL_ERROR"

    @pytest.mark.asyncio
    async def test_tailor_routes_to_helper(self):
        from naukri_server.tools.resume_builder import naukri_resume_builder
        with patch("naukri_server.tools.resume_tailor._tailor_resume", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "status": "success",
                "job_title": "Python Dev",
                "company": "Acme",
                "suggestions": {"headline": "Add Python", "skills_to_add": ["Django"]},
            }
            result = await naukri_resume_builder(action="tailor", job_id="99999", timeout_seconds=90)
            assert result["status"] == "success"
            assert result["job_title"] == "Python Dev"
            mock.assert_awaited_once_with(job_id="99999", timeout_seconds=90)


# =====================================================================
# 7. naukri_insights(insight_type="skill_gap") → _skill_gap_analysis
# =====================================================================

class TestInsightsSkillGap:
    """Insights dispatcher routes insight_type='skill_gap' to _skill_gap_analysis."""

    @pytest.mark.asyncio
    async def test_skill_gap_routes_to_helper(self):
        from naukri_server.tools.insights import naukri_insights
        with patch("naukri_server.tools.skill_gap._skill_gap_analysis", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "status": "success",
                "jobs_analyzed": 20,
                "skill_gaps": [{"skill": "kubernetes", "frequency": 12}],
                "strong_skills": [{"skill": "python", "frequency": 18}],
                "assessments_used": 2,
            }
            result = await naukri_insights(
                insight_type="skill_gap",
                keywords="python developer",
                use_recommendations=False,
                sample_size=25,
                include_assessments=True,
                timeout_seconds=90,
            )
            assert result["status"] == "success"
            assert result["jobs_analyzed"] == 20
            assert result["skill_gaps"][0]["skill"] == "kubernetes"
            mock.assert_awaited_once_with(
                keywords="python developer",
                use_recommendations=False,
                sample_size=25,
                include_assessments=True,
                timeout_seconds=90,
            )


# =====================================================================
# 8. naukri_insights(insight_type="salary_benchmark") → _salary_benchmark
# =====================================================================

class TestInsightsSalaryBenchmark:
    """Insights dispatcher routes insight_type='salary_benchmark' to _salary_benchmark."""

    @pytest.mark.asyncio
    async def test_salary_benchmark_requires_keywords(self):
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="salary_benchmark")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keywords" in result["message"]

    @pytest.mark.asyncio
    async def test_salary_benchmark_routes_to_helper(self):
        from naukri_server.tools.insights import naukri_insights
        with patch("naukri_server.tools.research._salary_benchmark", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "status": "success",
                "keywords": "data engineer",
                "jobs_sampled": 15,
                "jobs_with_salary": 10,
                "salary_aggregate": {"min": 8.0, "max": 35.0, "avg": 18.5, "median": 17.0},
                "your_positioning": {"current_vs_market": "at_market"},
                "salary_by_company": [],
            }
            result = await naukri_insights(
                insight_type="salary_benchmark",
                keywords="data engineer",
                location="Bangalore",
                sample_size=15,
                freshness=7,
                timeout_seconds=60,
            )
            assert result["status"] == "success"
            assert result["jobs_sampled"] == 15
            assert result["salary_aggregate"]["avg"] == 18.5
            mock.assert_awaited_once_with(
                keywords="data engineer",
                location="Bangalore",
                sample_size=15,
                freshness=7,
                timeout_seconds=60,
            )
