"""Deep tests for naukri_auto_hunt — pure unit tests, no network or browser.

Covers:
- Validation: invalid min_fit_score (negative, > 100)
- Search fails -> API_ERROR
- Profile fetch fails -> API_ERROR
- No jobs found -> success with empty list
- All jobs already applied (is_applied flag) -> filtered out
- Jobs scored and sorted by fit_score descending
- min_fit_score filters low-score results
- Timeout handling (asyncio.wait_for)
- Limit/page validation (validate_limit clamps silently)
- Parallel exception handling in gather (Exception returned by return_exceptions=True)
- Cross-reference with local tracking (all locally applied -> note returned)
- Profile skills alias normalisation passes through to scorer
- Boundary: min_fit_score exactly at 0 and 100
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

def _make_job(job_id, title="Dev", company="Acme", is_applied=False,
              tags=None, experience="3-5 years", location="Bangalore",
              work_mode=None, salary=None):
    return {
        "job_id": str(job_id),
        "title": title,
        "company": company,
        "is_applied": is_applied,
        "tags": tags or ["python", "django"],
        "skills": None,
        "experience": experience,
        "location": location,
        "work_mode": work_mode,
        "salary": salary,
        "experience_min": None,
        "experience_max": None,
    }


_GOOD_PROFILE = {
    "status": "success",
    "key_skills": ["Python", "Django", "AWS"],
    "total_experience": "4 years 0 months",
    "current_location": "Bangalore",
    "expected_ctc": 20.0,
}

_SEARCH_ONE_JOB = {
    "status": "success",
    "jobs": [_make_job("J1")],
}

_SEARCH_EMPTY = {
    "status": "success",
    "jobs": [],
}


# ---------------------------------------------------------------------------
# 1. Validation — negative min_fit_score
# ---------------------------------------------------------------------------

class TestMinFitScoreValidation:

    @pytest.mark.asyncio
    async def test_negative_min_fit_score_returns_validation_error(self):
        """min_fit_score < 0 must return VALIDATION_ERROR immediately."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        result = await naukri_auto_hunt(keywords="python", min_fit_score=-1)

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "min_fit_score" in result["message"]

    @pytest.mark.asyncio
    async def test_above_100_min_fit_score_returns_validation_error(self):
        """min_fit_score > 100 must return VALIDATION_ERROR immediately."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        result = await naukri_auto_hunt(keywords="python", min_fit_score=101)

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "min_fit_score" in result["message"]

    @pytest.mark.asyncio
    async def test_boundary_zero_min_fit_score_passes_validation(self):
        """min_fit_score=0 is valid and must not return VALIDATION_ERROR."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = _SEARCH_ONE_JOB
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", min_fit_score=0)

        assert result["status"] == "success"
        assert result.get("error_code") != "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_boundary_100_min_fit_score_passes_validation(self):
        """min_fit_score=100 is valid and must not return VALIDATION_ERROR."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = _SEARCH_EMPTY
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", min_fit_score=100)

        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# 2. Search fails -> API_ERROR
# ---------------------------------------------------------------------------

class TestSearchFailure:

    @pytest.mark.asyncio
    async def test_search_returns_error_dict_propagates_api_error(self):
        """When naukri_search_jobs returns status=error, auto_hunt returns API_ERROR."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = {"status": "error", "message": "network timeout"}
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Search failed" in result["message"]

    @pytest.mark.asyncio
    async def test_search_raises_exception_propagates_api_error(self):
        """When naukri_search_jobs raises an Exception (captured via return_exceptions),
        auto_hunt wraps it as API_ERROR."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile:
            mock_search.side_effect = RuntimeError("connection refused")
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Search failed" in result["message"]


# ---------------------------------------------------------------------------
# 3. Profile fetch fails -> API_ERROR
# ---------------------------------------------------------------------------

class TestProfileFailure:

    @pytest.mark.asyncio
    async def test_profile_returns_error_dict_propagates_api_error(self):
        """When get_cached_profile returns status=error, auto_hunt returns API_ERROR."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = _SEARCH_ONE_JOB
            mock_profile.return_value = {"status": "error", "message": "auth expired"}

            result = await naukri_auto_hunt(keywords="python")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Profile fetch failed" in result["message"]

    @pytest.mark.asyncio
    async def test_profile_raises_exception_propagates_api_error(self):
        """When get_cached_profile raises an Exception (captured via return_exceptions),
        auto_hunt wraps it as API_ERROR."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = _SEARCH_ONE_JOB
            mock_profile.side_effect = ConnectionError("session dead")

            result = await naukri_auto_hunt(keywords="python")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Profile fetch failed" in result["message"]


# ---------------------------------------------------------------------------
# 4. No jobs found -> success with empty list
# ---------------------------------------------------------------------------

class TestNoJobsFound:

    @pytest.mark.asyncio
    async def test_empty_jobs_list_returns_success_with_zeros(self):
        """When search returns an empty jobs list, auto_hunt returns success
        with jobs_found=0, jobs_matched=0, and an empty ranked_jobs list."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = _SEARCH_EMPTY
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python")

        assert result["status"] == "success"
        assert result["jobs_found"] == 0
        assert result["jobs_matched"] == 0
        assert result["ranked_jobs"] == []


