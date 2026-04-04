"""Parsed job factories — defensive parsing with resilience logging for sync API responses.

Centralises the multi-key root extraction and per-item field mapping that
previously lived inline in sync_service._parse_applied_jobs / _parse_saved_jobs.
Every extraction step now logs what it found (debug) or warns when critical
fields are missing, making silent data-loss visible in logs.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# --- Root keys the sync APIs have historically returned jobs under ----------

APPLIED_ROOT_KEYS = [
    "applyDetails", "appliedJobs", "jobs", "jobDetails",
    "applications", "data", "appliedJobList", "results",
]

SAVED_ROOT_KEYS = [
    "savedJobs", "jobs", "jobDetails", "data",
    "savedJobList", "results", "bookmarks",
]


# ---------------------------------------------------------------------------
# Generic helper — find the job array in an API response
# ---------------------------------------------------------------------------

def find_job_array(data, keys: list[str], context: str) -> list:
    """Try multiple root keys to find the job array in an API response.

    Returns the first ``list`` found under *keys*, or *data* itself when it
    is already a list.  Logs which key matched (debug) or warns when none did.
    """
    if isinstance(data, list):
        logger.debug("[%s] Response is raw list (%d items)", context, len(data))
        return data

    if not isinstance(data, dict):
        logger.warning("[%s] Unexpected response type: %s", context, type(data).__name__)
        return []

    for key in keys:
        val = data.get(key)
        if val and isinstance(val, list):
            logger.debug("[%s] Found jobs under key '%s' (%d items)", context, key, len(val))
            return val

    logger.warning(
        "[%s] No job array found. Tried keys: %s. Top-level keys: %s",
        context, keys, list(data.keys())[:10],
    )
    return []


# ---------------------------------------------------------------------------
# Applied job factory
# ---------------------------------------------------------------------------

def parse_applied_job(item: dict, map_status_fn) -> Optional[dict]:
    """Parse a single applied job from any API schema variant.

    Returns ``None`` when the item has no usable job ID (logged as warning).
    Logs debug for missing optional fields.

    Args:
        item: Raw dict from the API response array.
        map_status_fn: Callable that maps a raw status string to a local status.
    """
    if not isinstance(item, dict):
        logger.debug("Skipping non-dict item in applied jobs: %s", type(item).__name__)
        return None

    job_id = str(item.get("jobId") or item.get("job_id") or item.get("id") or "")
    if not job_id:
        logger.warning("Applied job missing job_id. Available keys: %s", list(item.keys())[:10])
        return None

    title = item.get("jobTitle") or item.get("title") or item.get("designation")
    if not title:
        logger.debug("Applied job %s missing title", job_id)

    company = item.get("company") or item.get("companyName")
    if not company:
        logger.debug("Applied job %s missing company", job_id)

    # Extract status — history API uses statusMsg or nested status list
    raw_status = item.get("statusMsg") or item.get("status") or item.get("applicationStatus")
    status_list: list = []
    if isinstance(raw_status, list):
        status_list = raw_status
        raw_status = (
            raw_status[0].get("statusMsg")
            if raw_status and isinstance(raw_status[0], dict)
            else raw_status
        )

    # Extract view count from status array (statusId=4 = "Viewed")
    view_count = None
    last_viewed = None
    for s in status_list:
        if isinstance(s, dict) and s.get("statusId") == 4:
            view_count = s.get("count", 0)
            last_viewed = s.get("modifiedDate")
            break

    # Salary — direct field or nested salaryDetail
    salary = item.get("salary")
    if salary is None:
        salary_detail = item.get("salaryDetail")
        if isinstance(salary_detail, dict):
            salary = salary_detail.get("label")

    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "status": map_status_fn(raw_status),
        "applied_date": item.get("appliedDate") or item.get("applied_date") or item.get("createdDate"),
        "salary": salary,
        "location": item.get("location") or item.get("cityName"),
        "url": item.get("jdUrl") or item.get("url"),
        "apply_type": item.get("applyType"),
        "recruiter_active": item.get("isRecruiterActive"),
        "ars_score": item.get("arsScore"),
        "star_rating": item.get("starRating"),
        "is_open": item.get("isOpen") == "true" if item.get("isOpen") is not None else None,
        "job_activity": item.get("jobActivity"),
        "job_activity_date": item.get("jobActivityDate"),
        "apply_flow_type": item.get("applyFlowType"),
        "is_crawled": item.get("isCrawled", False),
        "company_rating": item.get("companyRating"),
        "view_count": view_count,
        "last_viewed": last_viewed,
    }


# ---------------------------------------------------------------------------
# Saved job factory
# ---------------------------------------------------------------------------

def parse_saved_job(item: dict) -> Optional[dict]:
    """Parse a single saved job from any API schema variant.

    Returns ``None`` when the item has no usable job ID (logged as warning).
    Logs debug for missing optional fields.
    """
    if not isinstance(item, dict):
        logger.debug("Skipping non-dict item in saved jobs: %s", type(item).__name__)
        return None

    job_id = str(item.get("jobId") or item.get("job_id") or item.get("id") or "")
    if not job_id:
        logger.warning("Saved job missing job_id. Available keys: %s", list(item.keys())[:10])
        return None

    title = item.get("title") or item.get("jobTitle") or item.get("designation")
    if not title:
        logger.debug("Saved job %s missing title", job_id)

    company = item.get("companyName") or item.get("company")
    if not company:
        logger.debug("Saved job %s missing company", job_id)

    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "saved_date": item.get("savedDate") or item.get("createdDate") or item.get("savedAt"),
        "salary": item.get("salary"),
        "location": item.get("location") or item.get("cityName"),
        "url": item.get("url") or item.get("jdUrl"),
    }
