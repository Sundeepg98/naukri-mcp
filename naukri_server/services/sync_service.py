"""Sync service — pure business logic for sync parsing, status mapping, and CSV flattening.

Extracted from tools/sync.py and tools/export.py. Contains pure functions plus
the in-place merge helpers (merge_applications, merge_saved_jobs) which take all
their state via parameters. I/O-bound orchestration (_sync_applications,
_sync_saved_jobs, _fetch_*, _load/_save_sync_state_async) remains in the tool
modules because tests mock their internal call graph via
patch("naukri_server.tools.sync.<name>").

Parsing is delegated to ``naukri_server.domain.parsed_job`` factories which add
resilience logging (debug for optional fields, warning for critical ones).
All field reads from external API responses route through ``safe_get`` for
anti-corruption-layer logging.
"""

from datetime import datetime, timezone

from naukri_server.config import logger
from naukri_server.domain import safe_get
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
    # Pure merge helpers (moved from tools/sync.py)
    "merge_applications",
    "merge_saved_jobs",
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
# Pure merge helpers (moved from tools/sync.py)
#
# Both functions mutate their first argument in place (preserving the existing
# contract) and return a stats dict. Tests construct lists, call the function,
# and assert on the mutated list + stats — no I/O is involved.
# ---------------------------------------------------------------------------

# Fields synced from remote that overwrite local when present.
_REMOTE_OVERWRITE_FIELDS = (
    "title", "company", "recruiter_active", "apply_type",
    "ars_score", "star_rating", "job_activity", "company_rating", "is_open",
)
# Fields filled from remote ONLY if the local entry does not already have them.
_REMOTE_FILL_IF_MISSING_FIELDS = ("applied_date", "salary", "location", "url")


def merge_applications(local_apps: list, remote_jobs: list) -> dict:
    """Merge remote applied jobs into ``local_apps`` (mutates in place).

    Rules:
      * Existing job_id -> overwrite ``_REMOTE_OVERWRITE_FIELDS`` from remote,
        fill ``_REMOTE_FILL_IF_MISSING_FIELDS`` if absent locally.
      * New job_id -> add with source="naukri_sync".
      * Local-only jobs are left untouched.
      * Status conflicts are resolved by preferring remote (logged at INFO).
      * ``applied_at`` is NEVER overwritten on existing entries.

    Returns ``{new_added, updated, unchanged, local_only}`` stats.
    """
    # Deduplicate local_apps by job_id (keep last entry per id)
    local_by_id: dict = {}
    for a in local_apps:
        jid = safe_get(a, "job_id", field_name="job_id", warn=False)
        if jid:
            local_by_id[jid] = a
    now = datetime.now(timezone.utc).isoformat()

    new_added = 0
    updated = 0
    unchanged = 0
    remote_ids: set = set()

    for rj in remote_jobs:
        rid = rj["job_id"]
        remote_ids.add(rid)

        if rid in local_by_id:
            existing = local_by_id[rid]
            changed = False
            for field in _REMOTE_OVERWRITE_FIELDS:
                rv = safe_get(rj, field, field_name=field, warn=False)
                if rv is not None and rv != existing.get(field):
                    existing[field] = rv
                    changed = True
            # Status: update from remote but log conflicts
            new_status = safe_get(rj, "status", field_name="status", warn=False)
            if new_status and new_status != existing.get("status"):
                logger.info("Sync: status conflict for %s — local=%s, remote=%s (using remote)",
                            rid, existing.get("status"), new_status)
                existing["status"] = new_status
                changed = True
            for field in _REMOTE_FILL_IF_MISSING_FIELDS:
                rv = safe_get(rj, field, field_name=field, warn=False)
                if rv is not None and field not in existing:
                    existing[field] = rv
            # PRESERVE existing applied_at — never overwrite with now
            if changed:
                existing["last_synced"] = now
                updated += 1
            else:
                unchanged += 1
        else:
            # NEW entry: use remote applied_date if available, else now
            entry = {
                "job_id": rid,
                "title": safe_get(rj, "title", field_name="title", warn=False),
                "company": safe_get(rj, "company", field_name="company", warn=False),
                "status": safe_get(rj, "status", field_name="status", warn=False, default="applied"),
                "applied_at": safe_get(rj, "applied_date", "appliedDate",
                                       field_name="applied_at", warn=False, default=now),
                "source": "naukri_sync",
                "last_synced": now,
            }
            for k, v in rj.items():
                if k not in entry and v is not None:
                    entry[k] = v
            local_by_id[rid] = entry
            new_added += 1

    # Rebuild local_apps from deduplicated dict (preserves merge results)
    local_apps.clear()
    local_apps.extend(local_by_id.values())

    local_only = sum(1 for a in local_apps
                     if a.get("job_id") and a["job_id"] not in remote_ids)

    return {"new_added": new_added, "updated": updated,
            "unchanged": unchanged, "local_only": local_only}


def merge_saved_jobs(local_saved: list, remote_jobs: list) -> dict:
    """Merge remote saved jobs into ``local_saved`` (mutates in place).

    Returns ``{new_added, already_local, local_only}`` stats.
    """
    local_ids = {j["job_id"] for j in local_saved if j.get("job_id")}
    now = datetime.now(timezone.utc).isoformat()

    new_added = 0
    already_local = 0
    remote_ids: set = set()

    for rj in remote_jobs:
        rid = rj["job_id"]
        remote_ids.add(rid)

        if rid in local_ids:
            already_local += 1
        else:
            local_saved.append({
                "job_id": rid,
                "title": safe_get(rj, "title", field_name="title", warn=False),
                "company": safe_get(rj, "company", field_name="company", warn=False),
                "saved_at": safe_get(rj, "saved_date", field_name="saved_at",
                                     warn=False, default=now),
                "source": "naukri_sync",
                "last_synced": now,
                **{k: v for k, v in rj.items()
                   if k not in ("job_id", "title", "company", "saved_date") and v is not None},
            })
            new_added += 1

    local_only = sum(1 for j in local_saved
                     if j.get("job_id") and j["job_id"] not in remote_ids)

    return {"new_added": new_added, "already_local": already_local,
            "local_only": local_only}


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
