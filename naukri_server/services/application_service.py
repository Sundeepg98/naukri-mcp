"""Application service — business logic for application tracking, follow-up, and analysis."""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from naukri_server.interfaces import api_client
from naukri_server.models import paginate
from naukri_server.config import logger, APPLICATION_STATUS_API
from naukri_server.validation import validate_limit, validate_page
from naukri_server.domain.application import (
    StalenessReport,
    compute_follow_up_priority as _domain_follow_up_priority,
)

__all__ = [
    "record_application",
    "list_applications",
    "get_application_detail",
    "purge_applications",
    "compute_follow_up_priority",
    "interview_prep",
    "draft_follow_up",
    "recruiter_history",
    "get_stale_applications",
    "application_follow_up",
    "add_interview_round",
    "list_interview_rounds",
    "compare_offers",
    "VALID_ROUND_TYPES",
    "VALID_ROUND_STATUSES",
]


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------

async def record_application(job_id: str, title: Optional[str] = None, company: Optional[str] = None,
                              status: str = "applied", extra: Optional[dict] = None):
    """Record or update a job application in SQLite."""
    from naukri_server.database import get_application, upsert_application

    now = datetime.now(timezone.utc).isoformat()
    existing = await get_application(str(job_id))

    if existing:
        existing["status"] = status
        existing["updated_at"] = now
        if title:
            existing["title"] = title
        if company:
            existing["company"] = company
        if extra:
            existing.update(extra)
        await upsert_application(existing)
    else:
        entry = {
            "job_id": str(job_id),
            "title": title,
            "company": company,
            "status": status,
            "applied_at": now,
            **(extra or {}),
        }
        await upsert_application(entry)


async def list_applications(
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    page: int = 1,
    filter_info: Optional[int] = None,
) -> dict:
    """List tracked job applications with filtering and summary stats."""
    from naukri_server.database import list_applications as db_list, count_applications_by_status
    from naukri_server.models import Application

    limit = validate_limit(limit)
    page = validate_page(page)

    effective_date_to = date_to
    if date_to and "T" not in date_to:
        effective_date_to = date_to + "T23:59:59"

    offset = (page - 1) * limit

    apps, total = await db_list(
        status=status, date_from=date_from, date_to=effective_date_to,
        limit=limit, offset=offset,
    )

    if filter_info == 1:
        apps = [a for a in apps if a.get("recruiter_active") or a.get("job_activity")]
    elif filter_info == 2:
        apps = [a for a in apps if a.get("source") == "naukri_sync"]
    elif filter_info == 3:
        apps = [a for a in apps if a.get("source") != "naukri_sync"]

    enriched = []
    for app_dict in apps:
        app = Application.from_dict(app_dict)
        app_dict["is_stale"] = app.is_stale
        app_dict["days_since_applied"] = app.days_since_applied
        app_dict["has_recruiter_interest"] = app.has_recruiter_interest
        enriched.append(app_dict)

    by_status = await count_applications_by_status()

    return {
        "status": "success",
        "total": total,
        "count": len(enriched),
        "page": page,
        "has_more": (offset + limit) < total,
        "summary": {"total_all_statuses": sum(by_status.values()), "by_status": by_status},
        "applications": enriched,
    }


async def get_application_detail(job_id: str) -> dict:
    """Get detailed status for a specific job application from Naukri API."""
    from naukri_server.domain.application_detail import from_api_response

    data = await api_client.get(APPLICATION_STATUS_API, params={"jobId": job_id, "applyType": "normal"})
    result = from_api_response(data, job_id)

    # Merge local tracking data if available
    from naukri_server.database import get_application
    local_app = await get_application(str(job_id))
    if local_app:
        result["local_tracking"] = {
            "applied_at": local_app.get("applied_at"),
            "source": local_app.get("source"),
            "fit_score": local_app.get("fit_score"),
        }

    return result


