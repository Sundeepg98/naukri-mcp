"""Deep unit tests for naukri_server.tools.skill_gap — _skill_gap_analysis.

Every test is PURE: no network, no browser, no file I/O.
Uses unittest.mock.patch with AsyncMock for async helpers.
Patches at the source module (where functions are defined).
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _make_jobs(*skill_lists, base_title="Job", base_company="Co"):
    """Return a list of minimal job dicts, one per skill list."""
    jobs = []
    for idx, skills in enumerate(skill_lists):
        jobs.append({
            "title": f"{base_title} {idx + 1}",
            "company": f"{base_company} {idx + 1}",
            "tags": list(skills),
        })
    return jobs


def _jobs_result(jobs):
    return {"status": "success", "jobs": jobs}


def _profile_result(key_skills=None, skills_with_experience=None):
    return {
        "status": "success",
        "key_skills": key_skills or [],
        "skills_with_experience": skills_with_experience or [],
    }


def _assessments_result(assessments=None):
    return {"status": "success", "assessments": assessments or []}


def _error_result(message="error", code="API_ERROR"):
    return {"status": "error", "message": message, "error_code": code}


# ===========================================================================
# 1. Validation: keywords required when use_recommendations=False
# ===========================================================================

@pytest.mark.asyncio
async def test_keywords_required_when_not_using_recommendations():
    """use_recommendations=False without keywords → VALIDATION_ERROR immediately."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    result = await _skill_gap_analysis(use_recommendations=False, keywords=None)

    assert result["status"] == "error"
    assert result["error_code"] == "VALIDATION_ERROR"
    assert "keywords" in result["message"].lower()


