"""@mcp.resource() handler registrations.

Each handler reuses an existing tool helper to avoid duplicating fetch/cache logic.
Resources are read on-demand by the client, so any error is surfaced as a dict with
status/error_code (matching the tool error convention).

Test seam: tests patch the imported helpers (e.g. patch
'naukri_server.resources.handlers._get_taxonomy', ...) so the resource handlers
can be exercised without network/browser/filesystem access.
"""

from __future__ import annotations

from typing import Any

from naukri_server import mcp

# Import helpers under stable names so tests can monkey-patch this module.
from naukri_server.tools.insights import _get_taxonomy
from naukri_server.tools.settings import _get_settings
from naukri_server.tools.resume_builder import _get_templates
from naukri_server.services.profile_service import (
    get_cached_profile,
    get_cached_dashboard,
)
from naukri_server.domain.skill_taxonomy import DEFAULT_TAXONOMY


def _error(message: str, code: str) -> dict[str, Any]:
    """Build a standard error dict matching the tool error convention."""
    return {"status": "error", "message": message, "error_code": code}


@mcp.resource(
    "naukri://taxonomy",
    name="naukri_taxonomy",
    title="Naukri skill taxonomy",
    description=(
        "Canonical skill taxonomy used for fit scoring: "
        "88 canonical skills with 150+ aliases for case-insensitive normalization. "
        "Plus the live Naukri job-role taxonomy (37 departments, 167 role categories, 1461 roles) "
        "if available; the API call is best-effort and may be omitted."
    ),
    mime_type="application/json",
)
async def taxonomy_resource() -> dict[str, Any]:
    """Return the canonical skill taxonomy + the live job-role taxonomy.

    The skill taxonomy (DEFAULT_TAXONOMY) is always present and pure. The Naukri
    job-role taxonomy is fetched best-effort; on failure the resource still returns
    successfully with `job_roles: None` so clients can rely on the skill section.
    """
    skills = {
        "canonical_count": DEFAULT_TAXONOMY.canonical_count,
        "alias_count": DEFAULT_TAXONOMY.alias_count,
        # Sorted dict of canonical -> sorted alias list for deterministic output.
        "aliases": {
            canonical: sorted(aliases)
            for canonical, aliases in sorted(DEFAULT_TAXONOMY._aliases.items())
        },
    }

    job_roles: dict[str, Any] | None
    try:
        job_roles = await _get_taxonomy()
    except Exception as exc:  # pragma: no cover — network errors handled at runtime
        job_roles = {"status": "error", "message": str(exc), "error_code": "API_ERROR"}

    return {
        "status": "success",
        "skills": skills,
        "job_roles": job_roles,
    }


@mcp.resource(
    "naukri://profile",
    name="naukri_profile",
    title="Naukri profile snapshot",
    description=(
        "Cached Naukri profile (skills, employment, education, CTC, notice period). "
        "Returns the TtlCache value if fresh, otherwise triggers a fetch via the same "
        "code path as naukri_get_profile."
    ),
    mime_type="application/json",
)
async def profile_resource() -> dict[str, Any]:
    """Return the cached profile snapshot, fetching if cache is empty/stale."""
    try:
        return await get_cached_profile()
    except Exception as exc:
        return _error(f"Failed to load profile: {exc}", "API_ERROR")


@mcp.resource(
    "naukri://dashboard",
    name="naukri_dashboard",
    title="Naukri dashboard snapshot",
    description=(
        "Cached Naukri dashboard (profile views, recruiter activity, completeness, "
        "notifications). Returns the TtlCache value if fresh, otherwise triggers a "
        "fetch via the same code path as naukri_dashboard."
    ),
    mime_type="application/json",
)
async def dashboard_resource() -> dict[str, Any]:
    """Return the cached dashboard snapshot, fetching if cache is empty/stale."""
    try:
        return await get_cached_dashboard()
    except Exception as exc:
        return _error(f"Failed to load dashboard: {exc}", "API_ERROR")


@mcp.resource(
    "naukri://settings",
    name="naukri_settings",
    title="Naukri account settings",
    description=(
        "All current Naukri account settings (formatted + raw consent fields). "
        "Same payload as naukri_get_settings; sourced from the formatted settings API."
    ),
    mime_type="application/json",
)
async def settings_resource() -> dict[str, Any]:
    """Return the current Naukri account settings."""
    try:
        return await _get_settings()
    except Exception as exc:
        return _error(f"Failed to load settings: {exc}", "API_ERROR")


@mcp.resource(
    "naukri://resume/templates",
    name="naukri_resume_templates",
    title="Naukri resume builder templates",
    description=(
        "Catalog of available resume builder templates (free + pro variants with "
        "preview URLs). Same payload as naukri_resume_templates."
    ),
    mime_type="application/json",
)
async def resume_templates_resource() -> dict[str, Any]:
    """Return the resume builder template catalog."""
    try:
        return await _get_templates()
    except Exception as exc:
        return _error(f"Failed to load resume templates: {exc}", "API_ERROR")


# Public list for tests — keeps the registered URIs in one discoverable place.
RESOURCE_URIS: tuple[str, ...] = (
    "naukri://taxonomy",
    "naukri://profile",
    "naukri://dashboard",
    "naukri://settings",
    "naukri://resume/templates",
)
