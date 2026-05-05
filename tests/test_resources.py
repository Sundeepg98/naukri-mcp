"""Tests for @mcp.resource() handlers in naukri_server.resources.handlers.

Pure unit tests — every external dependency (taxonomy fetch, profile cache,
dashboard cache, settings API, resume-builder API) is mocked. No network,
no browser, no file I/O.

Each registered resource has:
  1. A registration test (URI present in the FastMCP resource manager).
  2. A happy-path read test (mocks the underlying helper, verifies shape).
  3. An error-path test (helper raises -> resource returns status=error).

The 5 resources are also asserted as a group in test_all_five_registered.
"""

import json

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server import mcp
from naukri_server.resources.handlers import RESOURCE_URIS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registered_uris() -> set[str]:
    """Return the set of registered resource URIs as strings."""
    return {str(r.uri) for r in mcp._resource_manager.list_resources()}


async def _read_resource(uri: str) -> dict:
    """Fetch the resource via the FastMCP resource manager and decode JSON."""
    resource = await mcp._resource_manager.get_resource(uri)
    assert resource is not None, f"Resource {uri} not registered"
    raw = await resource.read()
    # FunctionResource serializes non-str/bytes returns to JSON via pydantic_core.
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


# ===========================================================================
# Group test — all 5 resources registered
# ===========================================================================


def test_all_five_registered():
    """All 5 expected resources must be registered after package import."""
    registered = _registered_uris()
    expected = {
        "naukri://taxonomy",
        "naukri://profile",
        "naukri://dashboard",
        "naukri://settings",
        "naukri://resume/templates",
    }
    missing = expected - registered
    assert not missing, f"Missing resources: {missing}. Registered: {registered}"
    # RESOURCE_URIS public constant must agree with what's actually wired.
    assert set(RESOURCE_URIS) == expected


# ===========================================================================
# 1. naukri://taxonomy
# ===========================================================================


@pytest.mark.asyncio
async def test_taxonomy_resource_returns_skill_taxonomy_with_job_roles():
    """Taxonomy resource exposes the canonical SkillTaxonomy + live job roles."""
    fake_job_roles = {
        "status": "success",
        "total_departments": 37,
        "total_roles": 1461,
        "departments": [],
    }
    with patch(
        "naukri_server.resources.handlers._get_taxonomy",
        new=AsyncMock(return_value=fake_job_roles),
    ):
        payload = await _read_resource("naukri://taxonomy")

    assert payload["status"] == "success"
    # Skill section: deterministic, sourced from DEFAULT_TAXONOMY (no mocking).
    skills = payload["skills"]
    assert skills["canonical_count"] >= 1
    assert skills["alias_count"] >= 1
    assert isinstance(skills["aliases"], dict)
    # Aliases must be sorted lists for determinism.
    sample_canonical = next(iter(skills["aliases"]))
    assert isinstance(skills["aliases"][sample_canonical], list)
    # Job roles section round-trips the mocked value.
    assert payload["job_roles"] == fake_job_roles


# ===========================================================================
# 2. naukri://profile
# ===========================================================================


@pytest.mark.asyncio
async def test_profile_resource_returns_cached_profile():
    """Profile resource returns whatever get_cached_profile yields."""
    fake_profile = {
        "status": "success",
        "name": "Test User",
        "key_skills": ["Node.js", "TypeScript"],
        "total_experience": "5 years 0 months",
        "expected_ctc": "25",
    }
    with patch(
        "naukri_server.resources.handlers.get_cached_profile",
        new=AsyncMock(return_value=fake_profile),
    ):
        payload = await _read_resource("naukri://profile")

    assert payload == fake_profile


@pytest.mark.asyncio
async def test_profile_resource_handles_helper_exception():
    """If the cached-profile helper raises, the resource returns a structured error."""
    with patch(
        "naukri_server.resources.handlers.get_cached_profile",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        payload = await _read_resource("naukri://profile")

    assert payload["status"] == "error"
    assert payload["error_code"] == "API_ERROR"
    assert "boom" in payload["message"]


# ===========================================================================
# 3. naukri://dashboard
# ===========================================================================


@pytest.mark.asyncio
async def test_dashboard_resource_returns_cached_dashboard():
    """Dashboard resource returns the cached dashboard payload."""
    fake_dash = {
        "status": "success",
        "profile_views": 42,
        "ctc_lpa": 25.0,
        "experience_years": 5,
    }
    with patch(
        "naukri_server.resources.handlers.get_cached_dashboard",
        new=AsyncMock(return_value=fake_dash),
    ):
        payload = await _read_resource("naukri://dashboard")

    assert payload == fake_dash


# ===========================================================================
# 4. naukri://settings
# ===========================================================================


@pytest.mark.asyncio
async def test_settings_resource_returns_settings_payload():
    """Settings resource returns the formatted-settings payload."""
    fake_settings = {
        "status": "success",
        "count": 2,
        "settings": [
            {"section": "comm", "id": "1", "label": "Recruiter notifications", "value": "1"},
            {"section": "comm", "id": "2", "label": "Promotional", "value": "0"},
        ],
        "naukri_auto_apply_consent": True,
    }
    with patch(
        "naukri_server.resources.handlers._get_settings",
        new=AsyncMock(return_value=fake_settings),
    ):
        payload = await _read_resource("naukri://settings")

    assert payload == fake_settings


# ===========================================================================
# 5. naukri://resume/templates
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_templates_resource_returns_template_catalog():
    """Resume templates resource returns the template catalog payload."""
    fake_templates = {
        "status": "success",
        "count": 3,
        "free_count": 1,
        "pro_count": 2,
        "templates": [
            {"id": "t1", "name": "Classic", "type": "free", "preview_url": "https://x/p1"},
            {"id": "t2", "name": "Modern", "type": "pro", "preview_url": "https://x/p2"},
            {"id": "t3", "name": "Bold", "type": "pro", "preview_url": "https://x/p3"},
        ],
    }
    with patch(
        "naukri_server.resources.handlers._get_templates",
        new=AsyncMock(return_value=fake_templates),
    ):
        payload = await _read_resource("naukri://resume/templates")

    assert payload == fake_templates
