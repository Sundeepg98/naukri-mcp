"""Company tools — search, browse jobs, get slug, follow/unfollow."""

from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action
from naukri_server.interfaces import api_client  # noqa: F401 — tests patch via this module
from naukri_server.models import validate_action_params
from naukri_server.services.company_service import (
    parse_company as _parse_company,
    first as _first,
    extract_slug_from_url as _extract_slug_from_url,
    search_companies as _search_companies,
    get_company_jobs as _get_company_jobs,
    get_company_slug as _get_company_slug,
    batch_get_slugs as _batch_get_slugs,
    batch_follow_status as _batch_follow_status,
    get_follow_status as _get_follow_status,
    follow_or_unfollow as _follow_or_unfollow,
)
from naukri_server.utils import derive_slug


# ---------------------------------------------------------------------------
# Company registry — maps action to handler(kwargs) -> dict
# ---------------------------------------------------------------------------

def _make_research_handler(**kw):
    from naukri_server.tools.research import _research_company
    return _research_company(
        keyword=kw["keyword"], include_jobs=kw.get("include_jobs", True),
        include_reviews=kw.get("include_reviews", True),
        include_interviews=kw.get("include_interviews", True),
        jobs_limit=kw.get("jobs_limit", 5),
        timeout_seconds=kw.get("timeout_seconds", 120),
    )

async def _do_follow_action(**kw) -> dict:
    """Dispatch follow_status/follow/unfollow with multi-ID support."""
    action = kw["_action"]
    ids = kw.get("group_ids") or ([kw.get("group_id")] if kw.get("group_id") else [])
    if not ids:
        return {"status": "error", "message": "group_id or group_ids is required for follow actions.", "error_code": "VALIDATION_ERROR"}

    if action == "follow_status":
        return await (_batch_follow_status(ids) if len(ids) > 1 else _get_follow_status(ids))
    return await _follow_or_unfollow(ids, action)


_COMPANY_REGISTRY: dict[str, callable] = {
    "search": lambda **kw: _search_companies(keyword=kw["keyword"], page=kw.get("page", 1), limit=kw.get("limit", 20)),
    "jobs": lambda **kw: _get_company_jobs(group_id=kw["group_id"], page=kw.get("page", 1), limit=kw.get("limit", 20)),
    "slug": lambda **kw: _get_company_slug(group_id=kw["group_id"]),
    "batch_slugs": lambda **kw: _batch_get_slugs([g.strip() for g in kw["group_id"].split(",") if g.strip()]),
    "research": _make_research_handler,
    "follow_status": _do_follow_action,
    "follow": _do_follow_action,
    "unfollow": _do_follow_action,
}

_VALID_PARAMS_PER_ACTION = {
    "search": {"keyword", "page", "limit"},
    "jobs": {"group_id", "page", "limit"},
    "slug": {"group_id"},
    "batch_slugs": {"group_id"},
    "research": {"keyword", "include_jobs", "include_reviews", "include_interviews",
                 "jobs_limit", "timeout_seconds"},
    "follow_status": {"group_id", "group_ids"},
    "follow": {"group_id", "group_ids"},
    "unfollow": {"group_id", "group_ids"},
}


