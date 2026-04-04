"""Deep tests for naukri_server.tools.compare — _compare_jobs side-by-side analysis.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier24_compare.py.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job_detail(job_id="J1", title="SWE", company="Acme",
                     salary="10-20 LPA", experience="2-5 Yrs",
                     location="Bangalore", work_mode="Work from office",
                     tags=None, company_rating=4.2, experience_min=2, experience_max=5):
    return {
        "status": "success", "job_id": job_id, "title": title, "company": company,
        "company_rating": company_rating, "salary": salary, "experience": experience,
        "experience_min": experience_min, "experience_max": experience_max,
        "location": location, "work_mode": work_mode,
        "tags": tags or ["Python", "Django"],
        "group_id": None, "vacancies": 2, "is_applied": False,
        "external_apply": False, "external_apply_url": None,
        "posted_date": "2026-03-01", "apply_count": 50, "candidates_count": 100,
    }


def _make_profile(key_skills=None, total_experience=3,
                  current_location="Bangalore", expected_ctc="15 LPA"):
    return {
        "status": "success",
        "key_skills": key_skills or ["Python", "Django", "REST API"],
        "total_experience": total_experience,
        "current_location": current_location,
        "expected_ctc": expected_ctc,
    }


_EMPTY_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# 1. Compare 2 jobs with profile available
# ---------------------------------------------------------------------------

class TestCompareJobs:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking._load_json", return_value=[])
    @patch("naukri_server.tools.tracking._applications_lock", new_callable=lambda: lambda: _EMPTY_LOCK)
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_compare_two_jobs_success(self, mock_get_job, mock_profile, mock_lock, mock_load):
        mock_get_job.side_effect = [
            _make_job_detail("J1", tags=["Python", "Django"]),
            _make_job_detail("J2", tags=["Python", "Go"], company="BetaCorp"),
        ]
        mock_profile.return_value = _make_profile()
        from naukri_server.tools.compare import _compare_jobs
        result = await _compare_jobs(["J1", "J2"], timeout_seconds=10)
        assert result["status"] == "success"
        assert result["count"] == 2
        assert any("python" in s.lower() for s in result["common_skills"])

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking._load_json", return_value=[])
    @patch("naukri_server.tools.tracking._applications_lock", new_callable=lambda: lambda: _EMPTY_LOCK)
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_fit_scores_computed(self, mock_get_job, mock_profile, mock_lock, mock_load):
        mock_get_job.side_effect = [
            _make_job_detail("J1", tags=["Python", "Django"]),
            _make_job_detail("J2", tags=["Java", "Spring"]),
        ]
        mock_profile.return_value = _make_profile(key_skills=["Python", "Django", "REST API"])
        from naukri_server.tools.compare import _compare_jobs
        result = await _compare_jobs(["J1", "J2"], timeout_seconds=10)
        for job in result["jobs"]:
            assert "fit_score" in job
            assert "matched_skills" in job
            assert "recommendation" in job
        assert "average_fit_score" in result
        assert "best_match_job_id" in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking._load_json", return_value=[])
    @patch("naukri_server.tools.tracking._applications_lock", new_callable=lambda: lambda: _EMPTY_LOCK)
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_one_job_fails_others_succeed(self, mock_get_job, mock_profile, mock_lock, mock_load):
        mock_get_job.side_effect = [Exception("timeout"), _make_job_detail("J2")]
        mock_profile.return_value = _make_profile()
        from naukri_server.tools.compare import _compare_jobs
        result = await _compare_jobs(["J1", "J2"], timeout_seconds=10)
        assert result["status"] == "success"
        assert result["count"] == 1
        assert any("J1" in e for e in result["errors"])

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking._load_json", return_value=[])
    @patch("naukri_server.tools.tracking._applications_lock", new_callable=lambda: lambda: _EMPTY_LOCK)
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_all_jobs_fail(self, mock_get_job, mock_profile, mock_lock, mock_load):
        mock_get_job.side_effect = [Exception("timeout"), Exception("not found")]
        mock_profile.return_value = _make_profile()
        from naukri_server.tools.compare import _compare_jobs
        result = await _compare_jobs(["J1", "J2"], timeout_seconds=10)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking._load_json", return_value=[])
    @patch("naukri_server.tools.tracking._applications_lock", new_callable=lambda: lambda: _EMPTY_LOCK)
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_profile_fetch_failure_graceful(self, mock_get_job, mock_profile, mock_lock, mock_load):
        """Profile failure = no fit_score keys, but jobs still returned."""
        mock_get_job.side_effect = [_make_job_detail("J1"), _make_job_detail("J2")]
        mock_profile.return_value = {"status": "error", "message": "auth failed"}
        from naukri_server.tools.compare import _compare_jobs
        result = await _compare_jobs(["J1", "J2"], timeout_seconds=10)
        assert result["status"] == "success"
        assert result["count"] == 2
        for job in result["jobs"]:
            assert "fit_score" not in job


class TestCompareJobsValidation:
    @pytest.mark.asyncio
    async def test_less_than_two_job_ids(self):
        from naukri_server.tools.compare import _compare_jobs
        result = await _compare_jobs(["J1"], timeout_seconds=5)
        assert result["status"] == "error"
        assert "at least 2" in result["message"]

    @pytest.mark.asyncio
    async def test_more_than_five_job_ids(self):
        from naukri_server.tools.compare import _compare_jobs
        result = await _compare_jobs(["J1", "J2", "J3", "J4", "J5", "J6"], timeout_seconds=5)
        assert result["status"] == "error"
        assert "Maximum 5" in result["message"]


class TestSkillSetComputation:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking._load_json", return_value=[])
    @patch("naukri_server.tools.tracking._applications_lock", new_callable=lambda: lambda: _EMPTY_LOCK)
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_common_and_all_skills(self, mock_get_job, mock_profile, mock_lock, mock_load):
        mock_get_job.side_effect = [
            _make_job_detail("J1", tags=["Python", "Django", "PostgreSQL"]),
            _make_job_detail("J2", tags=["Python", "FastAPI", "PostgreSQL"]),
        ]
        mock_profile.return_value = {"status": "error", "message": "no profile"}
        from naukri_server.tools.compare import _compare_jobs
        result = await _compare_jobs(["J1", "J2"], timeout_seconds=10)
        common_lower = [s.lower() for s in result["common_skills"]]
        assert "python" in common_lower
        assert "postgresql" in common_lower
        all_lower = [s.lower() for s in result["all_skills"]]
        assert "django" in all_lower
        assert "fastapi" in all_lower


class TestCompareJobsTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_partial_success(self):
        import asyncio as _asyncio
        async def _slow(*a, **kw):
            await _asyncio.sleep(999)
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock, side_effect=_slow), \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock, side_effect=_slow):
            from naukri_server.tools.compare import _compare_jobs
            result = await _compare_jobs(["J1", "J2"], timeout_seconds=0.001)
        assert result["status"] == "partial_success"
        assert result["error_code"] == "TIMEOUT"