async def purge_applications(before_date: str, dry_run: bool = True) -> dict:
    """Delete old applications from SQLite before a given date."""
    from naukri_server.database import list_applications as db_list, delete_applications_before

    purge_candidates, purge_count = await db_list(date_to=before_date, limit=5, offset=0)
    _, total = await db_list(limit=1, offset=0)
    remaining_count = total - purge_count

    sample = [
        {"job_id": a.get("job_id"), "title": a.get("title"), "applied_at": (a.get("applied_at") or "")[:10]}
        for a in purge_candidates
    ]

    if not dry_run and purge_count > 0:
        deleted = await delete_applications_before(before_date)
        purge_count = deleted

        try:
            from naukri_server.events import event_bus, ApplicationsPurged
            await event_bus.emit(ApplicationsPurged(
                purged_count=purge_count,
                before_date=before_date,
            ))
        except Exception:
            pass

    return {
        "status": "success",
        "purged_count": purge_count,
        "remaining_count": remaining_count,
        "dry_run": dry_run,
        "sample_purged": sample,
    }


# ---------------------------------------------------------------------------
# Follow-up scoring
# ---------------------------------------------------------------------------

def compute_follow_up_priority(app: dict) -> int:
    """Score how worthwhile it is to follow up on this application (0-100).

    Delegates to domain.application.compute_follow_up_priority for the
    5-factor scoring model (activity, ARS, rating, recency, base=50).
    """
    return _domain_follow_up_priority(app)


# ---------------------------------------------------------------------------
# Interview prep helpers
# ---------------------------------------------------------------------------

async def _safe_fetch_company_intel(company):
    try:
        from naukri_server.tools.ambitionbox import naukri_company_intel
        return await naukri_company_intel(company=company, intel_type="interviews")
    except Exception:
        return None


async def _safe_fetch_mock_topics():
    try:
        from naukri_server.tools.mock_interview import naukri_mock_interview
        return await naukri_mock_interview(action="topics")
    except Exception:
        return None


async def _safe_fetch_fit_score(job_id):
    try:
        from naukri_server.tools.smart_apply import naukri_assess_fit
        return await naukri_assess_fit(job_id=job_id)
    except Exception:
        return None


async def interview_prep(job_id: str) -> dict:
    """Generate interview prep package for a specific job application."""
    from naukri_server.database import get_application
    app = await get_application(str(job_id))
    if not app:
        return {"status": "error", "message": f"No application found for job {job_id}", "error_code": "NOT_FOUND"}

    company = app.get("company", "")
    title = app.get("title", "")

    results = await asyncio.gather(
        _safe_fetch_company_intel(company),
        _safe_fetch_mock_topics(),
        _safe_fetch_fit_score(job_id),
        return_exceptions=True,
    )

    company_intel = results[0] if not isinstance(results[0], Exception) else None
    mock_topics = results[1] if not isinstance(results[1], Exception) else None
    fit_data = results[2] if not isinstance(results[2], Exception) else None

    prep = {
        "status": "success",
        "job_id": job_id,
        "company": company,
        "title": title,
        "applied_at": app.get("applied_at"),
        "ars_score": app.get("ars_score"),
    }

    if company_intel and isinstance(company_intel, dict) and company_intel.get("status") == "success":
        prep["company_rating"] = company_intel.get("overall_rating")
        prep["interview_difficulty"] = company_intel.get("difficulty_breakdown")
        prep["sample_questions"] = company_intel.get("interview_experiences", [])[:3]

    if mock_topics and isinstance(mock_topics, dict) and mock_topics.get("status") == "success":
        prep["mock_topics"] = mock_topics.get("topics", [])[:5]

    if fit_data and isinstance(fit_data, dict) and fit_data.get("status") == "success":
        prep["matched_skills"] = fit_data.get("fit_assessment", {}).get("skill_match", {}).get("matched", [])
        prep["missing_skills"] = fit_data.get("fit_assessment", {}).get("skill_match", {}).get("missing", [])

    return prep


