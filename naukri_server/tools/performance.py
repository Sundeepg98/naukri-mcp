"""Performance tools — search impressions, recruiter activity, and profile activity level."""

from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import SEARCH_IMPRESSIONS_API, RECRUITER_ACTIVITY_API, ACTIVITY_LEVEL_API

# Valid filter values for recruiter activity (from activityBucketCount keys)
ACTIVITY_FILTERS = {"VIEWED", "MOBILE_VIEWED", "DOWNLOADED", "CONTACTED", "ADD_TO_FOLDER"}

# Valid day ranges for impressions and recruiter_activity
VALID_DAYS = {7, 30, 90}


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool and daily_brief)
# ---------------------------------------------------------------------------

async def _get_search_impressions(days: int = 7) -> dict:
    """Fetch search impression stats from the API and return structured result."""
    data = await api_get(
        SEARCH_IMPRESSIONS_API,
        params={"days": str(days), "totalAppearances": "1"},
    )
    return {
        "status": "success",
        "days": days,
        "total_appearances": data.get("totalSearchAppearances"),
        "recruiter_actions": data.get("recruiterActions"),
        "daily_average": data.get("dayWiseSearchAppearance"),
        "percentage_change": data.get("percentageChange"),
        "timeline": data.get("searchAppearanceTimeline", {}),
        "top_keywords": data.get("searchKeyWords", {}),
    }


async def _get_recruiter_activity(
    page: int = 1,
    size: int = 20,
    filter_by: Optional[str] = None,
) -> dict:
    """Fetch recruiter activity from the API and return structured result."""
    # Validate filter_by
    if filter_by is not None:
        filter_by = filter_by.upper()
        if filter_by not in ACTIVITY_FILTERS:
            return {
                "status": "error",
                "message": f"Invalid filter_by '{filter_by}'. Must be one of: {', '.join(sorted(ACTIVITY_FILTERS))}",
            }

    # POST body format discovered via JS bundle analysis of profilePerformance page:
    # The frontend sends {page: N, size: M, filterBy: "TYPE"|null}
    body = {"page": page, "size": size}
    if filter_by:
        body["filterBy"] = filter_by

    data = await api_post(RECRUITER_ACTIVITY_API, body=body)

    success = data.get("successResponse", data)
    activities_raw = success.get("jobseekerActivityList", [])
    bucket_counts = success.get("activityBucketCount", {})

    # Parse activity buckets
    buckets = {}
    for bucket_name, bucket_data in bucket_counts.items():
        if isinstance(bucket_data, dict):
            buckets[bucket_name] = {
                "count": bucket_data.get("count", 0),
                "percentage_change": bucket_data.get("percentageChange", 0),
            }
        else:
            buckets[bucket_name] = {"count": bucket_data}

    # Parse individual activities (each has ~25 keys)
    activities = []
    for act in activities_raw:
        if not isinstance(act, dict):
            continue
        activities.append({
            "recruiter_name": act.get("recruiterName") or act.get("name", ""),
            "company": act.get("companyName") or act.get("company", ""),
            "action": act.get("activityType") or act.get("action", ""),
            "date": act.get("activityDate") or act.get("date", ""),
            "designation": act.get("designation", ""),
            "location": act.get("city") or act.get("location", ""),
            "recruiter_id": act.get("recruiterId") or act.get("recruiterProfileId", ""),
            "previous_actions_count": act.get("previousActionCount", 0),
        })

    total = success.get("count", len(activities))
    has_more = (page * size) < total

    return {
        "status": "success",
        "page": page,
        "size": size,
        "filter_by": filter_by,
        "total_actions": total,
        "percentage_change": success.get("percentageChange"),
        "buckets": buckets,
        "activities": activities,
        "has_more": has_more,
    }


async def _get_activity_level() -> dict:
    """Fetch profile activity level from the API and return structured result."""
    data = await api_get(ACTIVITY_LEVEL_API)
    return {
        "status": "success",
        "level": data.get("level", "UNKNOWN"),
        "logged_in": data.get("loggedInStatus", False),
        "resume_updated": data.get("rmjStatus", False),
        "profile_updated": data.get("updatedStatus", False),
    }


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_performance(
    metric: str,
    days: int = 7,
    page: int = 1,
    size: int = 20,
    filter_by: Optional[str] = None,
) -> dict:
    """Unified performance dashboard — impressions, recruiter activity, and activity level.

    Metrics:
      - "impressions": Search appearance stats — how many times recruiters found you,
        which keywords they searched, and day-by-day timeline. Uses days param.
      - "recruiter_activity": Who viewed, downloaded, contacted you, or added you to
        their folder. Supports pagination (page/size) and filtering (filter_by).
      - "activity_level": Current profile activity level (HIGH/MEDIUM/LOW).
        No extra params needed.

    Args:
        metric: "impressions" | "recruiter_activity" | "activity_level"
        days: Time period — must be 7, 30, or 90 (default 7). Used by impressions.
        page: Page number for recruiter_activity (default 1).
        size: Items per page for recruiter_activity (default 20).
        filter_by: Filter recruiter_activity by type. One of:
                   "VIEWED" | "MOBILE_VIEWED" | "DOWNLOADED" | "CONTACTED" | "ADD_TO_FOLDER"
                   None = all actions (default).

    Returns:
        - impressions: {status, days, total_appearances, recruiter_actions, daily_average, percentage_change, timeline, top_keywords}
        - recruiter_activity: {status, page, size, filter_by, total_actions, percentage_change, buckets, activities, has_more}
        - activity_level: {status, level, logged_in, resume_updated, profile_updated}
        - {status: "error", message} on failure
    """
    # ── impressions ───────────────────────────────────────────────────
    if metric == "impressions":
        if days not in VALID_DAYS:
            return {"status": "error", "message": f"Invalid days={days}. Must be one of: {', '.join(str(d) for d in sorted(VALID_DAYS))}"}
        try:
            return await _get_search_impressions(days=days)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status}
        except Exception as e:
            return {"status": "error", "message": f"Get search impressions failed: {type(e).__name__}: {e}"}

    # ── recruiter_activity ────────────────────────────────────────────
    elif metric == "recruiter_activity":
        try:
            return await _get_recruiter_activity(page=page, size=size, filter_by=filter_by)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status}
        except Exception as e:
            return {"status": "error", "message": f"Get recruiter activity failed: {type(e).__name__}: {e}"}

    # ── activity_level ────────────────────────────────────────────────
    elif metric == "activity_level":
        try:
            return await _get_activity_level()
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status}
        except Exception as e:
            return {"status": "error", "message": f"Get activity level failed: {type(e).__name__}: {e}"}

    # ── unknown metric ────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown metric '{metric}'. Use: impressions, recruiter_activity, activity_level"}
