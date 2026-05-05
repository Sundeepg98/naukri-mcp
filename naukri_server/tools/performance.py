"""Performance tools — search impressions, recruiter activity, and profile activity level."""

import json
from typing import Optional

from naukri_server import mcp
from naukri_server.api import NaukriAPIError
from naukri_server.interfaces import api_client
from naukri_server.config import SEARCH_IMPRESSIONS_API, RECRUITER_ACTIVITY_API, ACTIVITY_LEVEL_API, WIDGET_HEADERS
from naukri_server.domain import safe_get
from naukri_server.validation import validate_page

# Valid filter values for recruiter activity (from activityBucketCount keys)
ACTIVITY_FILTERS = {"VIEWED", "MOBILE_VIEWED", "DOWNLOADED", "CONTACTED", "ADD_TO_FOLDER"}

# Valid day ranges for impressions and recruiter_activity
VALID_DAYS = {7, 30, 90}


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool and daily_brief)
# ---------------------------------------------------------------------------

async def _get_search_impressions(days: int = 7) -> dict:
    """Fetch search impression stats from the API and return structured result."""
    data = await api_client.get(
        SEARCH_IMPRESSIONS_API,
        params={"days": str(days), "totalAppearances": "1"},
        extra_headers=WIDGET_HEADERS,
    )
    return {
        "status": "success",
        "days": days,
        "total_appearances": safe_get(data, "totalSearchAppearances", default=None, field_name="total_appearances", warn=True, context="search_impressions"),
        "recruiter_actions": safe_get(data, "recruiterActions", default=None, field_name="recruiter_actions", warn=True, context="search_impressions"),
        "daily_average": safe_get(data, "dayWiseSearchAppearance", default=None),
        "percentage_change": safe_get(data, "percentageChange", default=None),
        "timeline": safe_get(data, "searchAppearanceTimeline", default={}),
        "top_keywords": safe_get(data, "searchKeyWords", default={}),
    }


async def _get_recruiter_activity(
    page: int = 1,
    size: int = 100,
    filter_by: Optional[str] = None,
) -> dict:
    """Fetch recruiter activity from the API and return structured result."""
    page = validate_page(page)
    # Validate filter_by
    if filter_by is not None:
        filter_by = filter_by.upper()
        if filter_by not in ACTIVITY_FILTERS:
            return {
                "status": "error",
                "message": f"Invalid filter_by '{filter_by}'. Must be one of: {', '.join(sorted(ACTIVITY_FILTERS))}",
                "error_code": "VALIDATION_ERROR",
            }

    # POST body format discovered via JS bundle analysis of profilePerformance page:
    # The frontend sends {page: N, size: M, filterBy: "TYPE"|null}
    body = {"page": page, "size": size}
    if filter_by:
        body["filterBy"] = filter_by

    data = await api_client.post(RECRUITER_ACTIVITY_API, body=body)

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
                "label": bucket_data.get("label", ""),
                "is_new": bool(bucket_data.get("isNew", 0)),
            }
        else:
            buckets[bucket_name] = {"count": bucket_data}

    # Parse individual activities (each has ~25 keys)
    activities = []
    for act in activities_raw:
        if not isinstance(act, dict):
            continue
        activities.append({
            "recruiter_name": safe_get(act, "recruiterName", "name", default="", field_name="recruiter_name", warn=True, context="recruiter_activity"),
            "company": safe_get(act, "companyName", "company", default="", field_name="company", warn=True, context="recruiter_activity"),
            "action": safe_get(act, "activityType", "action", default="", field_name="activity_type", warn=True, context="recruiter_activity"),
            "date": safe_get(act, "activityDate", "date", default="", field_name="activity_date", warn=True, context="recruiter_activity"),
            "designation": safe_get(act, "designation", default=""),
            "location": safe_get(act, "city", "location", default=""),
            "recruiter_id": safe_get(act, "recruiterId", "recruiterProfileId", default=""),
            "previous_actions_count": act.get("previousActionCount", 0),
            "company_master_name": act.get("companyMasterName", ""),
            "is_new": bool(act.get("isNew", 0)),
            "activity_map": act.get("activityMap"),
            "meta_job_id": None,
            "domain_expertise": act.get("domainExpertise", ""),
            "last_active_date": act.get("lastActiveDate", ""),
            "is_international": bool(int(act.get("isInternational", 0) or 0)),
            "follower_count": int(act.get("followerCount", 0) or 0),
            "msg_sent": bool(int(act.get("isMsgSent", 0) or 0)),
            "user_following": bool(int(act.get("userFollowing", 0) or 0)),
        })
        meta_str = act.get("metaData", "")
        if meta_str:
            try:
                meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
                activities[-1]["meta_job_id"] = meta.get("jobId")
            except (json.JSONDecodeError, TypeError):
                pass

    # Emit RecruiterEngaged for CONTACTED or DOWNLOADED actions (once per batch)
    try:
        from naukri_server.events import event_bus, RecruiterEngaged
        for act in activities:
            if act.get("action") in ("CONTACTED", "DOWNLOADED"):
                await event_bus.emit(RecruiterEngaged(job_id=act.get("meta_job_id", ""), company=act.get("company", ""), title=""))
                break  # Emit once per batch, not per activity
    except Exception:
        pass

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
    data = await api_client.get(ACTIVITY_LEVEL_API, extra_headers=WIDGET_HEADERS)
    return {
        "status": "success",
        "level": safe_get(data, "level", default="UNKNOWN", field_name="level", warn=True, context="activity_level"),
        "logged_in": safe_get(data, "loggedInStatus", default=False),
        "resume_updated": safe_get(data, "rmjStatus", default=False),
        "profile_updated": safe_get(data, "updatedStatus", default=False),
    }


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

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
        return {"status": "error", "message": f"Invalid days={days}. Must be 7, 30, or 90", "error_code": "VALIDATION_ERROR"}
    try:
        return await _get_search_impressions(days=days)
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


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
        return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


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
        return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}
