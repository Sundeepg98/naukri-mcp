"""Deep tests for naukri_server.tools.smart_apply — scoring, bulk saved, apply_top_fits,
action routing, agent-eligible priority sorting.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier24_smart_apply.py and tier24.py.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job_result(job_id="J1", title="SWE", company="Acme",
                     salary="10-20 LPA", location="Bangalore",
                     experience="2-5 Yrs", work_mode="Work from office",
                     tags=None):
    return {
        "status": "success",
        "job_id": job_id,
        "title": title,
        "company": company,
        "salary": salary,
        "location": location,
        "experience": experience,
        "work_mode": work_mode,
        "tags": tags or ["Python", "Django"],
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


def _make_saved_jobs(count=2):
    return {
        "status": "success",
        "saved_jobs": [
            {"job_id": f"J{i}", "title": f"Job {i}", "company": f"Co{i}"}
            for i in range(count)
        ],
    }


# ---------------------------------------------------------------------------
# 1. _score_job
# ---------------------------------------------------------------------------

class TestScoreJob:
    def test_basic_score_job_returns_dict(self):
        from naukri_server.tools.smart_apply import _score_job
        result = _score_job(_make_job_result(), _make_profile())
        assert isinstance(result, dict)
        assert "overall_score" in result
        assert "skill_match" in result
        assert "recommendation" in result

    def test_score_job_with_is_agent_eligible_false(self):
        from naukri_server.tools.smart_apply import _score_job
        result = _score_job(_make_job_result(), _make_profile(), is_agent_eligible=False)
        assert 0 <= result["overall_score"] <= 100

    def test_score_job_uses_skills_key_fallback(self):
        """Falls back to 'skills' key when 'tags' is absent."""
        from naukri_server.tools.smart_apply import _score_job
        job = _make_job_result(tags=None)
        job.pop("tags", None)
        job["skills"] = ["Python"]
        result = _score_job(job, _make_profile())
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 2. _bulk_saved_scoring
# ---------------------------------------------------------------------------

class TestBulkSavedScoring:
    @pytest.mark.asyncio
    async def test_empty_saved_jobs(self):
        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved:
            with patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
                mock_saved.return_value = {"status": "success", "saved_jobs": []}
                mock_profile.return_value = _make_profile()
                from naukri_server.tools.smart_apply import _bulk_saved_scoring
                result = await _bulk_saved_scoring(min_fit_score=0, timeout_seconds=5)
        assert result["status"] == "success"
        assert result["total_saved"] == 0
        assert result["scored_jobs"] == []

    @pytest.mark.asyncio
    async def test_bulk_saved_profile_fetch_failure(self):
        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved:
            with patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
                mock_saved.return_value = _make_saved_jobs(2)
                mock_profile.return_value = {"status": "error", "message": "Unauthorized"}
                from naukri_server.tools.smart_apply import _bulk_saved_scoring
                result = await _bulk_saved_scoring(min_fit_score=0, timeout_seconds=5)
        assert result["status"] == "error"
        assert "profile" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_bulk_saved_job_detail_fetch_failure_graceful(self):
        """If one job detail fetch fails, it's skipped gracefully."""
        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_get_job:
            mock_saved.return_value = _make_saved_jobs(2)
            mock_profile.return_value = _make_profile()
            mock_get_job.side_effect = [Exception("timeout"), _make_job_result("J1")]
            from naukri_server.tools.smart_apply import _bulk_saved_scoring
            result = await _bulk_saved_scoring(min_fit_score=0, timeout_seconds=10)
        assert result["status"] == "success"
        assert result["scored_count"] <= 1


# ---------------------------------------------------------------------------
# 3. _apply_top_fits
# ---------------------------------------------------------------------------

class TestApplyTopFits:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_no_jobs_above_threshold(self, mock_bulk):
        mock_bulk.return_value = {"status": "success", "total_saved": 3, "scored_jobs": []}
        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=80)
        assert result["status"] == "success"
        assert result["applied"] == 0
        assert "No saved jobs" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_applies_top_n(self, mock_bulk, mock_apply):
        mock_bulk.return_value = {
            "status": "success",
            "total_saved": 5,
            "scored_jobs": [
                {"job_id": f"J{i}", "title": f"Job {i}", "company": "Co",
                 "fit_score": 90 - i, "fit_details": {"bonuses": {}}}
                for i in range(5)
            ],
        }
        mock_apply.return_value = {"status": "applied"}
        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=60, limit=3)
        assert result["status"] == "success"
        assert result["applied"] == 3
        assert result["attempted"] == 3

    @pytest.mark.asyncio
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_bulk_scoring_error_propagated(self, mock_bulk):
        mock_bulk.return_value = {"status": "error", "message": "timeout", "error_code": "API_ERROR"}
        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=60)
        assert result["status"] == "error"
        assert "timeout" in result["message"]


