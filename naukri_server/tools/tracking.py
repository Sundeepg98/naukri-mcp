"""Application tracking and saved jobs tools."""

import asyncio
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.config import (
    logger, APPLICATION_STATUS_API, MATCH_ANALYTICS_API,
    SAVE_JOB_API, UNSAVE_JOB_API,
)

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
            backup = path.with_suffix(".backup")
            if backup.exists():
                try:
                    logger.warning("Primary %s corrupted, recovering from backup", path.name)
                    return json.loads(backup.read_text(encoding="utf-8"))
                except Exception:
                    pass
            logger.error("Both primary and backup corrupted for %s", path.name)
            return []
    return []


def _save_json(path: Path, data: list):
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    # Backup existing file before overwriting
    if path.exists():
        backup = path.with_suffix(".backup")
        shutil.copy2(str(path), str(backup))
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
    """List your tracked job applications with filtering and summary stats.

    Reads from local applications tracking file. Use naukri_sync(entity="applications") first to fetch latest data from Naukri.com.

    Lists applications from local tracking. For detailed status of ONE specific
    application, use naukri_get_application_status instead.

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


# ---------------------------------------------------------------------------
# Internal helpers for saved jobs (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

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


async def _list_saved_jobs(limit: int = 50) -> dict:
    """List saved/bookmarked jobs from local tracking."""
    async with _saved_jobs_lock:
        saved = _load_json(SAVED_JOBS_FILE)

    saved.sort(key=lambda j: j.get("saved_at", ""), reverse=True)

    return {
        "status": "success",
        "total": len(saved),
        "count": min(len(saved), limit),
        "saved_jobs": saved[:limit],
    }


async def _save_job(job_id: str, title: str = None, company: str = None,
                    notes: Optional[str] = None,
                    sync_to_naukri: bool = False) -> dict:
    """Save/bookmark a job locally (and optionally on Naukri)."""
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


async def _unsave_job(job_id: str) -> dict:
    """Unsave/unbookmark a job locally and on Naukri."""
    # Always attempt the remote unsave regardless of local state
    try:
        await api_post(UNSAVE_JOB_API + job_id, body={})
    except Exception as e:
        logger.warning("Failed to unsave job on Naukri: %s", e)

    # Remove from local saved_jobs.json
    async with _saved_jobs_lock:
        saved = _load_json(SAVED_JOBS_FILE)
        original_len = len(saved)
        saved = [j for j in saved if j.get("job_id") != job_id]
        if len(saved) < original_len:
            _save_json(SAVED_JOBS_FILE, saved)
            return {"status": "unsaved", "job_id": job_id}
        else:
            return {"status": "not_found", "job_id": job_id}


# ---------------------------------------------------------------------------
# Unified MCP tool for saved jobs
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_saved_jobs(
    action: str = "list",
    job_id: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
    notes: Optional[str] = None,
    sync_to_naukri: bool = False,
    limit: int = 50,
) -> dict:
    """Unified saved/bookmarked jobs management — list, save, and unsave.

    Actions:
      - "list": Get saved/bookmarked jobs (use limit for pagination)
      - "save": Save/bookmark a job for later (requires job_id)
      - "unsave": Unsave/unbookmark a job (requires job_id)

    Args:
        action: "list" | "save" | "unsave"
        job_id: Required for save/unsave — the Naukri job ID
        title: Job title for display (optional, save only)
        company: Company name for display (optional, save only)
        notes: Personal notes about this job (optional, save only)
        sync_to_naukri: If True, also save the job on Naukri's backend (save only)
        limit: Max results for list action (default 50)

    Returns:
        - list: {status, total, count, saved_jobs: [...]}
        - save: {status: "saved", job_id, total_saved, synced_remote}
                or {status: "already_saved", job_id}
        - unsave: {status: "unsaved", job_id}
                  or {status: "not_found", job_id}
        - {status: "error", message} on failure
    """
    # ── list ───────────────────────────────────────────────────────────
    if action == "list":
        return await _list_saved_jobs(limit=limit)

    # ── save ───────────────────────────────────────────────────────────
    elif action == "save":
        if not job_id:
            return {"status": "error", "message": "save requires job_id."}
        return await _save_job(job_id, title=title, company=company, notes=notes, sync_to_naukri=sync_to_naukri)

    # ── unsave ─────────────────────────────────────────────────────────
    elif action == "unsave":
        if not job_id:
            return {"status": "error", "message": "unsave requires job_id."}
        return await _unsave_job(job_id)

    # ── unknown action ─────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, save, unsave"}


@mcp.tool()
@api_tool("Get application status")
async def naukri_get_application_status(job_id: str) -> dict:
    """Get detailed status for a specific job application — recruiter activity, applicant count, match score, timeline.

    For listing all applications, use naukri_get_applications instead.
    Requires: a job_id from naukri_get_applications or naukri_apply results.

    Args:
        job_id: Naukri job ID (e.g. "270226007446")

    Returns:
        - {status: "success", job_id, title, company, location, is_open, total_applicants,
           recruiter_activity, match_rating, status_timeline: [{status, date}], matching_results}
        - {status: "error", message}
    """
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


@mcp.tool()
@api_tool("Get match analytics")
async def naukri_get_match_analytics(days: int = 7) -> dict:
    """Get match-score analytics for recent job applications — overall match distribution and per-field breakdowns.

    Args:
        days: Number of days of application history to analyze (default 7)

    Returns:
        - {status: "success", days, total_applies, complete_match, high_match, medium_match,
           low_match, field_breakdown, user_details}
        - {status: "error", message}
    """
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


@mcp.tool()
async def naukri_purge_applications(
    before_date: str,
    dry_run: bool = True,
) -> dict:
    """Delete old applications from local tracking before a given date.

    This only affects the local tracking file — it does NOT withdraw applications on Naukri.
    Use dry_run=True (default) to preview what would be deleted.

    Args:
        before_date: ISO date (YYYY-MM-DD). Delete applications applied before this date.
        dry_run: If True (default), only preview — don't actually delete.

    Returns:
        - {status: "success", purged_count, remaining_count, dry_run, sample_purged}
        - {status: "error", message}
    """
    async with _applications_lock:
        apps = _load_json(APPLICATIONS_FILE)
        if not apps:
            return {"status": "success", "purged_count": 0, "remaining_count": 0, "dry_run": dry_run, "sample_purged": []}

        keep = []
        purge = []
        for a in apps:
            applied_at = (a.get("applied_at") or a.get("appliedDate") or "")[:10]
            if applied_at and applied_at < before_date:
                purge.append(a)
            else:
                keep.append(a)

        if not dry_run and purge:
            _save_json(APPLICATIONS_FILE, keep)

        sample = [{"job_id": a.get("job_id"), "title": a.get("title"), "applied_at": a.get("applied_at", "")[:10]} for a in purge[:5]]

        return {
            "status": "success",
            "purged_count": len(purge),
            "remaining_count": len(keep),
            "dry_run": dry_run,
            "sample_purged": sample,
        }


@mcp.tool()
async def naukri_get_stale_applications(
    days_threshold: int = 14,
    min_stale_score: int = 40,
) -> dict:
    """Detect stale job applications that need follow-up or should be abandoned.

    Analyzes your synced applications for staleness signals: closed jobs, no
    recruiter activity, no views, low match scores, and age. Returns ranked
    list with stale scores and recommended actions.

    Run naukri_sync(entity="applications") first to ensure fresh data.

    Args:
        days_threshold: Consider apps older than N days for staleness (default 14)
        min_stale_score: Minimum staleness score 0-100 to include (default 40)

    Returns:
        - {status: "success", total_applications, stale_count,
           stale_applications: [{job_id, title, company, stale_score,
           reasons, recommendation, applied_date, is_open, view_count,
           job_activity, ars_score}]}
        - {status: "error", message}
    """
    from datetime import datetime, timezone

    async with _applications_lock:
        apps = _load_json(APPLICATIONS_FILE)

    if not apps:
        return {"status": "success", "total_applications": 0, "stale_count": 0, "stale_applications": []}

    now = datetime.now(timezone.utc)
    stale_apps = []

    for app in apps:
        applied_at = app.get("applied_at") or app.get("appliedDate") or ""
        if not applied_at:
            continue

        # Parse applied date
        try:
            if "T" in applied_at:
                applied_dt = datetime.fromisoformat(applied_at.replace("Z", "+00:00"))
            else:
                applied_dt = datetime.strptime(applied_at[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        days_since_apply = (now - applied_dt).days
        if days_since_apply < days_threshold:
            continue  # Too fresh to be stale

        # Compute stale score
        stale_score = 0
        reasons = []

        is_open = app.get("is_open")
        if is_open is False:
            stale_score += 100
            reasons.append("Job closed")

        view_count = app.get("view_count")
        has_been_viewed = view_count and view_count > 0
        if days_since_apply > days_threshold and not has_been_viewed:
            stale_score += 60
            reasons.append(f"Applied {days_since_apply}d ago, never viewed")
        elif days_since_apply > days_threshold * 2:
            stale_score += 30
            reasons.append(f"Applied {days_since_apply}d ago")

        job_activity = app.get("job_activity")
        if job_activity is not None and job_activity == 0:
            stale_score += 30
            reasons.append("Zero recruiter activity on posting")

        ars_score = app.get("ars_score")
        if ars_score is not None and ars_score < 30:
            stale_score += 20
            reasons.append(f"Low match score ({ars_score}%)")

        job_activity_date = app.get("job_activity_date")
        if job_activity_date:
            try:
                if "T" in str(job_activity_date):
                    act_dt = datetime.fromisoformat(str(job_activity_date).replace("Z", "+00:00"))
                else:
                    act_dt = datetime.strptime(str(job_activity_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_since_activity = (now - act_dt).days
                if days_since_activity > 14:
                    stale_score += 25
                    reasons.append(f"Recruiter inactive for {days_since_activity}d")
            except (ValueError, TypeError):
                pass

        if stale_score < min_stale_score:
            continue

        # Recommendation
        if stale_score >= 100:
            recommendation = "Move on — job is closed or very stale"
        elif stale_score >= 60:
            recommendation = "Follow up or move on"
        elif stale_score >= 40:
            recommendation = "Consider following up"
        else:
            recommendation = "Still active — wait"

        stale_apps.append({
            "job_id": app.get("job_id"),
            "title": app.get("title"),
            "company": app.get("company"),
            "stale_score": min(stale_score, 100),
            "reasons": reasons,
            "recommendation": recommendation,
            "applied_date": applied_at[:10],
            "is_open": is_open,
            "view_count": view_count,
            "job_activity": job_activity,
            "ars_score": ars_score,
        })

    # Sort by stale score descending
    stale_apps.sort(key=lambda x: x["stale_score"], reverse=True)

    return {
        "status": "success",
        "total_applications": len(apps),
        "stale_count": len(stale_apps),
        "stale_applications": stale_apps,
    }
