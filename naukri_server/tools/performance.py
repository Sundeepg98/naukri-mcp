"""Performance tools — search impressions, recruiter activity, and profile activity level."""

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import SEARCH_IMPRESSIONS_API, RECRUITER_ACTIVITY_API, ACTIVITY_LEVEL_API


@mcp.tool()
async def naukri_get_search_impressions(days: int = 7) -> dict:
    """Get your profile's search appearance stats — how many times recruiters found you,
    which keywords they searched, and day-by-day timeline.

    Args:
        days: Time period in days (7, 30, or 90). Default 7.

    Returns:
        - {status: "success", total_appearances, recruiter_actions, daily_average, percentage_change, timeline: {date: count}, top_keywords: {keyword: count}}
        - {status: "error", message}
    """
    try:
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
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get search impressions: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_recruiter_activity() -> dict:
    """Get detailed recruiter activity on your profile — who viewed, downloaded,
    contacted you, or added you to their folder. Shows activity counts by type.

    Returns:
        - {status: "success", total_actions, percentage_change, buckets: {VIEWED: {count, change}, DOWNLOADED: {...}, CONTACTED: {...}, ADD_TO_FOLDER: {...}}, activities: [{...}]}
        - {status: "error", message}
    """
    try:
        data = await api_post(RECRUITER_ACTIVITY_API, body={})

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

        # Parse individual activities
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
            })

        return {
            "status": "success",
            "total_actions": success.get("count", len(activities)),
            "percentage_change": success.get("percentageChange"),
            "buckets": buckets,
            "activities": activities,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get recruiter activity: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_activity_level() -> dict:
    """Check your profile's current activity level (HIGH, MEDIUM, or LOW).

    A HIGH activity level means your profile is actively visible to recruiters.
    Regular logins and profile updates keep it high.

    Returns:
        - {status: "success", level, logged_in, resume_updated, profile_updated}
        - {status: "error", message}
    """
    try:
        data = await api_get(ACTIVITY_LEVEL_API)
        return {
            "status": "success",
            "level": data.get("level", "UNKNOWN"),
            "logged_in": data.get("loggedInStatus", False),
            "resume_updated": data.get("rmjStatus", False),
            "profile_updated": data.get("updatedStatus", False),
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get activity level: {type(e).__name__}: {e}"}
