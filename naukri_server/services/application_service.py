"""Application service — business logic for application tracking, follow-up, and analysis."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from naukri_server.interfaces import api_client
from naukri_server.models import paginate
from naukri_server.config import (
    logger, APPLICATION_STATUS_API, STALE_MIN_SCORE, STALE_THRESHOLD_DAYS,
)
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


async def purge_applications(before_date: Optional[str] = None,
                             dry_run: bool = True) -> dict:
    """Archive-then-delete applications whose REAL apply date precedes a date.

    This is now the ONLY path that removes an application. The sync saga used to
    carry a silent copy of it; that is gone (see tools/sync.py).

    The preview and the deletion share one predicate --
    ``list_purgeable_applications`` -- so a dry run cannot report a different
    set from the one a real run would remove. Rows with no trustworthy apply
    date are excluded from BOTH: see database.real_applied_date.
    """
    from naukri_server.database import (
        list_applications as db_list,
        list_purgeable_applications,
        delete_applications_before,
    )

    # Retention config lives HERE now. It used to drive the sync saga's silent
    # purge; retention is an explicit, previewable, archived operation, so the
    # horizon belongs to the tool that performs one.
    if before_date is None:
        from naukri_server.config import AUTO_PURGE_DAYS
        from naukri_server import policy as _policy
        days = _policy.setting("retention.auto_purge_days", AUTO_PURGE_DAYS)
        before_date = (
            datetime.now(timezone.utc) - timedelta(days=int(days))
        ).isoformat()

    purgeable = await list_purgeable_applications(before_date)
    purge_count = len(purgeable)
    _, total = await db_list(limit=1, offset=0)
    remaining_count = total - purge_count

    sample = [
        {"job_id": a.get("job_id"), "title": a.get("title"),
         "applied_at": (a.get("applied_at") or "")[:10]}
        for a in purgeable[:5]
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

    # Make the guard VISIBLE. If he asks to purge everything before today and
    # nothing goes, the reason must be in the response rather than inferred
    # from a zero -- that is the "hard 0 that means it crashed" shape.
    from naukri_server.database import list_all_applications, real_applied_date
    all_apps = await list_all_applications()
    protected = sum(1 for a in all_apps if real_applied_date(a) is None)

    return {
        "status": "success",
        "purged_count": purge_count,
        "remaining_count": remaining_count,
        "dry_run": dry_run,
        "sample_purged": sample,
        "protected_no_apply_date": protected,
        "archived": bool(not dry_run and purge_count),
        "note": (
            "%d application(s) carry no trustworthy apply date and were NOT "
            "considered for deletion; applied_at is an insert timestamp, not "
            "the date you applied. Purged rows are copied to "
            "applications_archive first." % protected
        ),
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

# These three used to swallow the exception and return None, and interview_prep
# then OMITTED the corresponding fields while still reporting status "success".
# They now return an ERROR DICT instead of None, so the caller can say what is
# missing and why. Returning a dict rather than a (value, error) tuple is
# deliberate: it keeps the single-return signature every existing caller and
# test mock already expects.
def _fetch_error(label, exc):
    return {"status": "error",
            "message": "%s: %s: %s" % (label, type(exc).__name__, exc),
            "error_code": "API_ERROR"}


async def _safe_fetch_company_intel(company):
    try:
        from naukri_server.tools.ambitionbox import naukri_company_intel
        return await naukri_company_intel(company=company, intel_type="interviews")
    except Exception as e:
        return _fetch_error("Company intel", e)


async def _safe_fetch_mock_topics():
    # `naukri_mock_interview(action="topics")` -- the consolidated dispatcher --
    # was removed by the de-consolidation and this call site kept naming it,
    # so the lazy import raised ImportError, the `except` below dressed it as
    # an API error, and EVERY interview_prep call returned partial_success with
    # `mock_topics` missing. The atomic replacement is
    # `naukri_mock_interview_topics()`, which wraps the same `_get_topics` and
    # returns the same {status, total, count, topics, roles} shape.
    #
    # Pinned by tests/test_interview_prep_call_sites.py, which walks all three
    # of these helpers with `ast` and asserts every lazily imported tool name
    # actually exists -- a wrong name here is otherwise invisible until it
    # reaches a live call, dressed as somebody else's failure.
    try:
        from naukri_server.tools.mock_interview import naukri_mock_interview_topics
        return await naukri_mock_interview_topics()
    except Exception as e:
        return _fetch_error("Mock topics", e)


async def _safe_fetch_fit_score(job_id):
    try:
        from naukri_server.tools.smart_apply import naukri_assess_fit
        return await naukri_assess_fit(job_id=job_id)
    except Exception as e:
        return _fetch_error("Fit score", e)


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

    # A MISSING ENRICHMENT MUST SAY SO.
    #
    # This returned status "success" while silently OMITTING company_rating,
    # sample_questions, matched_skills and missing_skills whenever their fetch
    # died -- four of the eight fields the docstring promises, gone with no
    # error channel at all. A caller could not tell "AmbitionBox has no
    # interview data for this company" from "the fetch raised". Measured
    # 2026-08-22: with the session expired the tool returned six scalars and
    # called itself a success. Interview prep with no prep in it.
    #
    # daily_brief's partial_success + errors[] is the house pattern; this is it.
    errors = []

    def _unpack(idx, label):
        r = results[idx]
        if isinstance(r, Exception):
            errors.append("%s: %s: %s" % (label, type(r).__name__, r))
            return None
        if not isinstance(r, dict):
            errors.append("%s: no data returned" % label)
            return None
        if r.get("status") != "success":
            errors.append("%s: %s" % (label, r.get("message", "unknown")))
            return None
        return r

    company_intel = _unpack(0, "Company intel")
    mock_topics = _unpack(1, "Mock topics")
    fit_data = _unpack(2, "Fit score")

    prep = {
        "status": "success",
        "job_id": job_id,
        "company": company,
        "title": title,
        "applied_at": app.get("applied_at"),
        "ars_score": app.get("ars_score"),
    }

    if company_intel:
        prep["company_rating"] = company_intel.get("overall_rating")
        prep["interview_difficulty"] = company_intel.get("difficulty_breakdown")
        prep["sample_questions"] = company_intel.get("interview_experiences", [])[:3]

    if mock_topics:
        prep["mock_topics"] = mock_topics.get("topics", [])[:5]

    if fit_data:
        skill_match = fit_data.get("fit_assessment", {}).get("skill_match", {})
        prep["matched_skills"] = skill_match.get("matched", [])
        prep["missing_skills"] = skill_match.get("missing", [])

    if errors:
        prep["status"] = "partial_success"
        prep["errors"] = errors
        prep["missing_sections"] = [
            name for name, present in (
                ("company_rating", company_intel),
                ("sample_questions", company_intel),
                ("mock_topics", mock_topics),
                ("matched_skills", fit_data),
                ("missing_skills", fit_data),
            ) if not present
        ]

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


async def recruiter_history(limit: int = 20) -> dict:
    """Aggregate per-recruiter communication history from applications + inbox.

    Args:
        limit: How many company rows to return, most-applied first. The
            *_count fields always describe every company, not this slice.
    """
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

    # Counts describe the WHOLE portfolio; `companies` is only the page shown.
    # Computing them off the slice is what made total_companies read 20 when the
    # real figure was 115.
    shown = companies[:limit] if limit else companies
    return {
        "status": "success",
        "total_companies": len(companies),
        "responsive_count": sum(1 for c in companies if c["has_response"]),
        "unresponsive_count": sum(1 for c in companies if not c["has_response"]),
        "returned": len(shown),
        "has_more": len(shown) < len(companies),
        "companies": shown,
    }


# ---------------------------------------------------------------------------
# Stale application detection & follow-up
# ---------------------------------------------------------------------------

async def get_stale_applications(
    days_threshold: int = STALE_THRESHOLD_DAYS,
    min_stale_score: int = STALE_MIN_SCORE,
    limit: int = 50,
    page: int = 1,
    emit_events: bool = False,
) -> dict:
    """Detect stale job applications that need follow-up or should be abandoned.

    Args:
        emit_events: Emit an ApplicationStale event per high-priority stale
            application. OFF by default because this is a READ, and a read must
            not mutate.

    Until 2026-08-21 this function emitted unconditionally, which made every
    caller a writer. Four of the five callers are reads -
    ``naukri_stale_applications``, ``naukri_daily_brief``,
    ``application_follow_up`` (naukri_follow_up_priority) and the HTTP
    dashboard's /api/stale - and each ApplicationStale banks a notification.
    Measured on his live DB: 85 notifications over 17 distinct applications,
    in five identical bursts of 17. Four of those bursts sat on the 6h
    stale_check cadence; the fifth (2026-08-21T15:07) did not, which is a read
    caught in the act.

    Only ``scheduler_tasks._task_stale_check`` opts in - that task exists to
    notify him, and tests/test_read_path_purity.py pins it as the sole opt-in.
    """
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

    # Emit events for high-priority stale applications - opt-in only, see docstring
    if emit_events:
        from naukri_server.events import event_bus, ApplicationStale
        for sa in stale_apps:
            if sa.get("follow_up_priority", 0) >= 60:
                # Compute days_since_applied from the applied_date string
                # (YYYY-MM-DD). tzinfo MUST be attached: applied_date is a bare
                # date, fromisoformat returns a NAIVE datetime, and subtracting
                # that from an aware utcnow raises TypeError - which the except
                # below swallowed, so this field was 0 on every event ever
                # emitted. All 85 ApplicationStale rows in his event_log carry
                # "days_since_applied": "0" for applications months old.
                _days = 0
                try:
                    _applied = sa.get("applied_date", "")
                    if _applied:
                        _applied_dt = datetime.fromisoformat(_applied)
                        if _applied_dt.tzinfo is None:
                            _applied_dt = _applied_dt.replace(tzinfo=timezone.utc)
                        _days = (datetime.now(timezone.utc) - _applied_dt).days
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
    days_threshold: int = STALE_THRESHOLD_DAYS,
    min_stale_score: int = STALE_MIN_SCORE,
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
        # Says in the PAYLOAD what the docstring says in prose: this row lives
        # in naukri.db and nowhere else. Naukri has no jobseeker interview
        # surface to write to, so a caller that reads only the result must not
        # be able to come away thinking the platform was told.
        "scope": "local",
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
        # Local notebook, not a view of Naukri. An empty list here means
        # nothing was recorded locally -- never that there are no interviews.
        "scope": "local",
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
