"""Follow-up reminders for job applications.

Tool layer — business logic lives in ``services/reminder_service.py``.
Helpers ``_set_reminder`` and ``_list_reminders`` are re-exported here so
existing call sites (e.g. ``daily_brief``, ``test_events``) and patches
(``patch("naukri_server.tools.reminders._set_reminder", ...)``) keep working.
"""

from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action
from naukri_server.services.reminder_service import (
    set_reminder as _set_reminder,
    list_reminders as _list_reminders,
    dismiss_reminder as _dismiss_reminder,
)

# Re-export for backward compatibility (other modules + test patches import these)
__all__ = ["_set_reminder", "_list_reminders", "_dismiss_reminder"]


# ---------------------------------------------------------------------------
# Single-purpose MCP tools
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


@mcp.tool()
async def naukri_dismiss_reminder(job_id: str) -> dict:
    """Dismiss (delete) a follow-up reminder once it has been dealt with.

    The way a reminder ENDS. A reminder that is only ever re-dated stays due
    forever and is re-notified on every hourly reminder check; dismissing it
    is what stops that for good.

    Local only: this removes one row from the local reminders table. Nothing
    is withdrawn or changed on Naukri, and the reminder can be recreated at
    any time with naukri_set_reminder.

    Args:
        job_id: The job ID whose reminder should be dismissed

    Returns:
        {status, job_id, message} on success, or
        {status: "error", job_id, message, error_code: "NOT_FOUND"} if no
        reminder exists for that job_id.
    """
    return await handle_tool_action(
        lambda: _dismiss_reminder(job_id=job_id),
        "reminders.dismiss",
    )
