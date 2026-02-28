"""Application tracking and saved jobs tools."""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.config import logger, APPLICATION_STATUS_API, MATCH_ANALYTICS_API

# Data files live alongside questions.json in the naukri/ directory
_PACKAGE_ROOT = Path(__file__).parent.parent.parent
APPLICATIONS_FILE = _PACKAGE_ROOT / "applications.json"
SAVED_JOBS_FILE = _PACKAGE_ROOT / "saved_jobs.json"

_applications_lock = asyncio.Lock()
_saved_jobs_lock = asyncio.Lock()


def _load_json(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_json(path: Path, data: list):
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


async def record_application(job_id: str, title: str = None, company: str = None,
                              status: str = "applied", extra: dict = None):
    """Record or update a job application."""
    async with _applications_lock:
        apps = _load_json(APPLICATIONS_FILE)
        now = datetime.now(timezone.utc).isoformat()
        existing = next((a for a in apps if a.get("job_id") == job_id), None)
        if existing:
            existing["status"] = status
            existing["updated_at"] = now
            if title:
                existing["title"] = title
            if company:
                existing["company"] = company
            if extra:
                existing.update(extra)
        else:
            entry = {
                "job_id": job_id,
                "title": title,
                "company": company,
                "status": status,
                "applied_at": now,
                **(extra or {}),
            }
            apps.append(entry)
        _save_json(APPLICATIONS_FILE, apps)


@mcp.tool()
async def naukri_get_applications(
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Get tracked job applications with filtering and summary stats.

    Args:
        status: Filter by status ("applied", "needs_input", "already_applied", "error")
        date_from: ISO date string, include applications on/after this date (e.g. "2026-02-01")
        date_to: ISO date string, include applications on/before this date (e.g. "2026-02-28")
        limit: Max results to return (default 50)

    Returns:
        - {status: "success", total, count, summary: {total_all_statuses, by_status: {...}}, applications: [...]}
    """
    async with _applications_lock:
        apps = _load_json(APPLICATIONS_FILE)

    if date_from:
        apps = [a for a in apps if (a.get("applied_at") or "") >= date_from]
    if date_to:
        cutoff = date_to + "T23:59:59" if "T" not in date_to else date_to
        apps = [a for a in apps if (a.get("applied_at") or "") <= cutoff]

    by_status = {}
    for a in apps:
        s = a.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1

    if status:
        filtered = [a for a in apps if a.get("status") == status]
    else:
        filtered = apps

    filtered.sort(key=lambda a: a.get("applied_at", ""), reverse=True)

    return {
        "status": "success",
        "total": len(filtered),
        "count": min(len(filtered), limit),
        "summary": {"total_all_statuses": len(apps), "by_status": by_status},
        "applications": filtered[:limit],
    }


async def _push_save_to_naukri(job_id: str) -> bool:
    """Attempt to save a job on Naukri's backend via discovered POST endpoint."""
    from naukri_server.config import SAVE_JOB_API
    from naukri_server.api import api_post
    if not SAVE_JOB_API:
        logger.info("SAVE_JOB_API not configured, skipping remote sync")
        return False
    try:
        await api_post(f"{SAVE_JOB_API}{job_id}", {})
        return True
    except Exception as e:
        logger.warning("Failed to sync save to Naukri: %s", e)
        return False


@mcp.tool()
async def naukri_save_job(job_id: str, title: str = None, company: str = None,
                           notes: Optional[str] = None,
                           sync_to_naukri: bool = False) -> dict:
    """Save/bookmark a job for later.

    Args:
        job_id: Naukri job ID
        title: Job title (optional, for display)
        company: Company name (optional, for display)
        notes: Personal notes about this job
        sync_to_naukri: If True, also save the job on Naukri's backend
            via the discovered SAVE_JOB_API endpoint.

    Returns:
        - {status: "saved", job_id, total_saved, synced_remote}
        - {status: "already_saved", job_id}
    """
    async with _saved_jobs_lock:
        saved = _load_json(SAVED_JOBS_FILE)

        # Check for duplicate
        if any(j.get("job_id") == job_id for j in saved):
            return {"status": "already_saved", "job_id": job_id}

        saved.append({
            "job_id": job_id,
            "title": title,
            "company": company,
            "notes": notes,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        })
        _save_json(SAVED_JOBS_FILE, saved)

    synced_remote = False
    if sync_to_naukri:
        synced_remote = await _push_save_to_naukri(job_id)

    return {"status": "saved", "job_id": job_id, "total_saved": len(saved), "synced_remote": synced_remote}


@mcp.tool()
async def naukri_get_saved_jobs(limit: int = 50) -> dict:
    """Get saved/bookmarked jobs.

    Args:
        limit: Max results to return (default 50)

    Returns:
        - {status: "success", total, count, saved_jobs: [...]}
    """
    async with _saved_jobs_lock:
        saved = _load_json(SAVED_JOBS_FILE)

    saved.sort(key=lambda j: j.get("saved_at", ""), reverse=True)

    return {
        "status": "success",
        "total": len(saved),
        "count": min(len(saved), limit),
        "saved_jobs": saved[:limit],
    }


@mcp.tool()
async def naukri_get_application_status(job_id: str) -> dict:
    """Get detailed status for a specific job application — recruiter activity, applicant count, match score, timeline.

    Args:
        job_id: Naukri job ID (e.g. "270226007446")

    Returns:
        - {status: "success", job_id, title, company, location, is_open, total_applicants,
           recruiter_activity, match_rating, status_timeline: [{status, date}], matching_results}
        - {status: "error", message}
    """
    try:
        data = await api_get(APPLICATION_STATUS_API, params={"jobId": job_id, "applyType": "normal"})

        job_details = data.get("jobDetails") or {}
        status_steps = data.get("status") or []
        matching = data.get("matchingResults")

        timeline = []
        for step in status_steps:
            entry = {"status": step.get("status") or step.get("label", "")}
            if step.get("date"):
                entry["date"] = step["date"]
            timeline.append(entry)

        return {
            "status": "success",
            "job_id": job_id,
            "title": job_details.get("jobTitle"),
            "company": job_details.get("company"),
            "location": job_details.get("location"),
            "is_open": job_details.get("isOpen"),
            "total_applicants": data.get("totalApplicants"),
            "recruiter_activity": job_details.get("jobActivity"),
            "recruiter_activity_date": job_details.get("jobActivityDate"),
            "match_rating": data.get("starRating"),
            "feedback_stored": data.get("feedbackStored"),
            "status_timeline": timeline,
            "matching_results": matching,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get application status: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_match_analytics(days: int = 7) -> dict:
    """Get match-score analytics for recent job applications — overall match distribution and per-field breakdowns.

    Args:
        days: Number of days of application history to analyze (default 7)

    Returns:
        - {status: "success", days, total_applies, complete_match, high_match, medium_match,
           low_match, field_breakdown, user_details}
        - {status: "error", message}
    """
    try:
        data = await api_get(MATCH_ANALYTICS_API, params={"days": str(days)})

        return {
            "status": "success",
            "days": days,
            "total_applies": data.get("totalApplies"),
            "complete_match": data.get("completeMatch"),
            "high_match": data.get("highMatch"),
            "medium_match": data.get("mediumMatch"),
            "low_match": data.get("lowMatch"),
            "field_breakdown": data.get("relevantFieldMatch"),
            "user_details": data.get("userDetails"),
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get match analytics: {type(e).__name__}: {e}"}