# ---------------------------------------------------------------------------
# Draft follow-up & recruiter history
# ---------------------------------------------------------------------------

async def draft_follow_up(job_id: str) -> dict:
    """Generate a follow-up message draft for a stale application."""
    from naukri_server.database import get_application
    app = await get_application(str(job_id))
    if not app:
        return {"status": "error", "message": f"No application found for job {job_id}", "error_code": "NOT_FOUND"}

    company = app.get("company", "the company")
    title = app.get("title", "the position")
    applied_at = app.get("applied_at", "")

    try:
        applied_date = datetime.fromisoformat(applied_at.replace("+00:00", "")).strftime("%B %d, %Y")
    except Exception:
        applied_date = "recently"

    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(applied_at)
        days_ago = delta.days
    except Exception:
        days_ago = 0

    draft = (
        f"Hi,\n\n"
        f"I applied for the {title} position at {company} on {applied_date} "
        f"({days_ago} days ago). I remain very interested in this opportunity "
        f"and would love to discuss how my experience aligns with the role.\n\n"
        f"Would you be available for a brief conversation? I'm happy to work "
        f"around your schedule.\n\n"
        f"Thank you for your time.\n"
        f"Best regards"
    )

    return {
        "status": "success",
        "job_id": job_id,
        "company": company,
        "title": title,
        "days_since_applied": days_ago,
        "draft_message": draft,
        "suggested_subject": f"Follow-up: {title} Application at {company}",
    }


async def recruiter_history() -> dict:
    """Aggregate per-recruiter communication history from applications + inbox."""
    from naukri_server.database import get_recruiter_history as db_get_history

    rows = await db_get_history()

    _response_statuses = {"interview", "viewed", "shortlisted", "offered"}
    companies = []
    for row in rows:
        statuses_list = (row.get("statuses") or "").split(",")
        has_response = bool(_response_statuses & set(statuses_list))
        companies.append({
            "company": row.get("company", "Unknown"),
            "applications": row.get("applications", 0),
            "statuses": statuses_list,
            "first_applied": row.get("first_applied"),
            "last_applied": row.get("last_applied"),
            "has_response": has_response,
        })

    return {
        "status": "success",
        "total_companies": len(companies),
        "responsive_count": sum(1 for c in companies if c["has_response"]),
        "unresponsive_count": sum(1 for c in companies if not c["has_response"]),
        "companies": companies,
    }


# ---------------------------------------------------------------------------
# Stale application detection & follow-up
# ---------------------------------------------------------------------------

async def get_stale_applications(
    days_threshold: int = 14,
    min_stale_score: int = 40,
    limit: int = 50,
    page: int = 1,
) -> dict:
    """Detect stale job applications that need follow-up or should be abandoned."""
    from naukri_server.database import get_stale_applications_raw, list_applications as db_list

    apps = await get_stale_applications_raw(days_threshold)
    _, total_applications = await db_list(limit=1, offset=0)

    if not apps:
        return {"status": "success", "total_applications": total_applications, "total": 0, "count": 0, "page": page, "has_more": False, "stale_applications": []}

    stale_apps = []

    for app in apps:
        report = StalenessReport.compute(app, days_threshold)

        if report.score < min_stale_score:
            continue

        follow_up_priority = compute_follow_up_priority(app)

        stale_apps.append({
            "job_id": app.get("job_id"),
            "title": app.get("title"),
            "company": app.get("company"),
            "stale_score": report.score,
            "follow_up_priority": follow_up_priority,
            "reasons": report.reasons,
            "recommendation": report.recommendation,
            "applied_date": (app.get("applied_at") or "")[:10],
            "is_open": app.get("is_open"),
            "view_count": app.get("view_count"),
            "job_activity": app.get("job_activity"),
            "ars_score": app.get("ars_score"),
        })

    stale_apps.sort(key=lambda x: x["follow_up_priority"], reverse=True)

    # Emit events for high-priority stale applications
    from naukri_server.events import event_bus, ApplicationStale
    for sa in stale_apps:
        if sa.get("follow_up_priority", 0) >= 60:
            # Compute days_since_applied from the applied_date string (YYYY-MM-DD)
            _days = 0
            try:
                _applied = sa.get("applied_date", "")
                if _applied:
                    _days = (datetime.now(timezone.utc) - datetime.fromisoformat(_applied)).days
            except (ValueError, TypeError):
                pass
            await event_bus.emit(ApplicationStale(
                job_id=sa.get("job_id", ""),
                company=sa.get("company", ""),
                days_since_applied=_days,
                follow_up_priority=sa.get("follow_up_priority", 0),
            ))

    pagination, page_items = paginate(stale_apps, page, limit)

    return {
        "status": "success",
        "total_applications": total_applications,
        **pagination,
        "stale_applications": page_items,
    }