# ---------------------------------------------------------------------------
# 4. Agent-eligible sort priority in _apply_top_fits
# ---------------------------------------------------------------------------

class TestApplyTopFitsPriority:
    def _make_scored_jobs(self, specs):
        jobs = []
        for job_id, fit_score, agent_bonus in specs:
            jobs.append({
                "job_id": job_id, "title": f"Job {job_id}", "company": "Co",
                "fit_score": fit_score,
                "fit_details": {"bonuses": {"agent_eligible": agent_bonus}},
            })
        return jobs

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_agent_eligible_sorted_first_at_equal_score(self, mock_bulk, mock_apply):
        """Two jobs at score 80: agent-eligible applied first."""
        mock_bulk.return_value = {
            "status": "success", "total_saved": 2,
            "scored_jobs": self._make_scored_jobs([("J_non", 80, 0), ("J_elig", 80, 5)]),
        }
        mock_apply.return_value = {"status": "applied"}
        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=60, limit=1)
        assert result["applied"] == 1
        assert result["results"][0]["job_id"] == "J_elig"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_full_ordering_eligible_then_score_descending(self, mock_bulk, mock_apply):
        mock_bulk.return_value = {
            "status": "success", "total_saved": 4,
            "scored_jobs": self._make_scored_jobs([
                ("JE90", 90, 5), ("JE80", 80, 5), ("JN95", 95, 0), ("JN70", 70, 0),
            ]),
        }
        mock_apply.return_value = {"status": "applied"}
        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=60, limit=4)
        applied_ids = [r["job_id"] for r in result["results"]]
        assert applied_ids.index("JE90") < applied_ids.index("JN95")
        assert applied_ids.index("JE80") < applied_ids.index("JN70")


# ---------------------------------------------------------------------------
# 5. naukri_smart_apply — action routing
# ---------------------------------------------------------------------------

class TestNaukriSmartApplyAtomic:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_score_saved_jobs_routes(self, mock_bulk):
        mock_bulk.return_value = {"status": "success", "scored_jobs": [], "total_saved": 0, "scored_count": 0, "min_fit_score": 60}
        from naukri_server.tools.smart_apply import naukri_score_saved_jobs
        result = await naukri_score_saved_jobs(min_fit_score=60)
        assert result["status"] == "success"
        mock_bulk.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.smart_apply._apply_top_fits", new_callable=AsyncMock)
    async def test_apply_top_fits_routes(self, mock_atf):
        mock_atf.return_value = {"status": "success", "applied": 2}
        from naukri_server.tools.smart_apply import naukri_apply_top_fits
        result = await naukri_apply_top_fits(min_fit_score=70, limit=5)
        assert result["status"] == "success"
        mock_atf.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_min_fit_score_validation_error(self):
        from naukri_server.tools.smart_apply import naukri_assess_fit
        result = await naukri_assess_fit(job_id="J1", min_fit_score=150)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "min_fit_score" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs._fetch_match_score", new_callable=AsyncMock)
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_single_job_assessment(self, mock_get_job, mock_profile, mock_match):
        mock_get_job.return_value = _make_job_result()
        mock_profile.return_value = _make_profile()
        mock_match.return_value = None
        from naukri_server.tools.smart_apply import naukri_assess_fit
        result = await naukri_assess_fit(job_id="J1")
        assert result["status"] == "success"
        assert "fit_assessment" in result
        assert "job_summary" in result
        assert result["applied"] is False

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_single_job_fetch_failure(self, mock_get_job, mock_profile):
        mock_get_job.return_value = {"status": "error", "message": "Not found"}
        mock_profile.return_value = _make_profile()
        from naukri_server.tools.smart_apply import naukri_assess_fit
        result = await naukri_assess_fit(job_id="J_BAD")
        assert result["status"] == "error"
        assert "fetch job" in result["message"].lower()
