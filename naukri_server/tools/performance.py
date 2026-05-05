"""Performance tools — search impressions, recruiter activity, and profile activity level.

Tool layer — business logic lives in ``services/performance_service.py``.
``api_client`` is re-imported here so test patches at
``naukri_server.tools.performance.api_client.{get,post}`` continue to work.
"""

from typing import Optional

from naukri_server import mcp
from naukri_server.api import NaukriAPIError
from naukri_server.interfaces import api_client  # noqa: F401 — tests patch this path
from naukri_server.services.performance_service import (
    ACTIVITY_FILTERS,
    VALID_DAYS,
    get_search_impressions as _get_search_impressions,
    get_recruiter_activity as _get_recruiter_activity,
    get_activity_level as _get_activity_level,
)

# Re-export for backward compatibility (daily_brief + tests).
__all__ = [
    "ACTIVITY_FILTERS",
    "VALID_DAYS",
    "_get_search_impressions",
    "_get_recruiter_activity",
    "_get_activity_level",
]


# ---------------------------------------------------------------------------
# Single-purpose MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_search_impressions(days: int = 7) -> dict:
    """Get search impression stats — how many times recruiters found your profile.

    Shows total appearances, recruiter actions, daily averages, and top keywords
    recruiters used to find you.

    Args:
        days: Time period — must be 7, 30, or 90 (default 7)

    Returns:
        {status, days, total_appearances, recruiter_actions, daily_average,
         percentage_change, timeline, top_keywords}
    """
    if days not in VALID_DAYS:
        return {
            "status": "error",
            "message": f"Invalid days={days}. Must be 7, 30, or 90",
            "error_code": "VALIDATION_ERROR",
        }
    try:
        return await _get_search_impressions(days=days)
    except NaukriAPIError as e:
        return {
            "status": "error",
            "message": str(e),
            "http_status": e.status,
            "error_code": "API_ERROR",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed: {type(e).__name__}: {e}",
            "error_code": "API_ERROR",
        }


@mcp.tool()
async def naukri_recruiter_activity(
    page: int = 1,
    limit: int = 100,
    filter_by: Optional[str] = None,
) -> dict:
    """See which recruiters viewed, downloaded, or contacted you.

    Shows individual recruiter actions with company, designation, and date.
    Filter by action type to focus on specific recruiter behaviors.

    Args:
        page: Page number (default 1)
        limit: Items per page (default 100 = all activities)
        filter_by: Filter by action type — "VIEWED", "MOBILE_VIEWED",
                   "DOWNLOADED", "CONTACTED", "ADD_TO_FOLDER". None = all.

    Returns:
        {status, page, size, filter_by, total_actions, percentage_change,
         buckets, activities: [{recruiter_name, company, action, date, ...}], has_more}
    """
    try:
        return await _get_recruiter_activity(page=page, size=limit, filter_by=filter_by)
    except NaukriAPIError as e:
        return {
            "status": "error",
            "message": str(e),
            "http_status": e.status,
            "error_code": "API_ERROR",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed: {type(e).__name__}: {e}",
            "error_code": "API_ERROR",
        }


@mcp.tool()
async def naukri_activity_level() -> dict:
    """Check your current profile activity level (HIGH/MEDIUM/LOW).

    Shows whether your profile is actively being seen by recruiters,
    and whether your resume and profile are up to date.

    Returns:
        {status, level, logged_in, resume_updated, profile_updated}
    """
    try:
        return await _get_activity_level()
    except NaukriAPIError as e:
        return {
            "status": "error",
            "message": str(e),
            "http_status": e.status,
            "error_code": "API_ERROR",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed: {type(e).__name__}: {e}",
            "error_code": "API_ERROR",
        }
