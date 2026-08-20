"""Performance service — business logic for search impressions, recruiter
activity, and profile activity level.

Extracted from tools/performance.py as part of Wave 2B layer-separation
refactor. Tools become thin orchestrators that delegate here.
"""

import json
from typing import Optional

from naukri_server.interfaces import api_client
from naukri_server.config import (
    SEARCH_IMPRESSIONS_API, RECRUITER_ACTIVITY_API, ACTIVITY_LEVEL_API,
    WIDGET_HEADERS,
)
from jobcore import normalize_skill
from naukri_server.domain import safe_get
from naukri_server.validation import validate_limit, validate_page

# Valid filter values for recruiter activity (from activityBucketCount keys)
ACTIVITY_FILTERS = {"VIEWED", "MOBILE_VIEWED", "DOWNLOADED", "CONTACTED", "ADD_TO_FOLDER"}

# Valid day ranges for impressions and recruiter_activity
VALID_DAYS = {7, 30, 90}

# Recruiter search keywords are free text, so the raw map is enormous and
# repetitive: one live capture on 2026-08-20 returned 704 distinct keys, of
# which the top 15 covered the operator's actual profile. Returning the whole
# tail in every response is pure token cost for a tool called on every daily
# brief.
DEFAULT_TOP_KEYWORDS = 15
MAX_TOP_KEYWORDS = 200

__all__ = [
    "ACTIVITY_FILTERS",
    "VALID_DAYS",
    "DEFAULT_TOP_KEYWORDS",
    "MAX_TOP_KEYWORDS",
    "get_search_impressions",
    "get_recruiter_activity",
    "get_activity_level",
]


def _rank_keywords(raw, top_n: int) -> tuple:
    """Normalize, merge and rank the recruiter search-keyword map.

    Returns ``(top_keywords, stats)``.

    Normalization goes through ``jobcore.normalize_skill`` -- the same taxonomy
    auto_hunt scores against -- rather than a second table here. A second
    normalizer is how two of them drift apart, and a keyword ranked by one set
    of rules while jobs are scored by another is worse than no ranking.

    Raw keys are recruiter free text, so one skill arrives many ways: the
    2026-08-20 capture had node.js spelled five ways across 161 mentions, which
    ranked it below single-spelling skills a third its size.

    Deliberately NOT done: splitting mashed keys such as
    "Criblclick House Monitoring Tool". That mangling is upstream -- this map is
    a keyword->count dict and nothing here concatenates anything -- and guessing
    where to cut it would invent data. It survives as one honest entry.
    """
    if not isinstance(raw, dict):
        return {}, {"distinct_raw": 0, "distinct_normalized": 0,
                    "returned": 0, "total_mentions": 0}

    merged: dict = {}
    for key, count in raw.items():
        if not isinstance(key, str):
            continue
        try:
            count = int(count)
        except (TypeError, ValueError):
            continue
        canonical = normalize_skill(key)
        if not canonical:
            continue
        merged[canonical] = merged.get(canonical, 0) + count

    # Count descending, then name, so equal counts have a stable order rather
    # than whatever the upstream dict happened to yield.
    ordered = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
    top = dict(ordered[:top_n])
    stats = {
        "distinct_raw": len(raw),
        "distinct_normalized": len(merged),
        "returned": len(top),
        "total_mentions": sum(merged.values()),
    }
    return top, stats


async def get_search_impressions(days: int = 7, top_n: int = DEFAULT_TOP_KEYWORDS) -> dict:
    """Fetch search impression stats from the API and return structured result.

    Field naming, corrected 2026-08-20. Two names were wrong, which is what made
    a reviewer read `daily_average: 2314` next to `total: 5660` as impossible:

    * ``totalSearchAppearances`` is CUMULATIVE and ignores ``days`` entirely, so
      it is now ``total_appearances_all_time``. Sitting next to ``days`` under
      the old name ``total_appearances``, it read as a window figure.
    * ``dayWiseSearchAppearance`` is the total WITHIN the requested window --
      documented as such in probing/analytics-report.md, and provable from the
      payload itself, since the timeline buckets sum to exactly this value. It
      is now ``window_appearances``.

    ``daily_average`` survives as a real average, derived here rather than
    relabelled from upstream.
    """
    top_n = validate_limit(top_n, max_allowed=MAX_TOP_KEYWORDS)
    data = await api_client.get(
        SEARCH_IMPRESSIONS_API,
        params={"days": str(days), "totalAppearances": "1"},
        extra_headers=WIDGET_HEADERS,
    )
    window = safe_get(
        data, "dayWiseSearchAppearance", default=None,
        field_name="window_appearances", warn=True, context="search_impressions",
    )
    top_keywords, keyword_stats = _rank_keywords(
        safe_get(data, "searchKeyWords", default={}), top_n,
    )
    return {
        "status": "success",
        "days": days,
        "total_appearances_all_time": safe_get(
            data, "totalSearchAppearances", default=None,
            field_name="total_appearances_all_time", warn=True,
            context="search_impressions",
        ),
        "recruiter_actions": safe_get(
            data, "recruiterActions", default=None,
            field_name="recruiter_actions", warn=True, context="search_impressions",
        ),
        "window_appearances": window,
        "daily_average": (
            round(window / days, 1)
            if isinstance(window, (int, float)) and days else None
        ),
        "percentage_change": safe_get(data, "percentageChange", default=None),
        "timeline": safe_get(data, "searchAppearanceTimeline", default={}),
        "top_keywords": top_keywords,
        "keyword_stats": keyword_stats,
    }


async def get_recruiter_activity(
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
                "message": (
                    f"Invalid filter_by '{filter_by}'. Must be one of: "
                    f"{', '.join(sorted(ACTIVITY_FILTERS))}"
                ),
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
            "recruiter_name": safe_get(
                act, "recruiterName", "name", default="",
                field_name="recruiter_name", warn=True, context="recruiter_activity",
            ),
            "company": safe_get(
                act, "companyName", "company", default="",
                field_name="company", warn=True, context="recruiter_activity",
            ),
            "action": safe_get(
                act, "activityType", "action", default="",
                field_name="activity_type", warn=True, context="recruiter_activity",
            ),
            "date": safe_get(
                act, "activityDate", "date", default="",
                field_name="activity_date", warn=True, context="recruiter_activity",
            ),
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
                await event_bus.emit(RecruiterEngaged(
                    job_id=act.get("meta_job_id", ""),
                    company=act.get("company", ""),
                    title="",
                ))
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


async def get_activity_level() -> dict:
    """Fetch profile activity level from the API and return structured result."""
    data = await api_client.get(ACTIVITY_LEVEL_API, extra_headers=WIDGET_HEADERS)
    return {
        "status": "success",
        "level": safe_get(
            data, "level", default="UNKNOWN", field_name="level",
            warn=True, context="activity_level",
        ),
        "logged_in": safe_get(data, "loggedInStatus", default=False),
        "resume_updated": safe_get(data, "rmjStatus", default=False),
        "profile_updated": safe_get(data, "updatedStatus", default=False),
    }
