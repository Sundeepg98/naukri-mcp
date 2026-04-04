"""Follow-up reminders for job applications."""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger
from naukri_server.error_handler import handle_tool_action
from naukri_server.models import validate_action_params


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool + daily_brief)
# ---------------------------------------------------------------------------

async def _set_reminder(
    job_id: str,
    days: int = 7,
    note: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
) -> dict:
    """Set a follow-up reminder for a job application."""
    from naukri_server.database import get_reminder, upsert_reminder

    logger.info("Setting reminder for job %s in %d days", job_id, days)
    if days < 1 or days > 365:
        return {"status": "error", "message": "days must be between 1 and 365", "error_code": "VALIDATION_ERROR"}

    now = datetime.now(timezone.utc)
    remind_at = (now + timedelta(days=days)).isoformat()

    # Check for existing reminder on same job
    existing = await get_reminder(job_id)
    if existing:
        existing["remind_at"] = remind_at
        existing["note"] = note or existing.get("note")
        existing["updated_at"] = now.isoformat()
        if title:
            existing["title"] = title
        if company:
            existing["company"] = company
        await upsert_reminder(existing)
        return {
            "status": "success",
            "job_id": job_id,
            "remind_at": remind_at,
            "note": note,
            "message": f"Reminder updated — due in {days} days.",
        }

    await upsert_reminder({
        "job_id": job_id,
        "title": title,
        "company": company,
        "remind_at": remind_at,
        "note": note,
        "created_at": now.isoformat(),
    })

    return {
        "status": "success",
        "job_id": job_id,
        "remind_at": remind_at,
        "note": note,
        "message": f"Reminder set — due in {days} days.",
    }


async def _list_reminders(include_past: bool = True, include_app_status: bool = False) -> dict:
    """Get all follow-up reminders, highlighting due ones.

    Args:
        include_past: Include already-due reminders (default True).
        include_app_status: Batch-fetch live application status for each reminder (default False).
    """
    from naukri_server.database import list_reminders as db_list_reminders

    logger.info("Listing reminders (include_past=%s, include_app_status=%s)", include_past, include_app_status)
    reminders = await db_list_reminders()

    now = datetime.now(timezone.utc)
    result_list = []
    due_count = 0

    for r in reminders:
        remind_at = r.get("remind_at", "")
        try:
            remind_dt = datetime.fromisoformat(remind_at)
        except (ValueError, TypeError):
            continue

        is_due = now >= remind_dt
        days_until = (remind_dt - now).days if not is_due else -(now - remind_dt).days

        if not include_past and is_due:
            continue

        if is_due:
            due_count += 1

        result_list.append({
            "job_id": r.get("job_id"),
            "title": r.get("title"),
            "company": r.get("company"),
            "remind_at": remind_at,
            "note": r.get("note"),
            "is_due": is_due,
            "days_until_due": days_until,
            "created_at": r.get("created_at"),
        })

    # Sort: due first (by overdue days desc), then upcoming (by days asc)
    result_list.sort(key=lambda x: (not x["is_due"], x["days_until_due"]))

    # Enrich with live application status if requested
    if include_app_status and result_list:
        from naukri_server.tools.tracking import _get_application_detail

        job_ids = [r["job_id"] for r in result_list if r.get("job_id")]
        if job_ids:
            detail_tasks = [_get_application_detail(jid) for jid in job_ids]
            detail_results = await asyncio.gather(*detail_tasks, return_exceptions=True)

            # Build lookup by job_id
            status_by_job = {}
            for jid, detail in zip(job_ids, detail_results):
                if (
                    not isinstance(detail, Exception)
                    and isinstance(detail, dict)
                    and detail.get("status") != "error"
                ):
                    status_by_job[jid] = {
                        "current_status": detail.get("current_status"),
                        "view_count": detail.get("view_count"),
                        "ars_score": detail.get("ars_score"),
                    }

            # Attach to reminders
            for rem in result_list:
                jid = rem.get("job_id")
                if jid and jid in status_by_job:
                    rem["application_status"] = status_by_job[jid]

    return {
        "status": "success",
        "total": len(result_list),
        "due_count": due_count,
        "reminders": result_list,
    }


