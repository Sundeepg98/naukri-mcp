"""End-to-end smoke tests — exercise the LIVE Naukri API.

These are gated by @pytest.mark.e2e and SKIPPED by default. To run them:

    pytest -m e2e tests/test_e2e_smoke.py

They verify response shape hasn't drifted and basic round-trips work.
Each test asserts only the structural contract (status + key shape), not
specific data, so they remain stable as user data changes.
"""

import pytest

# Mark every test in this module as e2e — pytest.ini's `addopts = -m "not e2e"`
# means they're skipped by default unless invoked explicitly.
pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_profile_fetch_round_trip():
    """naukri_get_profile() returns a real profile dict with expected keys."""
    from naukri_server.tools.profile import _get_profile
    result = await _get_profile()
    assert result["status"] == "success"
    # Profile shape: must have at least name and skill data
    assert "name" in result or "key_skills" in result or "skills_with_experience" in result


@pytest.mark.asyncio
async def test_dashboard_fetch_round_trip():
    """naukri_dashboard() returns a real dashboard with profile views/activity."""
    from naukri_server.tools.profile import _get_dashboard
    result = await _get_dashboard()
    assert result["status"] == "success"
    # Dashboard shape: at least one of these top-level fields
    expected_any = {"profile_views", "ctc_lpa", "experience_years", "recruiter_activity_date"}
    assert expected_any & set(result.keys()), (
        f"Dashboard missing all expected fields {expected_any}; got {list(result.keys())[:10]}"
    )


@pytest.mark.asyncio
async def test_search_then_first_result_detail():
    """Search → first result → fetch its detail. Two-step round trip."""
    from naukri_server.tools.search import naukri_search_jobs
    from naukri_server.tools.jobs import _get_job

    search_result = await naukri_search_jobs(keywords="python", limit=5)
    assert search_result["status"] == "success"
    jobs = search_result.get("jobs", [])
    assert len(jobs) > 0, "Search returned zero jobs — Naukri API may have changed"
    first = jobs[0]
    assert "job_id" in first
    assert "title" in first

    # Fetch full detail for the first result
    detail = await _get_job(job_id_or_url=first["job_id"])
    assert detail["status"] == "success"
    assert "title" in detail
    assert "company" in detail


@pytest.mark.asyncio
async def test_settings_read_round_trip():
    """naukri_get_settings() returns the live formatted settings structure."""
    from naukri_server.tools.settings import _get_settings
    result = await _get_settings()
    assert result["status"] == "success"
    assert "settings" in result
    assert "count" in result
    # consent fields are present (booleans) when raw settings fetch succeeds
    if "naukri_auto_apply_consent" in result:
        assert isinstance(result["naukri_auto_apply_consent"], bool)


@pytest.mark.asyncio
async def test_taxonomy_resource_read():
    """naukri_taxonomy() returns the role hierarchy (37 dept × 167 cat × 1461 roles)."""
    from naukri_server.tools.insights import _get_taxonomy
    result = await _get_taxonomy()
    assert result["status"] == "success"
    assert result.get("total_departments", 0) > 0
    assert result.get("total_roles", 0) > 0
    assert isinstance(result.get("departments"), list)
    assert len(result["departments"]) > 0
    # Each department has a label
    first_dept = result["departments"][0]
    assert "label" in first_dept
