"""Saved/bookmarked jobs management tools."""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from naukri_server import mcp
from naukri_server.interfaces import api_client
from naukri_server.config import (
    logger, SAVE_JOB_API, UNSAVE_JOB_API, SAVED_JOBS_API,
    APPLICATIONS_FILE, SAVED_JOBS_FILE,
)
from naukri_server.models import paginate, validate_action_params
from naukri_server.validation import validate_limit, validate_page
from naukri_server.tools.tracking import _load_json, _save_json

_saved_jobs_lock = asyncio.Lock()


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
    limit = validate_limit(limit)
    page = validate_page(page)
    async with _saved_jobs_lock:
        saved = _load_json(SAVED_JOBS_FILE)

    saved.sort(key=lambda j: j.get("saved_at", ""), reverse=True)

    pagination, page_items = paginate(saved, page, limit)

    return {
        "status": "success",
        **pagination,
        "saved_jobs": page_items,
    }


async def _save_job(job_id: str, title: str = None, company: str = None,
                    notes: Optional[str] = None,
                    sync_to_naukri: bool = False) -> dict:
    """Save/bookmark a job locally (and optionally on Naukri)."""
    async with _saved_jobs_lock:
        saved = _load_json(SAVED_JOBS_FILE)

        # Check for duplicate
        if any(j.get("job_id") == job_id for j in saved):
            return {"status": "success", "action": "already_saved", "job_id": job_id}

        # Cross-file warning: check if already applied
        apps = _load_json(APPLICATIONS_FILE)
        applied_match = next((a for a in apps if str(a.get("job_id")) == str(job_id)), None)

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

    return {"status": "success", "action": "saved", "job_id": job_id, "total_saved": len(saved), "synced_remote": synced_remote, "already_applied": bool(applied_match)}


async def _sync_saved_jobs_from_naukri() -> dict:
    """Pull saved jobs from Naukri server and merge with local tracking."""
    try:
        data = await api_client.get(SAVED_JOBS_API, params={"start": "0", "limit": "100"})
    except Exception as e:
        return {"status": "error", "message": f"Failed to fetch saved jobs: {e}", "error_code": "API_ERROR"}

    remote_jobs = data.get("list", data.get("savedJobs", []))
    if not isinstance(remote_jobs, list):
        remote_jobs = []

    async with _saved_jobs_lock:
        local = _load_json(SAVED_JOBS_FILE)
        local_ids = {str(j.get("job_id")) for j in local}
        new_added = 0
        for rj in remote_jobs:
            jid = str(rj.get("jobId", rj.get("job_id", "")))
            if jid and jid not in local_ids:
                local.append({
                    "job_id": jid,
                    "title": rj.get("title"),
                    "company": rj.get("companyName", rj.get("company")),
                    "saved_at": rj.get("savedDate") or datetime.now(timezone.utc).isoformat(),
                    "source": "naukri_sync",
                })
                local_ids.add(jid)
                new_added += 1
        if new_added:
            _save_json(SAVED_JOBS_FILE, local)

    return {
        "status": "success",
        "total_remote": len(remote_jobs),
        "new_added": new_added,
        "already_local": len(remote_jobs) - new_added,
        "total_local": len(local),
    }


async def _unsave_job(job_id: str) -> dict:
    """Unsave/unbookmark a job locally and on Naukri."""
    # Always attempt the remote unsave regardless of local state
    try:
        await api_client.post(UNSAVE_JOB_API + job_id, body={})
    except Exception as e:
        logger.warning("Failed to unsave job on Naukri: %s", e)

    # Remove from local saved_jobs.json
    async with _saved_jobs_lock:
        saved = _load_json(SAVED_JOBS_FILE)
        original_len = len(saved)
        saved = [j for j in saved if j.get("job_id") != job_id]
        if len(saved) < original_len:
            _save_json(SAVED_JOBS_FILE, saved)
            return {"status": "success", "action": "unsaved", "job_id": job_id}
        else:
            return {"status": "error", "message": f"Job {job_id} not in saved jobs.", "error_code": "NOT_FOUND"}


# ---------------------------------------------------------------------------
# ISP param validation
# ---------------------------------------------------------------------------

_VALID_PARAMS_PER_ACTION = {
    "list": {"limit", "page"},
    "save": {"job_id", "title", "company", "notes", "sync_to_naukri"},
    "unsave": {"job_id"},
    "sync": set(),
}


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
    page: int = 1,
) -> dict:
    """Unified saved/bookmarked jobs management — list, save, unsave, and sync.

    Actions:
      - "list": Get saved/bookmarked jobs (use limit/page for pagination)
      - "save": Save/bookmark a job for later (requires job_id)
      - "unsave": Unsave/unbookmark a job (requires job_id)
      - "sync": Pull saved jobs from Naukri server and merge with local tracking

    Args:
        action: "list" | "save" | "unsave" | "sync"
        job_id: Required for save/unsave — the Naukri job ID
        title: Job title for display (optional, save only)
        company: Company name for display (optional, save only)
        notes: Personal notes about this job (optional, save only)
        sync_to_naukri: If True, also save the job on Naukri's backend (save only)
        limit: Max results per page for list action (default 50)
        page: Page number for list action (default 1)

    Returns:
        - list: {status, total, count, page, has_more, saved_jobs: [...]}
        - save: {status: "saved", job_id, total_saved, synced_remote}
                or {status: "already_saved", job_id}
        - unsave: {status: "unsaved", job_id}
                  or {status: "not_found", job_id}
        - {status: "error", message} on failure
    """
    # ── ISP: warn about params irrelevant to chosen action ─────────────
    _provided = {
        "job_id": job_id, "title": title, "company": company, "notes": notes,
        "sync_to_naukri": sync_to_naukri if sync_to_naukri else None,
        "limit": limit if limit != 50 else None,
        "page": page if page != 1 else None,
    }
    _unused = validate_action_params(action, _provided, _VALID_PARAMS_PER_ACTION)

    def _attach_unused(result: dict) -> dict:
        if _unused and isinstance(result, dict):
            result["unused_params"] = _unused
        return result

    # -- list ---------------------------------------------------------------
    if action == "list":
        return _attach_unused(await _list_saved_jobs(limit=limit, page=page))

    # -- save ---------------------------------------------------------------
    elif action == "save":
        if not job_id:
            return {"status": "error", "message": "save requires job_id.", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await _save_job(job_id, title=title, company=company, notes=notes, sync_to_naukri=sync_to_naukri))

    # -- unsave -------------------------------------------------------------
    elif action == "unsave":
        if not job_id:
            return {"status": "error", "message": "unsave requires job_id.", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await _unsave_job(job_id))

    # -- sync ---------------------------------------------------------------
    elif action == "sync":
        return _attach_unused(await _sync_saved_jobs_from_naukri())

    # -- unknown action -----------------------------------------------------
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, save, unsave, sync", "error_code": "VALIDATION_ERROR"}
