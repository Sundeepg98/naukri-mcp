"""Sync service — pure business logic for sync parsing, status mapping, and CSV flattening.

Extracted from tools/sync.py and tools/export.py. Only contains pure functions
with no dependencies that tests patch at the tool-module level. Orchestration
functions (_sync_applications, _sync_saved_jobs, _merge_*, _export_data, state
management) remain in the tool modules because tests mock their internal call
graph via patch("naukri_server.tools.sync.<name>").
"""

__all__ = [
    # Parsers
    "_parse_applied_jobs",
    "_parse_saved_jobs",
    # Status mapping
    "_map_naukri_status",
    # CSV helper
    "_flatten_for_csv",
]


# ---------------------------------------------------------------------------
# Parsers (defensive multi-key lookups for unknown schemas)
# ---------------------------------------------------------------------------

def _parse_applied_jobs(data) -> list:
    """Parse Naukri applied-jobs API response into normalized format.

    Handles two schemas:
    - History API (applyDetails): {jobId, jobTitle, company, appliedDate, location, ...}
    - Generic fallback: {jobs, jobDetails, applications, data, ...}
    """
    items = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("applyDetails", "appliedJobs", "jobs", "jobDetails",
                     "applications", "data", "appliedJobList", "results"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break

    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("jobId") or item.get("job_id") or item.get("id") or "")
        if not job_id:
            continue

        # Extract status — history API uses statusMsg or nested status list
        raw_status = item.get("statusMsg") or item.get("status") or item.get("applicationStatus")
        status_list = []
        if isinstance(raw_status, list):
            status_list = raw_status
            raw_status = raw_status[0].get("statusMsg") if raw_status and isinstance(raw_status[0], dict) else raw_status

        # Extract view count from status array (statusId=4 = "Viewed")
        view_count = None
        last_viewed = None
        for s in status_list:
            if isinstance(s, dict) and s.get("statusId") == 4:
                view_count = s.get("count", 0)
                last_viewed = s.get("modifiedDate")
                break

        jobs.append({
            "job_id": job_id,
            "title": item.get("jobTitle") or item.get("title") or item.get("designation"),
            "company": item.get("company") or item.get("companyName"),
            "status": _map_naukri_status(raw_status),
            "applied_date": item.get("appliedDate") or item.get("applied_date") or item.get("createdDate"),
            "salary": (item.get("salary")
                       or (item.get("salaryDetail", {}).get("label")
                           if isinstance(item.get("salaryDetail"), dict) else None)),
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
        })
    return jobs


def _parse_saved_jobs(data) -> list:
    """Parse Naukri saved-jobs API response into normalized format."""
    items = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("savedJobs", "jobs", "jobDetails", "data",
                     "savedJobList", "results", "bookmarks"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break

    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("jobId") or item.get("job_id") or item.get("id") or "")
        if not job_id:
            continue
        jobs.append({
            "job_id": job_id,
            "title": item.get("title") or item.get("jobTitle") or item.get("designation"),
            "company": item.get("companyName") or item.get("company"),
            "saved_date": item.get("savedDate") or item.get("createdDate") or item.get("savedAt"),
            "salary": item.get("salary"),
            "location": item.get("location") or item.get("cityName"),
            "url": item.get("url") or item.get("jdUrl"),
        })
    return jobs


def _map_naukri_status(status) -> str:
    """Map Naukri's internal status labels to our tracking statuses."""
    if not status:
        return "applied"
    s = str(status).lower()
    mapping = {
        "applied": "applied",
        "viewed": "viewed_by_recruiter",
        "shortlisted": "shortlisted",
        "rejected": "rejected",
        "not interested": "rejected",
        "interview": "interview",
        "hired": "hired",
    }
    for pattern, local_status in mapping.items():
        if pattern in s:
            return local_status
    return s


# ---------------------------------------------------------------------------
# CSV flatten helper (from export.py)
# ---------------------------------------------------------------------------

def _flatten_for_csv(records: list[dict]) -> list[dict]:
    """Flatten nested dicts and lists for CSV output."""
    flat = []
    for rec in records:
        row = {}
        for k, v in rec.items():
            if isinstance(v, list):
                row[k] = ", ".join(str(i) for i in v)
            elif isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    row[f"{k}.{sub_k}"] = sub_v if not isinstance(sub_v, (list, dict)) else str(sub_v)
            else:
                row[k] = v
        flat.append(row)
    return flat
