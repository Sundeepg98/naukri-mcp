"""Saved/bookmarked jobs management tools.

Tool layer — business logic lives in ``services/saved_jobs_service.py``.
The ``_push_save_to_naukri`` thin wrapper stays in this module because
test suites patch ``naukri_server.tools.saved_jobs.SAVE_JOB_API`` and
``naukri_server.tools.saved_jobs.api_client.post`` directly. It is
injected into ``service.save_job`` via the ``push_save`` parameter so
the service stays free of tool-layer config dependencies.
"""

from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action
from naukri_server.interfaces import api_client  # noqa: F401 — tests patch this path
from naukri_server.config import (  # noqa: F401 — tests patch SAVE_JOB_API on this module
    logger, SAVE_JOB_API, UNSAVE_JOB_API, SAVED_JOBS_API,
)
from naukri_server.services.saved_jobs_service import (
    list_saved_jobs as _list_saved_jobs,
    save_job as _save_job_service,
    sync_saved_jobs_from_naukri as _sync_saved_jobs_from_naukri,
    unsave_job as _unsave_job,
)


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


async def _save_job(
    job_id: str,
    title: Optional[str] = None,
    company: Optional[str] = None,
    notes: Optional[str] = None,
    sync_to_naukri: bool = False,
) -> dict:
    """Tool-layer adapter — injects ``_push_save_to_naukri`` into the service."""
    return await _save_job_service(
        job_id=job_id,
        title=title,
        company=company,
        notes=notes,
        sync_to_naukri=sync_to_naukri,
        push_save=_push_save_to_naukri,
    )


# Re-export for backward compatibility (tracking.py + tests).
__all__ = [
    "_push_save_to_naukri",
    "_list_saved_jobs",
    "_save_job",
    "_sync_saved_jobs_from_naukri",
    "_unsave_job",
    "SAVE_JOB_API",
    "UNSAVE_JOB_API",
    "SAVED_JOBS_API",
]


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
        lambda: _save_job(
            job_id, title=title, company=company, notes=notes,
            sync_to_naukri=sync_to_naukri,
        ),
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