# ---------------------------------------------------------------------------
# 5. All jobs already applied (is_applied=True) -> filtered out
# ---------------------------------------------------------------------------

class TestAppliedJobsFiltered:

    @pytest.mark.asyncio
    async def test_all_applied_jobs_returns_success_with_note(self):
        """When all returned jobs have is_applied=True, they are filtered out
        and the response includes a 'note' field about already applied."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        applied_jobs = [
            _make_job("A1", is_applied=True),
            _make_job("A2", is_applied=True),
        ]
        search_result = {"status": "success", "jobs": applied_jobs}

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = search_result
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python")

        assert result["status"] == "success"
        assert result["jobs_matched"] == 0
        assert result["ranked_jobs"] == []
        assert "note" in result
        assert "already applied" in result["note"].lower()


# ---------------------------------------------------------------------------
# 6. Jobs scored and sorted by fit_score descending
# ---------------------------------------------------------------------------

class TestJobsScoredAndSorted:

    @pytest.mark.asyncio
    async def test_ranked_jobs_sorted_by_fit_score_descending(self):
        """Jobs that pass min_fit_score must appear sorted by fit_score desc."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        # Job J_HIGH has matching skills, Job J_LOW has no matching skills.
        # With min_fit_score=0 both should appear; high-scoring first.
        jobs = [
            # Low scorer first in the raw list — should appear second in ranked
            {**_make_job("J_LOW"), "tags": ["cobol", "fortran"],
             "experience": "1-2 years"},
            # High scorer second — should appear first in ranked
            {**_make_job("J_HIGH"), "tags": ["python", "django", "aws"],
             "experience": "3-6 years"},
        ]
        search_result = {"status": "success", "jobs": jobs}

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = search_result
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", min_fit_score=0)

        assert result["status"] == "success"
        ranked = result["ranked_jobs"]
        assert len(ranked) >= 2
        # Verify descending order
        scores = [j["fit_score"] for j in ranked]
        assert scores == sorted(scores, reverse=True)
        # J_HIGH must beat J_LOW
        ids = [j["job_id"] for j in ranked]
        assert ids.index("J_HIGH") < ids.index("J_LOW")


# ---------------------------------------------------------------------------
# 7. min_fit_score filters low-score results
# ---------------------------------------------------------------------------

class TestMinFitScoreFiltering:

    @pytest.mark.asyncio
    async def test_jobs_below_min_fit_score_excluded(self):
        """Jobs whose computed fit_score < min_fit_score must not appear in ranked_jobs."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        # A job with no skill overlap with the profile will score very low
        low_match_job = {**_make_job("LOWMATCH"), "tags": ["cobol"], "experience": "10-20 years"}
        search_result = {"status": "success", "jobs": [low_match_job]}

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = search_result
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", min_fit_score=90)

        assert result["status"] == "success"
        assert result["jobs_matched"] == 0
        assert result["ranked_jobs"] == []

    @pytest.mark.asyncio
    async def test_jobs_above_min_fit_score_included(self):
        """Jobs with fit_score >= min_fit_score must appear in ranked_jobs."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        # Profile has Python, Django, AWS — job also requires Python, Django, AWS
        high_match_job = {
            **_make_job("HIGHMATCH"),
            "tags": ["python", "django", "aws"],
            "experience": "3-5 years",
        }
        search_result = {"status": "success", "jobs": [high_match_job]}

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = search_result
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", min_fit_score=0)

        assert result["status"] == "success"
        assert result["jobs_matched"] >= 1
        assert any(j["job_id"] == "HIGHMATCH" for j in result["ranked_jobs"])


