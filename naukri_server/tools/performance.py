"""Performance tools — search impressions, recruiter activity, and profile activity level."""

from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.config import SEARCH_IMPRESSIONS_API, RECRUITER_ACTIVITY_API, ACTIVITY_LEVEL_API

# Valid filter values for recruiter activity (from activityBucketCount keys)
ACTIVITY_FILTERS = {"VIEWED", "MOBILE_VIEWED", "DOWNLOADED", "CONTACTED", "ADD_TO_FOLDER"}


@mcp.tool()
@api_tool("Get search impressions")
async def naukri_get_search_impressions(days: int = 7) -> dict:
    """Get your profile's search appearance stats — how many times recruiters found you,
    which keywords they searched, and day-by-day timeline.

    Args:
        days: Time period in days (7, 30, or 90). Default 7.

    Returns:
        - {status: "success", total_appearances, recruiter_actions, daily_average, percentage_change, timeline: {date: count}, top_keywords: {keyword: count}}
        - {status: "error", message}
    """
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


@mcp.tool()
@api_tool("Get recruiter activity")
async def naukri_get_recruiter_activity(
    page: int = 1,
    size: int = 20,
    filter_by: Optional[str] = None,
) -> dict:
    """Get detailed recruiter activity on your profile — who viewed, downloaded,
    contacted you, or added you to their folder. Supports pagination and filtering.

    The API returns a paginated list of recruiter actions (last 90 days).
    Each page defaults to 20 items. Use filter_by to see only specific action types.

    Args:
        page: Page number (1-based). Default 1.
        size: Items per page (default 20, max ~50).
        filter_by: Filter by activity type. One of:
                   "VIEWED" — recruiters who viewed your Naukri profile
                   "MOBILE_VIEWED" — recruiters who viewed via mobile
                   "DOWNLOADED" — recruiters who downloaded your resume
                   "CONTACTED" — recruiters who sent you NVites/messages
                   "ADD_TO_FOLDER" — recruiters who bookmarked your profile
                   None — all actions (default)

    Returns:
        - {status: "success", page, size, filter_by, total_actions, percentage_change,
           buckets: {VIEWED: {count, change}, ...}, activities: [{recruiter_name, company, action, date, designation, location, recruiter_id, previous_actions_count}],
           has_more: bool}
        - {status: "error", message}
    """
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


@mcp.tool()
@api_tool("Get activity level")
async def naukri_get_activity_level() -> dict:
    """Check your profile's current activity level (HIGH, MEDIUM, or LOW).

    A HIGH activity level means your profile is actively visible to recruiters.
    Regular logins and profile updates keep it high.

    Returns:
        - {status: "success", level, logged_in, resume_updated, profile_updated}
        - {status: "error", message}
    """
    data = await api_get(ACTIVITY_LEVEL_API)
    return {
        "status": "success",
        "level": data.get("level", "UNKNOWN"),
        "logged_in": data.get("loggedInStatus", False),
        "resume_updated": data.get("rmjStatus", False),
        "profile_updated": data.get("updatedStatus", False),
    }
