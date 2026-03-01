"""Follow-up reminders for job applications."""

import asyncio
import json
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger

_PACKAGE_ROOT = Path(__file__).parent.parent.parent
REMINDERS_FILE = _PACKAGE_ROOT / "reminders.json"
_reminders_lock = asyncio.Lock()


def _load_reminders() -> list:
    if REMINDERS_FILE.exists():
        try:
            return json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            backup = REMINDERS_FILE.with_suffix(".backup")
            if backup.exists():
                try:
                    logger.warning("Reminders corrupted, recovering from backup")
                    return json.loads(backup.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return []
    return []


def _save_reminders(data: list):
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if REMINDERS_FILE.exists():
        backup = REMINDERS_FILE.with_suffix(".backup")
        shutil.copy2(str(REMINDERS_FILE), str(backup))
    tmp = REMINDERS_FILE.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(REMINDERS_FILE))


@mcp.tool()
async def naukri_set_reminder(
    job_id: str,
    days: int = 7,
    note: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
) -> dict:
    """Set a follow-up reminder for a job application.

    Creates a reminder that appears in your daily brief when due.
    Use after applying to track follow-up timing.

    Args:
        job_id: Job ID to set reminder for
        days: Remind after N days from now (default 7)
        note: Optional note (e.g., "Follow up with recruiter", "Check status")
        title: Job title (for display in reminders list)
        company: Company name (for display)

    Returns:
        - {status: "success", job_id, remind_at, note, message}
        - {status: "error", message}
    """
    if days < 1 or days > 365:
        return {"status": "error", "message": "days must be between 1 and 365"}

    now = datetime.now(timezone.utc)
    remind_at = (now + timedelta(days=days)).isoformat()

    async with _reminders_lock:
        reminders = _load_reminders()

        # Check for existing reminder on same job
        existing = next((r for r in reminders if r.get("job_id") == job_id), None)
        if existing:
            existing["remind_at"] = remind_at
            existing["note"] = note or existing.get("note")
            existing["updated_at"] = now.isoformat()
            if title:
                existing["title"] = title
            if company:
                existing["company"] = company
            _save_reminders(reminders)
            return {
                "status": "success",
                "job_id": job_id,
                "remind_at": remind_at,
                "note": note,
                "message": f"Reminder updated — due in {days} days.",
            }

        reminders.append({
            "job_id": job_id,
            "title": title,
            "company": company,
            "remind_at": remind_at,
            "note": note,
            "created_at": now.isoformat(),
        })
        _save_reminders(reminders)

    return {
        "status": "success",
        "job_id": job_id,
        "remind_at": remind_at,
        "note": note,
        "message": f"Reminder set — due in {days} days.",
    }


@mcp.tool()
async def naukri_get_reminders(
    include_past: bool = True,
) -> dict:
    """Get all follow-up reminders, highlighting due ones.

    Returns all reminders with is_due flag. Due reminders have passed
    their remind_at date. Also surfaced in naukri_daily_brief.

    Args:
        include_past: Include already-due reminders (default True)

    Returns:
        - {status: "success", total, due_count,
           reminders: [{job_id, title, company, remind_at, note,
           is_due, days_until_due, created_at}]}
    """
    async with _reminders_lock:
        reminders = _load_reminders()

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

    return {
        "status": "success",
        "total": len(result_list),
        "due_count": due_count,
        "reminders": result_list,
    }
