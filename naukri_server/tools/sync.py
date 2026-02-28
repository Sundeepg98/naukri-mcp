"""Naukri data sync — pull applied jobs and saved jobs from Naukri's backend."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.browser import browser
from naukri_server.config import (
    logger, APPLIED_JOBS_PAGE, SAVED_JOBS_PAGE,
    APPLIED_JOBS_API, SAVED_JOBS_API,
)
from naukri_server.tools.tracking import (
    _load_json, _save_json, _applications_lock, _saved_jobs_lock,
    APPLICATIONS_FILE, SAVED_JOBS_FILE,
)


# URL patterns for browser interception (discovered)
_APPLIED_JOBS_URL_PATTERN = None  # No client-side API — page is server-rendered
_SAVED_JOBS_URL_PATTERN = "savedJobs/detail"  # matches /jobapi/v3/user/savedJobs/detail


# ---------------------------------------------------------------------------
# Dual-strategy fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_via_rest(api_path: Optional[str], params: dict = None) -> Optional[dict]:
    """Try fetching data via direct REST API call.
    Returns None if endpoint is not configured or returns an error."""
    if not api_path:
        return None
    try:
        return await api_get(api_path, params)
    except NaukriAPIError as e:
        logger.warning("REST fetch failed for %s: %s", api_path, e)
        return None
    except Exception as e:
        logger.warning("REST fetch exception for %s: %s: %s", api_path, type(e).__name__, e)
        return None


async def _fetch_via_browser(page_url: str, url_pattern: Optional[str] = None,
                              timeout: float = 10) -> Optional[dict]:
    """Fetch data by navigating to a page and intercepting JSON API responses.

    Args:
        page_url: The Naukri page to navigate to.
        url_pattern: Substring to match in response URLs. If None, returns largest JSON response.
        timeout: Seconds to wait for the target response.

    Returns the captured JSON body, or None.
    """
    async with browser._lock:
        captured_responses = []
        response_event = asyncio.Event()

        async def on_response(response):
            content_type = response.headers.get("content-type", "")
            if response.status == 200 and "json" in content_type:
                try:
                    body = await response.json()
                    captured_responses.append({"url": response.url, "body": body})
                    if url_pattern and url_pattern in response.url:
                        response_event.set()
                except Exception:
                    pass

        browser.page.on("response", on_response)
        try:
            await browser.goto(page_url)
            try:
                await asyncio.wait_for(response_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            # Extra wait for trailing API calls
            await asyncio.sleep(2)
        finally:
            browser.page.remove_listener("response", on_response)

        # Return pattern-matched response if available
        if url_pattern:
            matched = [r for r in captured_responses if url_pattern in r["url"]]
            if matched:
                return matched[0]["body"]

        # Fallback: largest JSON response (most likely the data payload)
        if captured_responses:
            return max(captured_responses,
                       key=lambda r: len(json.dumps(r["body"], default=str)))["body"]

        return None


# ---------------------------------------------------------------------------
# Parsers (defensive multi-key lookups for unknown schemas)
# ---------------------------------------------------------------------------

def _parse_applied_jobs(data) -> list:
    """Parse Naukri applied-jobs API response into normalized format."""
    items = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("appliedJobs", "jobs", "jobDetails", "applications", "data",
                     "appliedJobList", "results"):
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
            "status": _map_naukri_status(item.get("status") or item.get("applicationStatus")),
            "applied_date": item.get("appliedDate") or item.get("applied_date") or item.get("createdDate"),
            "salary": (item.get("salary")
                       or (item.get("salaryDetail", {}).get("label")
                           if isinstance(item.get("salaryDetail"), dict) else None)),
            "location": item.get("location") or item.get("cityName"),
            "url": item.get("url") or item.get("jdUrl"),
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
# Merge logic
# ---------------------------------------------------------------------------

def _merge_applications(local_apps: list, remote_jobs: list) -> dict:
    """Merge remote applied jobs into local list. Mutates local_apps in place.

    Rules:
    - Existing job_id: update status/title/company from remote, preserve local fields.
    - New job_id: add with source="naukri_sync".
    - Local-only jobs are left untouched.
    """
    local_by_id = {a["job_id"]: a for a in local_apps if a.get("job_id")}
    now = datetime.now(timezone.utc).isoformat()

    new_added = 0
    updated = 0
    unchanged = 0
    remote_ids = set()

    for rj in remote_jobs:
        rid = rj["job_id"]
        remote_ids.add(rid)

        if rid in local_by_id:
            existing = local_by_id[rid]
            changed = False
            for field in ("title", "company", "status"):
                if rj.get(field) and rj[field] != existing.get(field):
                    existing[field] = rj[field]
                    changed = True
            for field in ("applied_date", "salary", "location", "url"):
                if rj.get(field) and field not in existing:
                    existing[field] = rj[field]
            if changed:
                existing["last_synced"] = now
                updated += 1
            else:
                unchanged += 1
        else:
            entry = {
                "job_id": rid,
                "title": rj.get("title"),
                "company": rj.get("company"),
                "status": rj.get("status", "applied"),
                "applied_at": rj.get("applied_date") or now,
                "source": "naukri_sync",
                "last_synced": now,
            }
            for k, v in rj.items():
                if k not in entry and v is not None:
                    entry[k] = v
            local_apps.append(entry)
            new_added += 1

    local_only = sum(1 for a in local_apps
                     if a.get("job_id") and a["job_id"] not in remote_ids)

    return {"new_added": new_added, "updated": updated,
            "unchanged": unchanged, "local_only": local_only}


def _merge_saved_jobs(local_saved: list, remote_jobs: list) -> dict:
    """Merge remote saved jobs into local list. Mutates local_saved in place."""
    local_ids = {j["job_id"] for j in local_saved if j.get("job_id")}
    now = datetime.now(timezone.utc).isoformat()

    new_added = 0
    already_local = 0
    remote_ids = set()

    for rj in remote_jobs:
        rid = rj["job_id"]
        remote_ids.add(rid)

        if rid in local_ids:
            already_local += 1
        else:
            local_saved.append({
                "job_id": rid,
                "title": rj.get("title"),
                "company": rj.get("company"),
                "saved_at": rj.get("saved_date") or now,
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
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_sync_applications(
    force_browser: bool = False,
) -> dict:
    """Sync applied jobs from Naukri.com into local tracking.

    Pulls your application history from Naukri's backend and merges
    with local applications.json. Preserves local-only fields (pending_questions,
    tracking_extra, source, etc.). New jobs from Naukri are added with
    source="naukri_sync".

    NOTE: Naukri's applied-jobs page is server-rendered with no client-side API.
    This tool only works if a REST API endpoint has been discovered and configured.
    Currently returns "not_available" until an endpoint is found.
    Applications made via naukri_apply/naukri_batch_apply are always tracked locally.

    Args:
        force_browser: If True, skip REST API and use browser interception
                       (requires a known URL pattern to avoid capturing wrong data).

    Returns:
        - {status: "success", method, total_remote, new_added, updated, unchanged, local_only, applications: [...first 20...]}
        - {status: "not_available", message} — no API endpoint discovered
        - {status: "error", message}
    """
    try:
        remote_data = None
        method = "unknown"

        if not force_browser:
            remote_data = await _fetch_via_rest(APPLIED_JOBS_API)
            if remote_data is not None:
                method = "rest_api"

        if remote_data is None and _APPLIED_JOBS_URL_PATTERN:
            remote_data = await _fetch_via_browser(
                APPLIED_JOBS_PAGE,
                url_pattern=_APPLIED_JOBS_URL_PATTERN,
            )
            method = "browser_intercept"

        if remote_data is None:
            return {
                "status": "not_available",
                "message": ("No applied-jobs API endpoint discovered yet. "
                            "Naukri's applied-jobs page is server-rendered with no client-side API. "
                            "Applications made via naukri_apply/naukri_batch_apply are tracked locally."),
            }

        remote_jobs = _parse_applied_jobs(remote_data)

        async with _applications_lock:
            local_apps = _load_json(APPLICATIONS_FILE)
            stats = _merge_applications(local_apps, remote_jobs)
            _save_json(APPLICATIONS_FILE, local_apps)

        return {
            "status": "success",
            "method": method,
            "total_remote": len(remote_jobs),
            **stats,
            "applications": local_apps[:20],
        }
    except Exception as e:
        return {"status": "error", "message": f"Sync failed: {type(e).__name__}: {e!r}"}


@mcp.tool()
async def naukri_sync_saved_jobs(
    force_browser: bool = False,
) -> dict:
    """Sync saved/bookmarked jobs from Naukri.com into local tracking.

    Pulls your saved jobs from Naukri's backend and merges with local
    saved_jobs.json. Jobs saved locally but not on Naukri are preserved.

    Tries direct REST API first, falls back to browser interception.

    Args:
        force_browser: If True, skip REST API and use browser interception.

    Returns:
        - {status: "success", method, total_remote, new_added, already_local, local_only, saved_jobs: [...first 20...]}
        - {status: "error", message}
    """
    try:
        remote_data = None
        method = "unknown"

        if not force_browser:
            remote_data = await _fetch_via_rest(SAVED_JOBS_API)
            if remote_data is not None:
                method = "rest_api"

        if remote_data is None:
            remote_data = await _fetch_via_browser(
                SAVED_JOBS_PAGE,
                url_pattern=_SAVED_JOBS_URL_PATTERN,
            )
            method = "browser_intercept"

        if remote_data is None:
            return {
                "status": "error",
                "message": "Could not fetch saved jobs. Run naukri_debug(action='discover') to find the API endpoint.",
            }

        remote_jobs = _parse_saved_jobs(remote_data)

        async with _saved_jobs_lock:
            local_saved = _load_json(SAVED_JOBS_FILE)
            stats = _merge_saved_jobs(local_saved, remote_jobs)
            _save_json(SAVED_JOBS_FILE, local_saved)

        return {
            "status": "success",
            "method": method,
            "total_remote": len(remote_jobs),
            **stats,
            "saved_jobs": local_saved[:20],
        }
    except Exception as e:
        return {"status": "error", "message": f"Sync failed: {type(e).__name__}: {e!r}"}
