"""Application tracking and saved jobs tools."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger

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
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


async def record_application(job_id: str, title: str = None, company: str = None,
                              status: str = "applied", extra: dict = None):
    """Record a job application. Called from apply.py after successful apply."""
    async with _applications_lock:
        apps = _load_json(APPLICATIONS_FILE)
        entry = {
            "job_id": job_id,
            "title": title,
            "company": company,
            "status": status,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            **(extra or {}),
        }
        # Don't duplicate
        if not any(a.get("job_id") == job_id for a in apps):
            apps.append(entry)
            _save_json(APPLICATIONS_FILE, apps)
            logger.info("Recorded application for job %s", job_id)


@mcp.tool()
async def naukri_get_applications(
    status: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Get tracked job applications.

    Args:
        status: Filter by status ("applied", "needs_input", "rejected", etc.)
        limit: Max results to return (default 50)

    Returns list of tracked applications with job details and timestamps.
    """
    async with _applications_lock:
        apps = _load_json(APPLICATIONS_FILE)

    if status:
        apps = [a for a in apps if a.get("status") == status]

    # Most recent first
    apps.sort(key=lambda a: a.get("applied_at", ""), reverse=True)

    return {
        "status": "success",
        "total": len(apps),
        "count": min(len(apps), limit),
        "applications": apps[:limit],
    }


@mcp.tool()
async def naukri_save_job(job_id: str, title: str = None, company: str = None,
                           notes: Optional[str] = None) -> dict:
    """Save/bookmark a job for later.

    Args:
        job_id: Naukri job ID
        title: Job title (optional, for display)
        company: Company name (optional, for display)
        notes: Personal notes about this job
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

    return {"status": "saved", "job_id": job_id}


@mcp.tool()
async def naukri_get_saved_jobs(limit: int = 50) -> dict:
    """Get saved/bookmarked jobs.

    Args:
        limit: Max results to return (default 50)
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
