"""Reminder service — business logic for follow-up reminders.

Extracted from tools/reminders.py as part of Wave 2B layer-separation refactor.
Tools become thin orchestrators that delegate to this service.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from naukri_server.config import logger

__all__ = [
    "set_reminder",
    "list_reminders",
]


async def set_reminder(
    job_id: str,
    days: int = 7,
    note: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
) -> dict:
    """Set or update a follow-up reminder for a job application."""
    from naukri_server.database import get_reminder, upsert_reminder

    logger.info("Setting reminder for job %s in %d days", job_id, days)
    if days < 1 or days > 365:
        return {
            "status": "error",
            "message": "days must be between 1 and 365",
            "error_code": "VALIDATION_ERROR",
        }

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

        try:
            from naukri_server.events import event_bus, ReminderSet
            await event_bus.emit(ReminderSet(
                job_id=job_id,
                company=company or existing.get("company", ""),
                remind_at=remind_at,
                days=days,
            ))
        except Exception:
            pass

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

    # Emit ReminderSet for both new and updated reminders
    try:
        from naukri_server.events import event_bus, ReminderSet
        await event_bus.emit(ReminderSet(
            job_id=job_id,
            company=company or "",
            remind_at=remind_at,
            days=days,
        ))
    except Exception:
        pass

    return {
        "status": "success",
        "job_id": job_id,
        "remind_at": remind_at,
        "note": note,
        "message": f"Reminder set — due in {days} days.",
    }


async def list_reminders(
    include_past: bool = True,
    include_app_status: bool = False,
    emit_events: bool = False,
) -> dict:
    """Get all follow-up reminders, highlighting due ones.

    Args:
        include_past: Include already-due reminders (default True).
        include_app_status: Batch-fetch live application status for each reminder (default False).
        emit_events: Emit a ReminderDue event per past-due reminder. OFF by
            default because this is a READ, and a read must not mutate.

    Until 2026-08-21 this function emitted unconditionally, which made every
    caller a writer: `naukri_list_reminders`, `naukri_daily_brief` and
    `naukri_follow_up_priority` all land here, each ReminderDue banks a
    high-priority notification, and all 50 of his reminders are months past
    due. One brief call therefore minted ~50 notifications while delivering
    10 - the brief poisoned its own top recommended action. Measured on his
    live DB: 1,127 ReminderDue events in 24h and 1,075 of 1,241 undelivered
    notifications.

    Only `scheduler_tasks._task_reminder_check` (hourly) opts in - that task
    exists to notify him, and tests/test_reminder_storm.py pins it as the sole
    opt-in so a future read path cannot quietly re-arm this.
    """
    from naukri_server.database import list_reminders as db_list_reminders

    logger.info(
        "Listing reminders (include_past=%s, include_app_status=%s)",
        include_past, include_app_status,
    )
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
            if emit_events:
                try:
                    from naukri_server.events import event_bus, ReminderDue
                    await event_bus.emit(ReminderDue(
                        job_id=r.get("job_id", ""),
                        company=r.get("company", ""),
                        title=r.get("title", ""),
                        note=r.get("note", ""),
                    ))
                except Exception:
                    pass

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
