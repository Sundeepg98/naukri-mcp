"""Sync service — pure business logic for sync parsing, status mapping, and CSV flattening.

Extracted from tools/sync.py and tools/export.py. Only contains pure functions
with no dependencies that tests patch at the tool-module level. Orchestration
functions (_sync_applications, _sync_saved_jobs, _merge_*, _export_data, state
management) remain in the tool modules because tests mock their internal call
graph via patch("naukri_server.tools.sync.<name>").

Parsing is delegated to ``naukri_server.domain.parsed_job`` factories which add
resilience logging (debug for optional fields, warning for critical ones).
"""

from naukri_server.domain.parsed_job import (
    find_job_array,
    parse_applied_job,
    parse_saved_job,
    APPLIED_ROOT_KEYS,
    SAVED_ROOT_KEYS,
)

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
# Parsers (delegate to domain factories with resilience logging)
# ---------------------------------------------------------------------------

def _parse_applied_jobs(data) -> list:
    """Parse Naukri applied-jobs API response into normalized format.

    Handles two schemas:
    - History API (applyDetails): {jobId, jobTitle, company, appliedDate, location, ...}
    - Generic fallback: {jobs, jobDetails, applications, data, ...}

    Uses ``find_job_array`` to locate the items list (with logging) and
    ``parse_applied_job`` to extract each item (with per-field logging).
    """
    items = find_job_array(data, APPLIED_ROOT_KEYS, "applied_jobs")
    return [
        job for item in items
        if (job := parse_applied_job(item, _map_naukri_status)) is not None
    ]


def _parse_saved_jobs(data) -> list:
    """Parse Naukri saved-jobs API response into normalized format.

    Uses ``find_job_array`` to locate the items list (with logging) and
    ``parse_saved_job`` to extract each item (with per-field logging).
    """
    items = find_job_array(data, SAVED_ROOT_KEYS, "saved_jobs")
    return [
        job for item in items
        if (job := parse_saved_job(item)) is not None
    ]


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
