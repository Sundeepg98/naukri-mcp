"""Saved/bookmarked jobs management tools."""

from datetime import datetime, timezone
from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action
from naukri_server.interfaces import api_client
from naukri_server.config import (
    logger, SAVE_JOB_API, UNSAVE_JOB_API, SAVED_JOBS_API,
)
from naukri_server.validation import validate_limit, validate_page


async def _push_save_to_naukri(job_id: str) -> bool:
    """Attempt to save a job on Naukri's backend via discovered POST endpoint."""
    if not SAVE_JOB_API:
        logger.info("SAVE_JOB_API not configured, skipping remote sync")
        return False
    try:
        await api_client.post(f"{SAVE_JOB_API}{job_id}", {})
        return True
    except Exception as e:
        logger.warning("Failed to sync save to Naukri: %s", e)
        return False


async def _list_saved_jobs(limit: int = 50, page: int = 1) -> dict:
    """List saved/bookmarked jobs from local tracking."""
    from naukri_server.database import list_saved_jobs as db_list_saved

    limit = validate_limit(limit)
    page = validate_page(page)
    offset = (page - 1) * limit

    saved, total = await db_list_saved(limit=limit, offset=offset)

    # Emit SavedJobExpiring for jobs saved 27+ days ago
    from naukri_server.events import event_bus, SavedJobExpiring
    for sj in saved:
        saved_at = sj.get("saved_at", "")
        if saved_at:
            try:
                saved_dt = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
                days_old = (datetime.now(timezone.utc) - saved_dt).days
                if days_old >= 27:
                    await event_bus.emit(SavedJobExpiring(
                        job_id=sj.get("job_id", ""),
                        title=sj.get("title", ""),
                        company=sj.get("company", ""),
                        expires_in_days=max(0, 30 - days_old),
                    ))
            except Exception:
                pass

    return {
        "status": "success",
        "total": total,
        "count": len(saved),
        "page": page,
        "has_more": (offset + limit) < total,
        "saved_jobs": saved,
    }


async def _save_job(job_id: str, title: Optional[str] = None, company: Optional[str] = None,
                    notes: Optional[str] = None,
                    sync_to_naukri: bool = False) -> dict:
    """Save/bookmark a job locally (and optionally on Naukri)."""
    from naukri_server.database import get_saved_job, upsert_saved_job, count_saved_jobs, get_application

    # Check for duplicate
    existing = await get_saved_job(job_id)
    if existing:
        return {"status": "success", "action": "already_saved", "job_id": job_id}

    # Cross-file warning: check if already applied
    applied_match = await get_application(str(job_id))

    await upsert_saved_job({
        "job_id": job_id,
        "title": title,
        "company": company,
        "notes": notes,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    })

    total = await count_saved_jobs()

    try:
        from naukri_server.events import event_bus, SavedJobAdded
        await event_bus.emit(SavedJobAdded(job_id=job_id, title=title or "", company=company or ""))
    except Exception:
        pass

    synced_remote = False
    if sync_to_naukri:
        synced_remote = await _push_save_to_naukri(job_id)

    return {"status": "success", "action": "saved", "job_id": job_id, "total_saved": total, "synced_remote": synced_remote, "already_applied": bool(applied_match)}


async def _sync_saved_jobs_from_naukri() -> dict:
    """Pull saved jobs from Naukri server and merge with local tracking."""
    from naukri_server.database import get_saved_job, upsert_saved_job, count_saved_jobs

    try:
        data = await api_client.get(SAVED_JOBS_API, params={"start": "0", "limit": "100"})
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch saved jobs: {e}", "error_code": "API_ERROR"}

    remote_jobs = data.get("list", data.get("savedJobs", []))
    if not isinstance(remote_jobs, list):
        remote_jobs = []

    new_added = 0
    for rj in remote_jobs:
        jid = str(rj.get("jobId", rj.get("job_id", "")))
        if not jid:
            continue
        existing = await get_saved_job(jid)
        if not existing:
            await upsert_saved_job({
                "job_id": jid,
                "title": rj.get("title"),
                "company": rj.get("companyName", rj.get("company")),
                "saved_at": rj.get("savedDate") or datetime.now(timezone.utc).isoformat(),
                "source": "naukri_sync",
            })
            new_added += 1

    total_local = await count_saved_jobs()

    return {
        "status": "success",
        "total_remote": len(remote_jobs),
        "new_added": new_added,
        "already_local": len(remote_jobs) - new_added,
        "total_local": total_local,
    }


async def _unsave_job(job_id: str) -> dict:
    """Unsave/unbookmark a job locally and on Naukri."""
    from naukri_server.database import delete_saved_job

    # Always attempt the remote unsave regardless of local state
    try:
        await api_client.post(UNSAVE_JOB_API + job_id, body={})
    except Exception as e:
        logger.warning("Failed to unsave job on Naukri: %s", e)

    # Remove from local DB
    deleted = await delete_saved_job(job_id)
    if deleted:
        try:
            from naukri_server.events import event_bus, SavedJobRemoved
            await event_bus.emit(SavedJobRemoved(job_id=job_id))
        except Exception:
            pass
        return {"status": "success", "action": "unsaved", "job_id": job_id}
    else:
        return {"status": "error", "message": f"Job {job_id} not in saved jobs.", "error_code": "NOT_FOUND"}


# ---------------------------------------------------------------------------
# Single-purpose MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_list_saved_jobs(limit: int = 50, page: int = 1) -> dict:
    """List your saved/bookmarked jobs with pagination.

    Args:
        limit: Max results per page (default 50)
        page: Page number (default 1)

    Returns:
        {status, total, count, page, has_more, saved_jobs: [...]}
    """
    return await handle_tool_action(
        lambda: _list_saved_jobs(limit=limit, page=page),
        "saved_jobs.list",
    )


@mcp.tool()
async def naukri_save_job(
    job_id: str,
    title: Optional[str] = None,
    company: Optional[str] = None,
    notes: Optional[str] = None,
    sync_to_naukri: bool = False,
) -> dict:
    """Save/bookmark a job for later review.

    Args:
        job_id: The Naukri job ID to save (required)
        title: Job title for display (optional)
        company: Company name for display (optional)
        notes: Personal notes about this job (optional)
        sync_to_naukri: If True, also save the job on Naukri's backend (default False)

    Returns:
        {status: "success", action: "saved", job_id, total_saved, synced_remote, already_applied}
        or {status: "success", action: "already_saved", job_id}
    """
    return await handle_tool_action(
        lambda: _save_job(job_id, title=title, company=company, notes=notes, sync_to_naukri=sync_to_naukri),
        "saved_jobs.save",
    )


@mcp.tool()
async def naukri_unsave_job(job_id: str) -> dict:
    """Unsave/unbookmark a previously saved job.

    Args:
        job_id: The Naukri job ID to unsave (required)

    Returns:
        {status: "success", action: "unsaved", job_id}
        or {status: "error", message, error_code: "NOT_FOUND"}
    """
    return await handle_tool_action(
        lambda: _unsave_job(job_id),
        "saved_jobs.unsave",
    )


@mcp.tool()
async def naukri_sync_saved_jobs() -> dict:
    """Pull saved jobs from Naukri server and merge with local tracking.

    Returns:
        {status, total_remote, new_added, already_local, total_local}
    """
    return await handle_tool_action(
        _sync_saved_jobs_from_naukri,
        "saved_jobs.sync",
    )
