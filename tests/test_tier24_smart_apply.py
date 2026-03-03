"""Tests for Tier 24: smart_apply module — scoring, bulk, apply_top_fits flows.

Every test is PURE: no network, no browser, no file I/O.
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
# 1. _score_job basic invocation
# ---------------------------------------------------------------------------

class TestScoreJob:
    def test_basic_score_job_returns_dict(self):
        from naukri_server.tools.smart_apply import _score_job
        job = _make_job_result()
        profile = _make_profile()
        result = _score_job(job, profile)
        assert isinstance(result, dict)
        assert "overall_score" in result
        assert "skill_match" in result
        assert "recommendation" in result

    def test_score_job_with_is_agent_eligible_false(self):
        """is_agent_eligible=False is the default — result is still a valid dict."""
        from naukri_server.tools.smart_apply import _score_job
        job = _make_job_result()
        profile = _make_profile()
        result = _score_job(job, profile, is_agent_eligible=False)
        assert 0 <= result["overall_score"] <= 100

    def test_score_job_uses_skills_key_fallback(self):
        """Falls back to 'skills' key when 'tags' is absent."""
        from naukri_server.tools.smart_apply import _score_job
        job = _make_job_result(tags=None)
        job.pop("tags", None)
        job["skills"] = ["Python"]
        profile = _make_profile()
        result = _score_job(job, profile)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 2. _bulk_saved_scoring — empty saved jobs
# ---------------------------------------------------------------------------

class TestBulkSavedScoring:
    @pytest.mark.asyncio
    async def test_empty_saved_jobs(self):
        """If no saved jobs exist, return early with empty scored_jobs."""
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
        """If profile fetch fails, return error."""
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
        with patch("naukri_server.tools.tracking._list_saved_jobs", new_callable=AsyncMock) as mock_saved:
            with patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
                with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_get_job:
                    mock_saved.return_value = _make_saved_jobs(2)
                    mock_profile.return_value = _make_profile()
                    # First job fails, second succeeds
                    mock_get_job.side_effect = [
                        Exception("timeout"),
                        _make_job_result("J1"),
                    ]

                    from naukri_server.tools.smart_apply import _bulk_saved_scoring
                    result = await _bulk_saved_scoring(min_fit_score=0, timeout_seconds=10)

        assert result["status"] == "success"
        # One succeeded, one skipped — errors may be recorded
        assert result["scored_count"] <= 1


# ---------------------------------------------------------------------------
# 3. _apply_top_fits — no jobs above threshold
# ---------------------------------------------------------------------------

class TestApplyTopFits:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_no_jobs_above_threshold(self, mock_bulk):
        """If no jobs meet min_fit_score, return success with applied=0."""
        mock_bulk.return_value = {
            "status": "success",
            "total_saved": 3,
            "scored_jobs": [],
        }
        from naukri_server.tools.smart_apply import _apply_top_fits
        result = await _apply_top_fits(min_fit_score=80)

        assert result["status"] == "success"
        assert result["applied"] == 0
        assert "No saved jobs" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_applies_top_n(self, mock_bulk, mock_apply):
        """Applies to top N jobs from scored list."""
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


# ---------------------------------------------------------------------------
# 4. naukri_smart_apply — action routing
# ---------------------------------------------------------------------------

class TestNaukriSmartApply:
    @pytest.mark.asyncio
    async def test_missing_job_id_returns_validation_error(self):
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(action=None, job_id=None)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_unknown_action_returns_validation_error(self):
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(action="nonexistent_action")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.smart_apply._bulk_saved_scoring", new_callable=AsyncMock)
    async def test_bulk_saved_action_routes(self, mock_bulk):
        mock_bulk.return_value = {"status": "success", "scored_jobs": [], "total_saved": 0, "scored_count": 0, "min_fit_score": 60}
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(action="bulk_saved", min_fit_score=60)
        assert result["status"] == "success"
        mock_bulk.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.smart_apply._apply_top_fits", new_callable=AsyncMock)
    async def test_apply_top_fits_action_routes(self, mock_atf):
        mock_atf.return_value = {"status": "success", "applied": 2}
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(action="apply_top_fits", min_fit_score=70, limit=5)
        assert result["status"] == "success"
        mock_atf.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_min_fit_score_validation_error(self):
        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(job_id="J1", min_fit_score=150)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "min_fit_score" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs._fetch_match_score", new_callable=AsyncMock)
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_single_job_assessment(self, mock_get_job, mock_profile, mock_match):
        """Single job assessment returns fit_assessment and job_summary."""
        mock_get_job.return_value = _make_job_result()
        mock_profile.return_value = _make_profile()
        mock_match.return_value = None

        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(job_id="J1")

        assert result["status"] == "success"
        assert "fit_assessment" in result
        assert "job_summary" in result
        assert result["applied"] is False

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_single_job_fetch_failure(self, mock_get_job, mock_profile):
        """Job fetch failure returns error."""
        mock_get_job.return_value = {"status": "error", "message": "Not found"}
        mock_profile.return_value = _make_profile()

        from naukri_server.tools.smart_apply import naukri_smart_apply
        result = await naukri_smart_apply(job_id="J_BAD")
        assert result["status"] == "error"
        assert "fetch job" in result["message"].lower()