async def application_follow_up(
    days_threshold: int = 14,
    min_stale_score: int = 40,
    limit: int = 10,
) -> dict:
    """Cross-reference stale applications with inbox and reminders for follow-up suggestions."""
    from naukri_server.tools.inbox import _fetch_inbox
    from naukri_server.tools.reminders import _list_reminders

    errors = []

    stale_result, inbox_result, reminders_result = await asyncio.gather(
        get_stale_applications(days_threshold=days_threshold, min_stale_score=min_stale_score, limit=limit),
        _fetch_inbox(limit=50, unread_only=False),
        _list_reminders(include_past=True),
        return_exceptions=True,
    )

    if isinstance(stale_result, Exception) or (isinstance(stale_result, dict) and stale_result.get("status") == "error"):
        return {"status": "error", "message": f"Stale detection failed: {stale_result}", "error_code": "API_ERROR"}

    stale_apps = stale_result.get("stale_applications", [])

    inbox_by_company: dict = {}
    if not isinstance(inbox_result, Exception) and isinstance(inbox_result, dict):
        for msg in inbox_result.get("messages", []):
            company_details = msg.get("company_details") or {}
            company_name = (company_details.get("company_name") or "").lower()
            if company_name:
                inbox_by_company.setdefault(company_name, []).append(msg)
    else:
        errors.append("Inbox fetch failed — recruiter messages unavailable")

    reminders_by_job: dict = {}
    if not isinstance(reminders_result, Exception) and isinstance(reminders_result, dict):
        for rem in reminders_result.get("reminders", []):
            reminders_by_job[rem.get("job_id")] = rem
    else:
        errors.append("Reminders fetch failed")

    enriched = []
    action_items = []
    for app in stale_apps:
        company_lower = (app.get("company") or "").lower()
        job_id = app.get("job_id")

        recruiter_msgs = inbox_by_company.get(company_lower, [])
        pending_reminder = reminders_by_job.get(job_id)

        if recruiter_msgs:
            suggested_action = "Recruiter contacted you — respond to their message"
            priority = "high"
        elif pending_reminder and pending_reminder.get("is_due"):
            suggested_action = "Reminder is due — follow up now"
            priority = "high"
        elif app.get("stale_score", 0) >= 70:
            suggested_action = "Highly stale — consider archiving or sending follow-up"
            priority = "medium"
        else:
            suggested_action = "Monitor — set a reminder if interested"
            priority = "low"

        entry = {
            "job_id": job_id,
            "title": app.get("title"),
            "company": app.get("company"),
            "applied_date": app.get("applied_date"),
            "stale_score": app.get("stale_score"),
            "follow_up_priority": app.get("follow_up_priority", 50),
            "reasons": app.get("reasons", []),
            "recruiter_messages": [
                {"subject": m.get("subject"), "date": m.get("date"), "sender": m.get("sender")}
                for m in recruiter_msgs[:3]
            ],
            "pending_reminder": pending_reminder,
            "suggested_action": suggested_action,
        }
        enriched.append(entry)

        action_items.append({
            "priority": priority,
            "follow_up_priority": app.get("follow_up_priority", 50),
            "action": suggested_action,
            "job_id": job_id,
            "company": app.get("company"),
            "title": app.get("title"),
        })

    priority_order = {"high": 0, "medium": 1, "low": 2}
    action_items.sort(key=lambda x: priority_order.get(x["priority"], 3))

    result = {
        "status": "success",
        "summary": {
            "total_stale": len(stale_apps),
            "with_recruiter_contact": sum(1 for e in enriched if e["recruiter_messages"]),
            "with_pending_reminder": sum(1 for e in enriched if e["pending_reminder"]),
        },
        "stale_applications": enriched,
        "action_items": action_items,
    }
    if errors:
        result["status"] = "partial_success"
        result["errors"] = errors
    return result