# ---------------------------------------------------------------------------
# Unified MCP tool — company discovery (search, jobs, slug)
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_company(
    action: str = "search",
    keyword: Optional[str] = None,
    group_id: Optional[str] = None,
    group_ids: Optional[list[str]] = None,
    page: int = 1,
    limit: int = 20,
    # research params
    include_jobs: bool = True,
    include_reviews: bool = True,
    include_interviews: bool = True,
    jobs_limit: int = 5,
    timeout_seconds: int = 120,
) -> dict:
    """Unified company discovery — search companies, browse jobs, get AmbitionBox slug, follow/unfollow, or research a company.

    Actions:
      - "search": Search companies by name/industry (requires keyword)
      - "jobs": Get job listings for a specific company (requires group_id)
      - "slug": Convert group_id to AmbitionBox company slug (requires group_id)
      - "batch_slugs": Convert multiple group_ids to AmbitionBox slugs (requires group_ids as comma-separated string)
      - "research": Comprehensive company research — jobs, salary, reviews, interviews (requires keyword)
      - "follow_status": Check follow status for companies (requires group_id or group_ids)
      - "follow": Follow companies (requires group_id or group_ids)
      - "unfollow": Unfollow companies (requires group_id or group_ids)

    Args:
        action: "search" | "jobs" | "slug" | "batch_slugs" | "research" | "follow_status" | "follow" | "unfollow"
        keyword: Company name or industry keyword (required for search, research)
        group_id: Company group ID from search results (required for jobs, slug; also works for follow actions)
        group_ids: List of company group IDs for batch follow actions
        page: Page number for pagination (default 1, used by search/jobs)
        limit: Max results per page (default 20, used by search/jobs)
        include_jobs: Include open job listings (default True, research only)
        include_reviews: Include AmbitionBox salary/review data (default True, research only)
        include_interviews: Include AmbitionBox interview experiences (default True, research only)
        jobs_limit: Max jobs to include (default 5, research only)
        timeout_seconds: Max seconds before timeout (default 120, research only)

    Returns:
        - search: {status, keyword, page, total, count, companies: [...]}
        - jobs: {status, group_id, page, total, count, jobs: [...]}
        - slug: {status, group_id, company_slug, company_name}
        - batch_slugs: {status, slugs: {group_id: slug_or_null, ...}, count}
        - research: {status, company_name, slug, jobs, salary, reviews, interviews, errors?}
        - follow_status (single): {status, followed: [...ids...], not_followed: [...ids...]}
        - follow_status (batch): {status, total, followed_count, unfollowed_count, companies: [{group_id, name, follower_count, is_followed, logo}]}
        - follow/unfollow: {status, action, followed/unfollowed: [...ids...], errors?}
        - {status: "error", message} on failure
    """
    # ── ISP: warn about params irrelevant to chosen action ─────────────
    _provided = {
        "keyword": keyword, "group_id": group_id, "group_ids": group_ids,
        "page": page if page != 1 else None, "limit": limit if limit != 20 else None,
        "include_jobs": include_jobs if not include_jobs else None,
        "include_reviews": include_reviews if not include_reviews else None,
        "include_interviews": include_interviews if not include_interviews else None,
        "jobs_limit": jobs_limit if jobs_limit != 5 else None,
        "timeout_seconds": timeout_seconds if timeout_seconds != 120 else None,
    }
    _unused = validate_action_params(action, _provided, _VALID_PARAMS_PER_ACTION)

    def _attach_unused(result: dict) -> dict:
        if _unused and isinstance(result, dict):
            result["unused_params"] = _unused
        return result

    # ── Pre-validation ──────────────────────────────────────────────────
    if action in ("search", "research") and not keyword:
        return {"status": "error", "message": f"keyword is required for action='{action}'.", "error_code": "VALIDATION_ERROR"}
    if action in ("jobs", "slug", "batch_slugs") and not group_id:
        return {"status": "error", "message": f"group_id is required for action='{action}'.", "error_code": "VALIDATION_ERROR"}
    if action == "batch_slugs":
        ids = [g.strip() for g in group_id.split(",") if g.strip()]
        if not ids:
            return {"status": "error", "message": "No valid group IDs provided.", "error_code": "VALIDATION_ERROR"}

    # ── Registry lookup ──────────────────────────────────────────────
    handler = _COMPANY_REGISTRY.get(action)
    if handler:
        kw = {
            "_action": action,
            "keyword": keyword, "group_id": group_id, "group_ids": group_ids,
            "page": page, "limit": limit,
            "include_jobs": include_jobs, "include_reviews": include_reviews,
            "include_interviews": include_interviews, "jobs_limit": jobs_limit,
            "timeout_seconds": timeout_seconds,
        }
        return _attach_unused(await handle_tool_action(lambda: handler(**kw), f"company.{action}"))

    return {
        "status": "error",
        "message": f"Unknown action '{action}'. Use: {', '.join(_COMPANY_REGISTRY)}",
        "error_code": "VALIDATION_ERROR",
    }


# ---------------------------------------------------------------------------
# Legacy follow tool — kept as private helper, no longer registered as MCP tool
# ---------------------------------------------------------------------------

async def _company_follow(
    action: str,
    group_ids: list[str] = [],
) -> dict:
    """Unified company follow management — check status, follow, or unfollow.

    Requires: group_ids from naukri_company(action="search") results.

    Actions:
      - "status": Check follow status for given company IDs
      - "follow": Follow one or more companies
      - "unfollow": Unfollow one or more companies

    Args:
        action: "status" | "follow" | "unfollow"
        group_ids: List of company group IDs (from naukri_company search results)

    Returns:
        - status: {status, followed: [...ids...], not_followed: [...ids...]}
        - follow/unfollow: {status, action, followed/unfollowed: [...ids...], errors?}
        - {status: "error", message} on failure
    """
    # ── validate group_ids ────────────────────────────────────────────
    if not group_ids:
        return {"status": "error", "message": "group_ids is required and must not be empty.", "error_code": "VALIDATION_ERROR"}

    # ── status ────────────────────────────────────────────────────────
    if action == "status":
        return await handle_tool_action(lambda: _get_follow_status(group_ids), "company_follow.status")

    # ── follow / unfollow ─────────────────────────────────────────────
    elif action in ("follow", "unfollow"):
        return await handle_tool_action(lambda: _follow_or_unfollow(group_ids, action), f"company_follow.{action}")

    # ── unknown action ────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: status, follow, unfollow", "error_code": "VALIDATION_ERROR"}