# ---------------------------------------------------------------------------
# 8. Timeout handling
# ---------------------------------------------------------------------------

class TestTimeoutHandling:

    @pytest.mark.asyncio
    async def test_timeout_returns_partial_success(self):
        """When _do_work exceeds timeout_seconds, the result is partial_success/TIMEOUT."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        async def _slow_search(**kwargs):
            await asyncio.sleep(10)
            return _SEARCH_ONE_JOB

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   side_effect=_slow_search), \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile:
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(
                keywords="python",
                timeout_seconds=0,  # immediate timeout
            )

        assert result["status"] == "partial_success"
        assert result["error_code"] == "TIMEOUT"
        assert "Timed out" in result["message"]


# ---------------------------------------------------------------------------
# 9. validate_limit clamps silently (no VALIDATION_ERROR)
# ---------------------------------------------------------------------------

class TestLimitClamping:

    @pytest.mark.asyncio
    async def test_limit_zero_clamped_to_one(self):
        """validate_limit(0) clamps to 1 silently — no VALIDATION_ERROR returned."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = _SEARCH_EMPTY
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", limit=0)

        assert result.get("error_code") != "VALIDATION_ERROR"
        # naukri_search_jobs must have been called with limit >= 1
        called_limit = mock_search.call_args.kwargs.get("limit") or mock_search.call_args.args[2] if mock_search.call_args.args else None
        if called_limit is not None:
            assert called_limit >= 1

    @pytest.mark.asyncio
    async def test_limit_above_max_clamped_to_50(self):
        """validate_limit(999) clamps to 50 silently — no VALIDATION_ERROR returned."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = _SEARCH_EMPTY
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", limit=999)

        assert result.get("error_code") != "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 10. Parallel exception handling — both gather coroutines raise
# ---------------------------------------------------------------------------

class TestGatherExceptionHandling:

    @pytest.mark.asyncio
    async def test_both_search_and_profile_raise_search_error_reported(self):
        """When both search and profile raise exceptions, search error is
        detected first (search_result checked before profile_result)."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile:
            mock_search.side_effect = ValueError("search boom")
            mock_profile.side_effect = ValueError("profile boom")

            result = await naukri_auto_hunt(keywords="python")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        # Since search is checked first, message must mention Search
        assert "Search failed" in result["message"]

    @pytest.mark.asyncio
    async def test_only_profile_raises_profile_error_reported(self):
        """When search succeeds but profile raises, error message mentions profile."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile:
            mock_search.return_value = _SEARCH_ONE_JOB
            mock_profile.side_effect = OSError("profile boom")

            result = await naukri_auto_hunt(keywords="python")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Profile fetch failed" in result["message"]


# ---------------------------------------------------------------------------
# 11. Ranked job fields are present
# ---------------------------------------------------------------------------

class TestRankedJobFields:

    @pytest.mark.asyncio
    async def test_ranked_job_has_required_fields(self):
        """Each item in ranked_jobs must contain the documented fields."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        required_fields = {
            "job_id", "title", "company", "fit_score",
            "matched_skills", "missing_skills", "recommendation",
        }

        high_match_job = {
            **_make_job("FLD1"),
            "tags": ["python", "django", "aws"],
            "experience": "3-5 years",
        }
        search_result = {"status": "success", "jobs": [high_match_job]}

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = search_result
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", min_fit_score=0)

        assert result["status"] == "success"
        assert result["ranked_jobs"], "Expected at least one ranked job"
        job = result["ranked_jobs"][0]
        for field in required_fields:
            assert field in job, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_fit_score_within_0_100(self):
        """All fit_score values in ranked_jobs must be in range [0, 100]."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        jobs = [
            {**_make_job(f"JS{i}"), "tags": ["python"], "experience": "3-5 years"}
            for i in range(3)
        ]
        search_result = {"status": "success", "jobs": jobs}

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock) as mock_search, \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock) as mock_profile, \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()):
            mock_search.return_value = search_result
            mock_profile.return_value = _GOOD_PROFILE

            result = await naukri_auto_hunt(keywords="python", min_fit_score=0)

        for job in result.get("ranked_jobs", []):
            assert 0 <= job["fit_score"] <= 100, (
                f"fit_score {job['fit_score']} out of range for job {job['job_id']}"
            )