# ---------------------------------------------------------------------------
# Interview round tracking
# ---------------------------------------------------------------------------

VALID_ROUND_TYPES = {"phone_screen", "technical", "system_design", "hr", "offer", "other"}
VALID_ROUND_STATUSES = {"scheduled", "completed", "cancelled"}


async def add_interview_round(job_id: str, round_type: str, date: str = "",
                                notes: str = "", status: str = "scheduled") -> dict:
    """Track an interview round for an application."""
    if round_type not in VALID_ROUND_TYPES:
        return {"status": "error", "message": f"Invalid round_type '{round_type}'. Use: {', '.join(sorted(VALID_ROUND_TYPES))}", "error_code": "VALIDATION_ERROR"}
    if status not in VALID_ROUND_STATUSES:
        return {"status": "error", "message": f"Invalid status '{status}'. Use: {', '.join(sorted(VALID_ROUND_STATUSES))}", "error_code": "VALIDATION_ERROR"}

    from naukri_server.database import add_interview_round as db_add_round, list_interview_rounds as db_list_rounds

    round_entry = {
        "job_id": job_id,
        "round_type": round_type,
        "date": date or datetime.now(timezone.utc).isoformat(),
        "notes": notes,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db_add_round(round_entry)
    job_rounds = await db_list_rounds(job_id)

    # Emit interview scheduled event for reactive subscribers
    from naukri_server.events import event_bus, ApplicationInterviewScheduled
    await event_bus.emit(ApplicationInterviewScheduled(
        job_id=job_id,
        round_type=round_type,
        date=round_entry.get("date", ""),
    ))

    return {
        "status": "success",
        "action": "round_added",
        "job_id": job_id,
        "round_type": round_type,
        "total_rounds": len(job_rounds),
    }


async def list_interview_rounds(job_id: Optional[str] = None) -> dict:
    """List interview rounds, optionally filtered by job_id."""
    from naukri_server.database import list_interview_rounds as db_list_rounds
    rounds = await db_list_rounds(job_id)

    by_job = {}
    for r in rounds:
        jid = r.get("job_id", "")
        if jid not in by_job:
            by_job[jid] = []
        by_job[jid].append(r)

    return {
        "status": "success",
        "total_rounds": len(rounds),
        "jobs_with_rounds": len(by_job),
        "rounds": rounds,
    }


async def compare_offers(job_ids: list) -> dict:
    """Compare multiple job offers side by side."""
    from naukri_server.database import get_application, list_interview_rounds as db_list_rounds

    offers = []
    for jid in job_ids:
        app = await get_application(str(jid))
        job_rounds = await db_list_rounds(str(jid))

        offer = {
            "job_id": jid,
            "title": app.get("title", "") if app else "",
            "company": app.get("company", "") if app else "",
            "status": app.get("status", "") if app else "",
            "ars_score": app.get("ars_score") if app else None,
            "interview_rounds": len(job_rounds),
            "latest_round": job_rounds[-1] if job_rounds else None,
        }
        offers.append(offer)

    return {
        "status": "success",
        "count": len(offers),
        "offers": offers,
    }
