"""Saved jobs service — business logic for save/unsave/list/sync of bookmarked jobs.

Extracted from tools/saved_jobs.py as part of Wave 2B layer-separation refactor.
Tools become thin orchestrators that delegate to this service.
"""

from datetime import datetime, timezone
from typing import Optional

from typing import Awaitable, Callable

from naukri_server.interfaces import api_client
from naukri_server.config import (
    logger, SAVED_JOBS_API, UNSAVE_JOB_API,
)
from naukri_server.domain import safe_get
from naukri_server.validation import validate_limit, validate_page

__all__ = [
    "list_saved_jobs",
    "save_job",
    "sync_saved_jobs_from_naukri",
    "unsave_job",
]


async def list_saved_jobs(limit: int = 50, page: int = 1) -> dict:
    """List saved/bookmarked jobs from local tracking."""
    from naukri_server.database import list_saved_jobs as db_list_saved

    limit = validate_limit(limit)
    page = validate_page(page)
    offset = (page - 1) * limit

    saved, total = await db_list_saved(limit=limit, offset=offset)

    # Emit SavedJobExpiring for jobs saved 27+ days ago
    from naukri_server.events import event_bus, SavedJobExpiring
    for sj in saved:
        saved_at = safe_get(sj, "saved_at", default="")
        if saved_at:
            try:
                saved_dt = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
                days_old = (datetime.now(timezone.utc) - saved_dt).days
                if days_old >= 27:
                    await event_bus.emit(SavedJobExpiring(
                        job_id=safe_get(sj, "job_id", default=""),
                        title=safe_get(sj, "title", default=""),
                        company=safe_get(sj, "company", default=""),
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


async def save_job(
    job_id: str,
    title: Optional[str] = None,
    company: Optional[str] = None,
    notes: Optional[str] = None,
    sync_to_naukri: bool = False,
    push_save: Optional[Callable[[str], Awaitable[bool]]] = None,
) -> dict:
    """Save/bookmark a job locally (and optionally on Naukri).

    Args:
        push_save: Optional async callable invoked when ``sync_to_naukri`` is
            True. Injecting the remote-save callable keeps the service free
            of direct dependencies on tools-layer config constants while
            preserving testability of the wrapper in the tool module.
    """
    from naukri_server.database import (
        get_saved_job, upsert_saved_job, count_saved_jobs, get_application,
    )

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
        await event_bus.emit(SavedJobAdded(
            job_id=job_id, title=title or "", company=company or "",
        ))
    except Exception:
        pass

    synced_remote = False
    if sync_to_naukri and push_save is not None:
        synced_remote = await push_save(job_id)

    return {
        "status": "success",
        "action": "saved",
        "job_id": job_id,
        "total_saved": total,
        "synced_remote": synced_remote,
        "already_applied": bool(applied_match),
    }


async def sync_saved_jobs_from_naukri() -> dict:
    """Pull saved jobs from Naukri server and merge with local tracking."""
    from naukri_server.database import (
        get_saved_job, upsert_saved_job, count_saved_jobs,
    )

    try:
        data = await api_client.get(
            SAVED_JOBS_API, params={"start": "0", "limit": "100"},
        )
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to fetch saved jobs: {e}",
            "error_code": "API_ERROR",
        }

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


async def unsave_job(job_id: str) -> dict:
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

    return {
        "status": "error",
        "message": f"Job {job_id} not in saved jobs.",
        "error_code": "NOT_FOUND",
    }