@pytest.mark.asyncio
async def test_keywords_empty_string_when_not_using_recommendations():
    """Empty-string keywords also triggers VALIDATION_ERROR (falsy)."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    result = await _skill_gap_analysis(use_recommendations=False, keywords="")

    assert result["status"] == "error"
    assert result["error_code"] == "VALIDATION_ERROR"


# ===========================================================================
# 2. Source selection — recommendations vs search
# ===========================================================================

@pytest.mark.asyncio
async def test_uses_get_recommendations_when_enabled():
    """use_recommendations=True → naukri_get_recommendations is called, not search."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    mock_recs = AsyncMock(return_value=_jobs_result(_make_jobs(["python", "django"])))
    mock_search = AsyncMock(return_value=_jobs_result([]))
    mock_profile = AsyncMock(return_value=_profile_result(["python"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.search.naukri_search_jobs", mock_search), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    mock_recs.assert_called_once()
    mock_search.assert_not_called()
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_uses_search_when_recommendations_disabled():
    """use_recommendations=False → naukri_search_jobs is called, not recommendations."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    mock_recs = AsyncMock(return_value=_jobs_result([]))
    mock_search = AsyncMock(return_value=_jobs_result(_make_jobs(["python", "fastapi"])))
    mock_profile = AsyncMock(return_value=_profile_result(["python"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.search.naukri_search_jobs", mock_search), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(
            use_recommendations=False,
            keywords="python developer",
            include_assessments=True,
        )

    mock_search.assert_called_once()
    mock_recs.assert_not_called()
    assert result["status"] == "success"


# ===========================================================================
# 3. Error paths: jobs fetch fails
# ===========================================================================

@pytest.mark.asyncio
async def test_jobs_fetch_error_dict_returns_api_error():
    """When jobs fetch returns {status: error}, result is API_ERROR."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    mock_recs = AsyncMock(return_value=_error_result("upstream timeout"))
    mock_profile = AsyncMock(return_value=_profile_result(["python"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "error"
    assert result["error_code"] == "API_ERROR"
    assert "fetch jobs" in result["message"].lower()


@pytest.mark.asyncio
async def test_jobs_fetch_exception_returns_api_error():
    """When jobs fetch raises an Exception, result is API_ERROR."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    mock_recs = AsyncMock(side_effect=RuntimeError("connection reset"))
    mock_profile = AsyncMock(return_value=_profile_result(["python"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "error"
    assert result["error_code"] == "API_ERROR"


# ===========================================================================
# 4. Error paths: profile fetch fails
# ===========================================================================

@pytest.mark.asyncio
async def test_profile_fetch_error_dict_returns_api_error():
    """When profile fetch returns {status: error}, result is API_ERROR."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    jobs = _make_jobs(["python", "django"])
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_error_result("profile not found"))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "error"
    assert result["error_code"] == "API_ERROR"
    assert "fetch profile" in result["message"].lower()


@pytest.mark.asyncio
async def test_profile_fetch_exception_returns_api_error():
    """When profile fetch raises an Exception, result is API_ERROR."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    jobs = _make_jobs(["python"])
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(side_effect=ConnectionError("auth expired"))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "error"
    assert result["error_code"] == "API_ERROR"


# ===========================================================================
# 5. No jobs found → NOT_FOUND
# ===========================================================================

@pytest.mark.asyncio
async def test_no_jobs_found_returns_not_found():
    """Empty jobs list → NOT_FOUND error."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    mock_recs = AsyncMock(return_value={"status": "success", "jobs": []})
    mock_profile = AsyncMock(return_value=_profile_result(["python"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "error"
    assert result["error_code"] == "NOT_FOUND"
    assert "no jobs" in result["message"].lower()


# ===========================================================================
# 6. sample_size clamped at 50
# ===========================================================================

@pytest.mark.asyncio
async def test_sample_size_clamped_at_50():
    """sample_size > 50 is silently clamped to 50 (passed as limit to the fetcher)."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    jobs = _make_jobs(["python"])
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result(["python"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        await _skill_gap_analysis(
            use_recommendations=True,
            sample_size=999,
            include_assessments=True,
        )

    # naukri_get_recommendations must receive limit=50 (clamped), not 999
    call_kwargs = mock_recs.call_args
    limit_passed = call_kwargs.kwargs.get("limit") or (call_kwargs.args[0] if call_kwargs.args else None)
    assert limit_passed == 50


# ===========================================================================
# 7. Missing skills counted by frequency across jobs
# ===========================================================================

@pytest.mark.asyncio
async def test_missing_skills_counted_by_frequency():
    """Skills absent from profile are counted across all jobs and sorted by frequency."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    # Profile has only "python"
    # Job 1: python (matched), docker, kubernetes (both missing)
    # Job 2: python (matched), docker (missing), react (missing)
    # Job 3: python (matched), docker (missing)
    # → docker missing 3x, kubernetes missing 1x, react missing 1x
    jobs = _make_jobs(
        ["python", "docker", "kubernetes"],
        ["python", "docker", "react"],
        ["python", "docker"],
    )
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result(["python"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "success"
    assert result["jobs_analyzed"] == 3

    gaps = result["skill_gaps"]
    assert len(gaps) >= 1

    # docker should be the most common gap (frequency=3)
    gap_map = {g["skill"]: g["frequency"] for g in gaps}
    assert "docker" in gap_map
    assert gap_map["docker"] == 3

    # Gaps must be sorted descending by frequency
    frequencies = [g["frequency"] for g in gaps]
    assert frequencies == sorted(frequencies, reverse=True)


# ===========================================================================
# 8. Matched skills counted correctly
# ===========================================================================

@pytest.mark.asyncio
async def test_matched_skills_counted_correctly():
    """Skills present in profile are accumulated in strong_skills by frequency."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    # Profile has python and django
    # Job 1: python, django, react  → matched: python, django
    # Job 2: python, react          → matched: python
    # → python matched 2x, django matched 1x
    jobs = _make_jobs(
        ["python", "django", "react"],
        ["python", "react"],
    )
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result(["python", "django"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "success"
    strong = {s["skill"]: s["frequency"] for s in result["strong_skills"]}
    assert strong.get("python") == 2
    assert strong.get("django") == 1


# ===========================================================================
# 9. Assessment boost: 2x for passed skills
# ===========================================================================

@pytest.mark.asyncio
async def test_assessment_boost_doubles_matched_frequency():
    """Passed assessment skills have their matched frequency doubled."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    # Profile: python, django
    # Both jobs match python and django
    # Passed assessment: python → its frequency (2) becomes 4
    jobs = _make_jobs(
        ["python", "django"],
        ["python", "django"],
    )
    assessments = [
        {"skill": "Python", "status": "passed"},
    ]
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result(["python", "django"]))
    mock_assessments = AsyncMock(return_value=_assessments_result(assessments))

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "success"
    assert result["assessments_used"] == 1

    strong = {s["skill"]: s["frequency"] for s in result["strong_skills"]}
    # python matched 2 jobs, boosted 2x → 4
    assert strong.get("python") == 4
    # django not boosted → stays at 2
    assert strong.get("django") == 2


# ===========================================================================
# 10. Skill gaps sorted by frequency (most_common)
# ===========================================================================

@pytest.mark.asyncio
async def test_skill_gaps_sorted_descending_by_frequency():
    """skill_gaps list is ordered from highest to lowest frequency."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    # Profile empty → all job skills are gaps
    # docker appears in 3 jobs, react in 2, kubernetes in 1
    jobs = _make_jobs(
        ["docker", "react"],
        ["docker", "react"],
        ["docker", "kubernetes"],
    )
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result([]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "success"
    freqs = [g["frequency"] for g in result["skill_gaps"]]
    assert freqs == sorted(freqs, reverse=True), "skill_gaps not sorted by frequency desc"
    assert freqs[0] == 3  # docker


# ===========================================================================
# 11. Strong skills sorted by frequency
# ===========================================================================

@pytest.mark.asyncio
async def test_strong_skills_sorted_descending_by_frequency():
    """strong_skills list is ordered from highest to lowest frequency."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    # Profile: python, django, react
    # python appears in all 3, django in 2, react in 1
    jobs = _make_jobs(
        ["python", "django"],
        ["python", "django"],
        ["python", "react"],
    )
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result(["python", "django", "react"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "success"
    freqs = [s["frequency"] for s in result["strong_skills"]]
    assert freqs == sorted(freqs, reverse=True), "strong_skills not sorted by frequency desc"
    assert freqs[0] == 3  # python


# ===========================================================================
# 12. assessment_passed flag on strong skills
# ===========================================================================

@pytest.mark.asyncio
async def test_assessment_passed_flag_set_on_strong_skills():
    """strong_skills entries for passed-assessment skills carry assessment_passed=True."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    jobs = _make_jobs(["python", "django"])
    assessments = [
        {"skill": "python", "status": "passed"},
    ]
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result(["python", "django"]))
    mock_assessments = AsyncMock(return_value=_assessments_result(assessments))

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "success"
    strong_map = {s["skill"]: s for s in result["strong_skills"]}
    assert strong_map["python"].get("assessment_passed") is True
    # django NOT in passed_skills → no assessment_passed key
    assert "assessment_passed" not in strong_map.get("django", {})


# ===========================================================================
# 13. Experience depth from profile exp_map
# ===========================================================================

@pytest.mark.asyncio
async def test_experience_depth_populated_for_strong_skills():
    """your_experience_years is set from profile skills_with_experience."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    jobs = _make_jobs(["python"])
    skills_with_exp = [
        {"skill": "python", "experience_years": 4, "experience_months": 6},
    ]
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result(
        key_skills=["python"],
        skills_with_experience=skills_with_exp,
    ))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "success"
    python_entry = next(
        (s for s in result["strong_skills"] if s["skill"] == "python"), None
    )
    assert python_entry is not None
    # 4 years + 6 months = 4.5 years
    assert abs(python_entry["your_experience_years"] - 4.5) < 0.01


# ===========================================================================
# 14. Assessments fetch optional / non-fatal
# ===========================================================================

@pytest.mark.asyncio
async def test_assessments_failure_is_non_fatal():
    """When assessments fetch raises an exception, analysis still succeeds."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    jobs = _make_jobs(["python", "docker"])
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result(["python"]))
    # Assessments raise — must be non-fatal
    mock_assessments = AsyncMock(side_effect=RuntimeError("assessment service down"))

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=True)

    assert result["status"] == "success"
    # No assessments means no boost — assessments_used should be 0
    assert result["assessments_used"] == 0


@pytest.mark.asyncio
async def test_include_assessments_false_skips_assessment_fetch():
    """When include_assessments=False, _list_assessments is never called."""
    from naukri_server.tools.skill_gap import _skill_gap_analysis

    jobs = _make_jobs(["python"])
    mock_recs = AsyncMock(return_value=_jobs_result(jobs))
    mock_profile = AsyncMock(return_value=_profile_result(["python"]))
    mock_assessments = AsyncMock(return_value=_assessments_result())

    with patch("naukri_server.tools.search.naukri_get_recommendations", mock_recs), \
         patch("naukri_server.tools.profile.get_cached_profile", mock_profile), \
         patch("naukri_server.tools.assessments._list_assessments", mock_assessments):
        result = await _skill_gap_analysis(use_recommendations=True, include_assessments=False)

    assert result["status"] == "success"
    mock_assessments.assert_not_called()
    assert result["assessments_used"] == 0
