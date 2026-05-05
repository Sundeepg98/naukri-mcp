"""Company tools — search, browse jobs, get slug, follow/unfollow.

Tool layer — all business logic lives in
``services/company_service.py`` (uses ``safe_get``, ``Salary`` domain
object, and the ``api_client`` interface). This module is a thin
``@mcp.tool()`` registry plus back-compat re-exports for
test patches at ``naukri_server.tools.companies._*``.
"""

from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action
from naukri_server.interfaces import api_client  # noqa: F401 — tests patch via this module
from naukri_server.services.company_service import (  # noqa: F401
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
from naukri_server.utils import derive_slug  # noqa: F401 — back-compat re-export

__all__ = [
    "_parse_company",
    "_first",
    "_extract_slug_from_url",
    "_search_companies",
    "_get_company_jobs",
    "_get_company_slug",
    "_batch_get_slugs",
    "_batch_follow_status",
    "_get_follow_status",
    "_follow_or_unfollow",
    "derive_slug",
]


# ---------------------------------------------------------------------------
# Single-purpose MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_search_companies(
    keyword: str,
    page: int = 1,
    limit: int = 10,
) -> dict:
    """Search for companies by name or industry keyword.

    Args:
        keyword: Company name or industry keyword (e.g. "Google", "fintech")
        page: Page number (default 1)
        limit: Max results per page (default 10, max 50)

    Returns:
        {status, keyword, page, total, count, companies: [{group_id, name, ...}]}
    """
    if not keyword:
        return {"status": "error", "message": "keyword is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _search_companies(keyword=keyword, page=page, limit=limit),
        "search_companies",
    )


@mcp.tool()
async def naukri_company_jobs(
    group_id: str,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """Get job listings at a specific company.

    Args:
        group_id: Company group ID from search results
        page: Page number (default 1)
        limit: Max results per page (default 20, max 50)

    Returns:
        {status, group_id, page, total, count, jobs: [...]}
    """
    if not group_id:
        return {"status": "error", "message": "group_id is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _get_company_jobs(group_id=group_id, page=page, limit=limit),
        "company_jobs",
    )


@mcp.tool()
async def naukri_company_slug(
    group_id: str,
) -> dict:
    """Get the AmbitionBox slug for a company. Supports batch via comma-separated group_ids.

    Args:
        group_id: Single group ID or comma-separated IDs (e.g. "123" or "123,456,789")

    Returns:
        Single: {status, group_id, company_slug, company_name}
        Batch:  {status, slugs: {group_id: slug_or_null, ...}, count}
    """
    if not group_id:
        return {"status": "error", "message": "group_id is required.", "error_code": "VALIDATION_ERROR"}
    ids = [g.strip() for g in group_id.split(",") if g.strip()]
    if not ids:
        return {"status": "error", "message": "No valid group IDs provided.", "error_code": "VALIDATION_ERROR"}
    if len(ids) == 1:
        return await handle_tool_action(
            lambda: _get_company_slug(group_id=ids[0]),
            "company_slug",
        )
    return await handle_tool_action(
        lambda: _batch_get_slugs(ids),
        "company_slug.batch",
    )


@mcp.tool()
async def naukri_research_company(
    keyword: str,
    include_jobs: bool = True,
    include_reviews: bool = True,
    include_interviews: bool = True,
    jobs_limit: int = 5,
    timeout_seconds: int = 120,
) -> dict:
    """Comprehensive company research — jobs, salary, reviews, and interview experiences.

    Args:
        keyword: Company name (e.g. "Google", "Infosys")
        include_jobs: Include open job listings (default True)
        include_reviews: Include AmbitionBox salary/review data (default True)
        include_interviews: Include AmbitionBox interview experiences (default True)
        jobs_limit: Max jobs to include (default 5)
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        {status, company_name, slug, jobs?, salary?, reviews?, interviews?, errors?}
    """
    if not keyword:
        return {"status": "error", "message": "keyword is required.", "error_code": "VALIDATION_ERROR"}
    from naukri_server.services.company_service import research_company as _svc_research
    return await handle_tool_action(
        lambda: _svc_research(
            keyword=keyword,
            include_jobs=include_jobs,
            include_reviews=include_reviews,
            include_interviews=include_interviews,
            jobs_limit=jobs_limit,
            timeout_seconds=timeout_seconds,
        ),
        "research_company",
    )


@mcp.tool()
async def naukri_follow_company(
    group_id: Optional[str] = None,
    group_ids: Optional[list[str]] = None,
    action: str = "follow",
) -> dict:
    """Follow or unfollow companies on Naukri.

    Args:
        group_id: Single company group ID
        group_ids: List of company group IDs for batch operations
        action: "follow" or "unfollow" (default "follow")

    Returns:
        {status, action, followed/unfollowed: [...ids...], errors?}
    """
    if action not in ("follow", "unfollow"):
        return {"status": "error", "message": f"action must be 'follow' or 'unfollow', got '{action}'.", "error_code": "VALIDATION_ERROR"}
    ids = group_ids or ([group_id] if group_id else [])
    if not ids:
        return {"status": "error", "message": "group_id or group_ids is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _follow_or_unfollow(ids, action),
        f"follow_company.{action}",
    )


@mcp.tool()
async def naukri_follow_status(
    group_id: Optional[str] = None,
    group_ids: Optional[list[str]] = None,
) -> dict:
    """Check follow status for one or more companies.

    Args:
        group_id: Single company group ID
        group_ids: List of company group IDs for batch check

    Returns:
        Single: {status, followed: [...], not_followed: [...]}
        Batch:  {status, total, followed_count, unfollowed_count, companies: [{group_id, name, is_followed, ...}]}
    """
    ids = group_ids or ([group_id] if group_id else [])
    if not ids:
        return {"status": "error", "message": "group_id or group_ids is required.", "error_code": "VALIDATION_ERROR"}
    if len(ids) > 1:
        return await handle_tool_action(
            lambda: _batch_follow_status(ids),
            "follow_status.batch",
        )
    return await handle_tool_action(
        lambda: _get_follow_status(ids),
        "follow_status",
    )