# ---------------------------------------------------------------------------
# ISP param validation
# ---------------------------------------------------------------------------

_VALID_PARAMS_PER_ACTION = {
    "list": {"include_past", "include_app_status"},
    "set": {"job_id", "days", "note", "title", "company"},
}


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_reminders(
    action: str = "list",
    job_id: Optional[str] = None,
    days: int = 7,
    note: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
    include_past: bool = True,
    include_app_status: bool = True,
) -> dict:
    """[Deprecated — use naukri_list_reminders or naukri_set_reminder instead] Unified reminder management.

    Actions:
      - "list": Get all reminders with is_due flag (use include_past to filter)
      - "set": Create/update a follow-up reminder (requires job_id)

    Args:
        action: "list" | "set"
        job_id: Required for set — the job ID to set reminder for
        days: For set — remind after N days from now (default 7, range 1-365)
        note: For set — optional note (e.g., "Follow up with recruiter")
        title: For set — job title (for display)
        company: For set — company name (for display)
        include_past: For list — include already-due reminders (default True)
        include_app_status: For list — enrich each reminder with live application
            status (current_status, view_count, ars_score). Default True.

    Returns:
        - list: {status, total, due_count, reminders: [{job_id, title, company,
          remind_at, note, is_due, days_until_due, created_at,
          application_status?: {current_status, view_count, ars_score}}]}
        - set: {status, job_id, remind_at, note, message}
        - {status: "error", message} on failure
    """
    # ── ISP: warn about params irrelevant to chosen action ─────────────
    _provided = {
        "job_id": job_id, "days": days if days != 7 else None,
        "note": note, "title": title, "company": company,
        "include_past": include_past if not include_past else None,
        "include_app_status": include_app_status if not include_app_status else None,
    }
    _unused = validate_action_params(action, _provided, _VALID_PARAMS_PER_ACTION)

    def _attach_unused(result: dict) -> dict:
        if _unused and isinstance(result, dict):
            result["unused_params"] = _unused
        return result

    # -- list ---------------------------------------------------------------
    if action == "list":
        return _attach_unused(await handle_tool_action(
            lambda: _list_reminders(include_past=include_past, include_app_status=include_app_status),
            "reminders.list",
        ))

    # -- set ----------------------------------------------------------------
    elif action == "set":
        if not job_id:
            return {"status": "error", "message": "set requires job_id.", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await handle_tool_action(
            lambda: _set_reminder(job_id=job_id, days=days, note=note, title=title, company=company),
            "reminders.set",
        ))

    # -- unknown action -----------------------------------------------------
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, set", "error_code": "VALIDATION_ERROR"}


# ---------------------------------------------------------------------------
# Single-purpose MCP tools (preferred over the unified tool above)
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_list_reminders(
    include_past: bool = True,
    include_app_status: bool = True,
) -> dict:
    """List all follow-up reminders with due status.

    Shows all reminders sorted by urgency — due reminders first, then upcoming.
    Optionally enriches each reminder with live application status from Naukri.

    Args:
        include_past: Include already-due reminders (default True)
        include_app_status: Enrich each reminder with live application
            status (current_status, view_count, ars_score). Default True.

    Returns:
        {status, total, due_count, reminders: [{job_id, title, company,
         remind_at, note, is_due, days_until_due, created_at,
         application_status?: {current_status, view_count, ars_score}}]}
    """
    return await handle_tool_action(
        lambda: _list_reminders(include_past=include_past, include_app_status=include_app_status),
        "reminders.list",
    )


@mcp.tool()
async def naukri_set_reminder(
    job_id: str,
    days: int = 7,
    note: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
) -> dict:
    """Set or update a follow-up reminder for a job application.

    Creates a reminder that triggers after N days. If a reminder already
    exists for this job, it updates the due date and note.

    Args:
        job_id: The job ID to set reminder for
        days: Remind after N days from now (default 7, range 1-365)
        note: Optional note (e.g., "Follow up with recruiter")
        title: Job title (for display)
        company: Company name (for display)

    Returns:
        {status, job_id, remind_at, note, message}
    """
    return await handle_tool_action(
        lambda: _set_reminder(job_id=job_id, days=days, note=note, title=title, company=company),
        "reminders.set",
    )
