"""Application tracking tools."""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from naukri_server import mcp
from naukri_server.api import NaukriAPIError
from naukri_server.interfaces import api_client
from naukri_server.error_handler import handle_tool_action
from naukri_server.models import paginate, validate_action_params
from naukri_server.utils import load_json_with_backup, save_json_atomic
from naukri_server.config import (
    logger, APPLICATION_STATUS_API,
    BATCH_APPLY_DEFAULT_DELAY_MS, BATCH_APPLY_DEFAULT_CONCURRENCY,
    APPLICATIONS_FILE,
)
from naukri_server.validation import validate_limit, validate_page

_applications_lock = asyncio.Lock()
_tracking_composite_lock = asyncio.Lock()



def _load_json(path: Path) -> list:
    return load_json_with_backup(path, logger)


def _save_json(path: Path, data: list):
    save_json_atomic(path, data, logger)


async def record_application(job_id: str, title: str = None, company: str = None,
                              status: str = "applied", extra: dict = None):
    """Record or update a job application in SQLite."""
    from naukri_server.database import get_application, upsert_application

    now = datetime.now(timezone.utc).isoformat()
    existing = await get_application(str(job_id))

    if existing:
        # Update: preserve existing fields, only overwrite what's explicitly passed
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


# ---------------------------------------------------------------------------
# Internal helpers for applications (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _list_applications(
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    page: int = 1,
    filter_info: Optional[int] = None,
) -> dict:
    """List tracked job applications with filtering and summary stats."""
    from naukri_server.database import list_applications, count_applications_by_status
    from naukri_server.models import Application

    limit = validate_limit(limit)
    page = validate_page(page)

    # Adjust date_to for end-of-day if no time component
    effective_date_to = date_to
    if date_to and "T" not in date_to:
        effective_date_to = date_to + "T23:59:59"

    offset = (page - 1) * limit

    # Query SQLite with status/date filters
    apps, total = await list_applications(
        status=status, date_from=date_from, date_to=effective_date_to,
        limit=limit, offset=offset,
    )

    # Apply filter_info as post-query filter (recruiter_active/source not all in SQL)
    if filter_info == 1:
        # Recruiter actions — apps where recruiter was active
        apps = [a for a in apps if a.get("recruiter_active") or a.get("job_activity")]
    elif filter_info == 2:
        # Naukri-synced applications
        apps = [a for a in apps if a.get("source") == "naukri_sync"]
    elif filter_info == 3:
        # External/manual applications
        apps = [a for a in apps if a.get("source") != "naukri_sync"]

    # Enrich with computed properties from Application entity
    enriched = []
    for app_dict in apps:
        app = Application.from_dict(app_dict)
        app_dict["is_stale"] = app.is_stale
        app_dict["days_since_applied"] = app.days_since_applied
        app_dict["has_recruiter_interest"] = app.has_recruiter_interest
        enriched.append(app_dict)

    # Status summary across all applications
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


async def _get_application_detail(job_id: str) -> dict:
    """Get detailed status for a specific job application from Naukri API."""
    data = await api_client.get(APPLICATION_STATUS_API, params={"jobId": job_id, "applyType": "normal"})

    job_details = data.get("jobDetails") or {}
    status_steps = data.get("status") or []
    matching = data.get("matchingResults")

    # --- Status timeline with richer detail ---
    timeline = []
    for step in status_steps:
        entry = {"status": step.get("status") or step.get("label", "")}
        if step.get("date"):
            entry["date"] = step["date"]
        if step.get("description"):
            entry["description"] = step["description"]
        if step.get("isCompleted") is not None:
            entry["is_completed"] = step["isCompleted"]
        if step.get("isCurrent") is not None:
            entry["is_current"] = step["isCurrent"]
        if step.get("stepOrder") is not None:
            entry["step_order"] = step["stepOrder"]
        if step.get("subStatus"):
            entry["sub_status"] = step["subStatus"]
        timeline.append(entry)

    # --- Screening question responses ---
    screening_questions = []
    raw_screening = data.get("screeningQuestions") or data.get("questionnaire") or data.get("screeningResponses") or []
    for sq in raw_screening:
        q_entry = {
            "question": sq.get("question") or sq.get("questionText") or sq.get("title", ""),
            "answer": sq.get("answer") or sq.get("response") or sq.get("answerText"),
        }
        if sq.get("questionId"):
            q_entry["question_id"] = sq["questionId"]
        if sq.get("questionType"):
            q_entry["question_type"] = sq["questionType"]
        if sq.get("isMandatory") is not None:
            q_entry["is_mandatory"] = sq["isMandatory"]
        screening_questions.append(q_entry)

    # --- Recruiter details ---
    raw_recruiter = data.get("recruiterDetails") or data.get("recruiter") or job_details.get("recruiterDetails") or job_details.get("recruiter") or {}
    recruiter = None
    if isinstance(raw_recruiter, dict) and raw_recruiter:
        recruiter = {
            "name": raw_recruiter.get("name") or raw_recruiter.get("recruiterName"),
            "designation": raw_recruiter.get("designation") or raw_recruiter.get("title"),
            "company": raw_recruiter.get("company") or raw_recruiter.get("companyName"),
            "profile_url": raw_recruiter.get("profileUrl") or raw_recruiter.get("recruiterProfileUrl"),
            "image_url": raw_recruiter.get("imageUrl") or raw_recruiter.get("photoUrl"),
            "last_active": raw_recruiter.get("lastActive") or raw_recruiter.get("lastActiveDate"),
        }
        # Strip None values from recruiter
        recruiter = {k: v for k, v in recruiter.items() if v is not None}
        if not recruiter:
            recruiter = None

    result = {
        "status": "success",
        "job_id": job_id,
        "title": job_details.get("jobTitle"),
        "company": job_details.get("company"),
        "location": job_details.get("location"),
        "is_open": job_details.get("isOpen"),
        "total_applicants": data.get("totalApplicants"),
        "recruiter_activity": job_details.get("jobActivity"),
        "recruiter_activity_date": job_details.get("jobActivityDate"),
        "match_rating": data.get("starRating"),
        "feedback_stored": data.get("feedbackStored"),
        "status_timeline": timeline,
        "matching_results": matching,
    }

    # --- Application metadata ---
    result["application_date"] = data.get("applicationDate") or data.get("appliedDate") or data.get("applyDate")
    result["current_status"] = data.get("currentStatus") or data.get("applicationStatus")
    result["apply_type"] = data.get("applyType") or data.get("applicationMode")

    # --- Job details enrichment ---
    result["job_url"] = job_details.get("jobUrl") or job_details.get("url") or data.get("jobUrl")
    result["salary_range"] = job_details.get("salaryRange") or job_details.get("salary") or job_details.get("ctcRange")
    result["experience_range"] = job_details.get("experienceRange") or job_details.get("experience")
    result["job_description_snippet"] = job_details.get("jobDescription") or job_details.get("snippet")
    result["job_type"] = job_details.get("jobType") or job_details.get("employmentType")
    result["industry"] = job_details.get("industry")
    result["functional_area"] = job_details.get("functionalArea")
    result["role_category"] = job_details.get("roleCategory")
    result["posted_date"] = job_details.get("postedDate") or job_details.get("createdDate")
    result["expiry_date"] = job_details.get("expiryDate") or job_details.get("validTill")

    # --- Screening and recruiter ---
    if screening_questions:
        result["screening_questions"] = screening_questions
    if recruiter:
        result["recruiter"] = recruiter

    # --- Applicant analytics ---
    result["view_count"] = data.get("viewCount") or data.get("applicationViewCount")
    result["shortlisted"] = data.get("shortlisted") or data.get("isShortlisted")
    result["rejected"] = data.get("rejected") or data.get("isRejected")
    result["ars_score"] = data.get("arsScore") or data.get("ars")
    result["star_rating"] = data.get("starRating")
    result["apply_flow_type"] = data.get("applyFlowType")  # "agentApply" or None
    result["job_activity"] = data.get("jobActivity")  # recruiter action count
    result["job_activity_date"] = data.get("jobActivityDate")
    result["is_crawled"] = data.get("isCrawled")
    # Embedded company rating from AmbitionBox
    company_rating = data.get("companyRating") or data.get("ambitionBoxData")
    if isinstance(company_rating, dict):
        result["company_rating"] = {
            "rating": company_rating.get("Rating") or company_rating.get("rating"),
            "reviews": company_rating.get("ReviewsCount") or company_rating.get("reviewsCount"),
        }

    # --- Additional metadata ---
    result["apply_source"] = data.get("applySource") or data.get("source")
    result["resume_used"] = data.get("resumeUsed") or data.get("resumeName")
    result["cover_letter_used"] = data.get("coverLetterUsed") or data.get("hasCoverLetter")

    # --- Local tracking data ---
    from naukri_server.database import get_application
    local_app = await get_application(str(job_id))
    if local_app:
        result["local_tracking"] = {
            "applied_at": local_app.get("applied_at"),
            "source": local_app.get("source"),
            "fit_score": local_app.get("fit_score"),
        }

    # --- Strip None values to keep response clean ---
    result = {k: v for k, v in result.items() if v is not None}
    result["status"] = "success"

    return result


async def _purge_applications(before_date: str, dry_run: bool = True) -> dict:
    """Delete old applications from SQLite before a given date."""
    from naukri_server.database import list_applications, delete_applications_before

    # Fetch apps that would be purged (for sample preview)
    purge_candidates, purge_count = await list_applications(date_to=before_date, limit=5, offset=0)
    # Get total count that would remain
    _, total = await list_applications(limit=1, offset=0)
    remaining_count = total - purge_count

    sample = [
        {"job_id": a.get("job_id"), "title": a.get("title"), "applied_at": (a.get("applied_at") or "")[:10]}
        for a in purge_candidates
    ]

    if not dry_run and purge_count > 0:
        deleted = await delete_applications_before(before_date)
        purge_count = deleted

    return {
        "status": "success",
        "purged_count": purge_count,
        "remaining_count": remaining_count,
        "dry_run": dry_run,
        "sample_purged": sample,
    }


def _compute_follow_up_priority(app: dict) -> int:
    """Score how worthwhile it is to follow up on this application (0-100)."""
    priority = 50  # baseline

    # Recruiter engaged = strong signal
    job_activity = app.get("job_activity", 0)
    if isinstance(job_activity, (int, float)) and job_activity > 0:
        priority += 20

    # High match score = worth pursuing
    ars = app.get("ars_score")
    if isinstance(ars, (int, float)):
        if ars >= 70:
            priority += 15
        elif ars >= 50:
            priority += 10
        elif ars >= 30:
            priority += 5

    # Company rating boost
    rating = app.get("company_rating")
    if isinstance(rating, dict):
        rating = rating.get("AggregateRating")
    if rating:
        try:
            r = float(rating)
            if r >= 4.0:
                priority += 10
            elif r >= 3.5:
                priority += 5
        except (ValueError, TypeError):
            pass

    # Recency decay
    try:
        applied = datetime.fromisoformat(
            app.get("applied_at", "").replace("+00:00", "+00:00")
        )
        days = (datetime.now(timezone.utc) - applied).days
        if days > 60:
            priority -= 20
        elif days > 45:
            priority -= 10
    except Exception:
        pass

    return max(0, min(100, priority))


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
        from naukri_server.tools.smart_apply import naukri_smart_apply
        return await naukri_smart_apply(job_id=job_id)
    except Exception:
        return None


async def _interview_prep(job_id: str) -> dict:
    """Generate interview prep package for a specific job application."""
    import asyncio

    # Get application details
    from naukri_server.database import get_application
    app = await get_application(str(job_id))
    if not app:
        return {"status": "error", "message": f"No application found for job {job_id}", "error_code": "NOT_FOUND"}

    company = app.get("company", "")
    title = app.get("title", "")

    # Parallel fetch: company intel + mock interview topics + fit assessment
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


async def _draft_follow_up(job_id: str) -> dict:
    """Generate a follow-up message draft for a stale application."""
    from naukri_server.database import get_application
    app = await get_application(str(job_id))
    if not app:
        return {"status": "error", "message": f"No application found for job {job_id}", "error_code": "NOT_FOUND"}

    company = app.get("company", "the company")
    title = app.get("title", "the position")
    applied_at = app.get("applied_at", "")

    # Parse date for message
    try:
        applied_date = datetime.fromisoformat(applied_at.replace("+00:00", "")).strftime("%B %d, %Y")
    except Exception:
        applied_date = "recently"

    # Calculate days since applied
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


async def _recruiter_history() -> dict:
    """Aggregate per-recruiter communication history from applications + inbox."""
    from naukri_server.database import get_recruiter_history

    rows = await get_recruiter_history()

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


async def _get_stale_applications(
    days_threshold: int = 14,
    min_stale_score: int = 40,
    limit: int = 50,
    page: int = 1,
) -> dict:
    """Detect stale job applications that need follow-up or should be abandoned."""
    from naukri_server.database import get_stale_applications_raw, list_applications

    apps = await get_stale_applications_raw(days_threshold)

    # Get total application count for summary
    _, total_applications = await list_applications(limit=1, offset=0)

    if not apps:
        return {"status": "success", "total_applications": total_applications, "total": 0, "count": 0, "page": page, "has_more": False, "stale_applications": []}

    now = datetime.now(timezone.utc)
    stale_apps = []

    for app in apps:
        days_since_apply = int(app.get("days_since_applied", 0))

        # Compute stale score
        stale_score = 0
        reasons = []

        is_open = app.get("is_open")
        if is_open is False:
            stale_score += 100
            reasons.append("Job closed")

        view_count = app.get("view_count")
        has_been_viewed = view_count and view_count > 0
        if days_since_apply > days_threshold and not has_been_viewed:
            stale_score += 60
            reasons.append(f"Applied {days_since_apply}d ago, never viewed")
        elif days_since_apply > days_threshold * 2:
            stale_score += 30
            reasons.append(f"Applied {days_since_apply}d ago")

        job_activity = app.get("job_activity")
        if job_activity is not None and job_activity == 0:
            stale_score += 30
            reasons.append("Zero recruiter activity on posting")

        ars_score = app.get("ars_score")
        if ars_score is not None and ars_score < 30:
            stale_score += 20
            reasons.append(f"Low match score ({ars_score}%)")

        job_activity_date = app.get("job_activity_date")
        if job_activity_date:
            try:
                if "T" in str(job_activity_date):
                    act_dt = datetime.fromisoformat(str(job_activity_date).replace("Z", "+00:00"))
                else:
                    act_dt = datetime.strptime(str(job_activity_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_since_activity = (now - act_dt).days
                if days_since_activity > 14:
                    stale_score += 25
                    reasons.append(f"Recruiter inactive for {days_since_activity}d")
            except (ValueError, TypeError):
                pass

        if stale_score < min_stale_score:
            continue

        # Recommendation
        if stale_score >= 100:
            recommendation = "Move on — job is closed or very stale"
        elif stale_score >= 60:
            recommendation = "Follow up or move on"
        elif stale_score >= 40:
            recommendation = "Consider following up"
        else:
            recommendation = "Still active — wait"

        follow_up_priority = _compute_follow_up_priority(app)

        stale_apps.append({
            "job_id": app.get("job_id"),
            "title": app.get("title"),
            "company": app.get("company"),
            "stale_score": min(stale_score, 100),
            "follow_up_priority": follow_up_priority,
            "reasons": reasons,
            "recommendation": recommendation,
            "applied_date": (app.get("applied_at") or "")[:10],
            "is_open": is_open,
            "view_count": view_count,
            "job_activity": job_activity,
            "ars_score": ars_score,
        })

    # Sort by follow_up_priority descending (most worth following up first)
    stale_apps.sort(key=lambda x: x["follow_up_priority"], reverse=True)

    pagination, page_items = paginate(stale_apps, page, limit)

    return {
        "status": "success",
        "total_applications": total_applications,
        **pagination,
        "stale_applications": page_items,
    }


async def _application_follow_up(
    days_threshold: int = 14,
    min_stale_score: int = 40,
    limit: int = 10,
) -> dict:
    """Cross-reference stale applications with inbox and reminders for follow-up suggestions."""
    from naukri_server.tools.inbox import _fetch_inbox
    from naukri_server.tools.reminders import _list_reminders

    errors = []

    # Parallel: stale apps + inbox + reminders
    stale_result, inbox_result, reminders_result = await asyncio.gather(
        _get_stale_applications(days_threshold=days_threshold, min_stale_score=min_stale_score, limit=limit),
        _fetch_inbox(limit=50, unread_only=False),
        _list_reminders(include_past=True),
        return_exceptions=True,
    )

    if isinstance(stale_result, Exception) or (isinstance(stale_result, dict) and stale_result.get("status") == "error"):
        return {"status": "error", "message": f"Stale detection failed: {stale_result}", "error_code": "API_ERROR"}

    stale_apps = stale_result.get("stale_applications", [])

    # Build inbox index by company (lowercase)
    inbox_by_company: dict = {}
    if not isinstance(inbox_result, Exception) and isinstance(inbox_result, dict):
        for msg in inbox_result.get("messages", []):
            company_details = msg.get("company_details") or {}
            company_name = (company_details.get("company_name") or "").lower()
            if company_name:
                inbox_by_company.setdefault(company_name, []).append(msg)
    else:
        errors.append("Inbox fetch failed — recruiter messages unavailable")

    # Build reminders index by job_id
    reminders_by_job: dict = {}
    if not isinstance(reminders_result, Exception) and isinstance(reminders_result, dict):
        for rem in reminders_result.get("reminders", []):
            reminders_by_job[rem.get("job_id")] = rem
    else:
        errors.append("Reminders fetch failed")

    # Cross-reference
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
# Interview round tracking helpers
# ---------------------------------------------------------------------------

_VALID_ROUND_TYPES = {"phone_screen", "technical", "system_design", "hr", "offer", "other"}
_VALID_ROUND_STATUSES = {"scheduled", "completed", "cancelled"}


async def _add_interview_round(job_id: str, round_type: str, date: str = "",
                                notes: str = "", status: str = "scheduled") -> dict:
    """Track an interview round for an application.

    round_type: "phone_screen", "technical", "system_design", "hr", "offer", "other"
    status: "scheduled", "completed", "cancelled"
    """
    if round_type not in _VALID_ROUND_TYPES:
        return {"status": "error", "message": f"Invalid round_type '{round_type}'. Use: {', '.join(sorted(_VALID_ROUND_TYPES))}", "error_code": "VALIDATION_ERROR"}
    if status not in _VALID_ROUND_STATUSES:
        return {"status": "error", "message": f"Invalid status '{status}'. Use: {', '.join(sorted(_VALID_ROUND_STATUSES))}", "error_code": "VALIDATION_ERROR"}

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

    return {
        "status": "success",
        "action": "round_added",
        "job_id": job_id,
        "round_type": round_type,
        "total_rounds": len(job_rounds),
    }


async def _list_interview_rounds(job_id: str = None) -> dict:
    """List interview rounds, optionally filtered by job_id."""
    from naukri_server.database import list_interview_rounds as db_list_rounds
    rounds = await db_list_rounds(job_id)

    # Group by job
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


async def _compare_offers(job_ids: list) -> dict:
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


# ---------------------------------------------------------------------------
# Application registry — maps action to handler(kwargs) -> dict
# ---------------------------------------------------------------------------

_VALID_PARAMS_PER_ACTION = {
    "list": {"status", "date_from", "date_to", "limit", "page", "filter_info"},
    "detail": {"job_id"},
    "purge": {"before_date", "dry_run"},
    "stale": {"days_threshold", "min_stale_score", "limit", "page"},
    "follow_up": {"days_threshold", "min_stale_score", "limit"},
    "apply": {"job_id", "answers", "set_reminder_days"},
    "batch_apply": {"keywords", "location", "experience", "salary_min", "salary_max",
                    "freshness", "limit", "delay_ms", "max_concurrent",
                    "set_reminder_days", "sort_by", "work_mode", "job_type",
                    "company_type", "answers"},
    "draft_follow_up": {"job_id"},
    "recruiter_history": set(),
    "interview_prep": {"job_id"},
    "add_round": {"job_id", "round_type", "date", "notes", "status"},
    "list_rounds": {"job_id"},
    "compare_offers": {"job_ids"},
}

async def _do_apply(**kw) -> dict:
    """Wrap apply with duplicate check, composite lock, saga orchestration, and optional reminder."""
    from naukri_server.tools.apply import _apply_single
    from naukri_server.tools.jobs import _extract_job_id
    from naukri_server.sagas import SagaExecutor

    job_id = _extract_job_id(kw["job_id"])
    # Check for duplicate application from local tracking
    from naukri_server.database import get_application
    existing = await get_application(str(job_id))
    if existing:
        return {
            "status": "already_applied",
            "message": "You have already applied to this job (from local tracking).",
            "job_id": job_id,
        }

    async with _tracking_composite_lock:
        saga = SagaExecutor("apply_workflow")
        apply_result = {}

        async def do_apply():
            nonlocal apply_result
            apply_result = await _apply_single(job_id, kw.get("answers"), tracking_extra={"source": "single"})
            return apply_result

        saga.add_step("apply", do_apply)

        # Step 2: Set reminder (only if apply succeeds and reminder requested)
        set_reminder_days = kw.get("set_reminder_days")
        if set_reminder_days:
            async def do_reminder():
                if apply_result.get("status") == "applied":
                    from naukri_server.tools.reminders import _set_reminder
                    await _set_reminder(
                        job_id=job_id,
                        days=set_reminder_days,
                        note=f"Follow up on application to {apply_result.get('company', 'unknown')}",
                    )
                    return {"reminder_set": True, "reminder_days": set_reminder_days}
                return {"reminder_set": False}

            saga.add_step("reminder", do_reminder)

        saga_result = await saga.run()

        # Merge saga metadata into the apply result
        if saga_result["status"] == "success" and apply_result:
            apply_result["saga_steps"] = saga_result["completed_steps"]
            # Copy reminder fields from saga results into apply_result
            reminder_result = saga_result.get("results", {}).get("reminder")
            if reminder_result:
                apply_result.update(reminder_result)
            return apply_result

        # Saga failed — return error with saga context
        if saga_result["status"] == "error":
            if apply_result:
                apply_result["saga_steps"] = saga_result.get("completed_steps", [])
                apply_result["saga_errors"] = saga_result.get("errors", [])
                return apply_result
            return saga_result

    return apply_result


async def _do_batch_apply(**kw) -> dict:
    """Wrap batch_apply with composite lock and optional reminders."""
    from naukri_server.tools.apply import _batch_apply

    async with _tracking_composite_lock:
        result = await _batch_apply(
            keywords=kw.get("keywords"), location=kw.get("location"), experience=kw.get("experience"),
            salary_min=kw.get("salary_min"), salary_max=kw.get("salary_max"), sort_by=kw.get("sort_by"),
            freshness=kw.get("freshness"), work_mode=kw.get("work_mode"), job_type=kw.get("job_type"),
            company_type=kw.get("company_type"), limit=kw.get("limit", 50), answers=kw.get("answers"),
            delay_ms=kw.get("delay_ms", BATCH_APPLY_DEFAULT_DELAY_MS),
            max_concurrent=kw.get("max_concurrent", BATCH_APPLY_DEFAULT_CONCURRENCY),
        )
        # Auto-set reminders for successful batch applications
        set_reminder_days = kw.get("set_reminder_days")
        if set_reminder_days and result.get("results"):
            from naukri_server.tools.reminders import _set_reminder
            reminder_count = 0
            for r in result["results"]:
                if r.get("status") == "applied":
                    try:
                        await _set_reminder(
                            job_id=r.get("job_id", ""),
                            days=set_reminder_days,
                            note=f"Follow up on application to {r.get('company', 'unknown')}",
                        )
                        reminder_count += 1
                    except Exception as e:
                        logger.debug("Failed to set reminder for job %s: %s", r.get("job_id", ""), e)
            if reminder_count:
                result["reminders_set"] = reminder_count
                result["reminder_days"] = set_reminder_days
    return result


_APPLICATION_REGISTRY: dict[str, callable] = {
    "list": lambda **kw: _list_applications(status=kw.get("status"), date_from=kw.get("date_from"), date_to=kw.get("date_to"), limit=kw.get("limit", 50), page=kw.get("page", 1), filter_info=kw.get("filter_info")),
    "detail": lambda **kw: _get_application_detail(kw["job_id"]),
    "purge": lambda **kw: _purge_applications(kw["before_date"], kw.get("dry_run", True)),
    "stale": lambda **kw: _get_stale_applications(days_threshold=kw.get("days_threshold", 14), min_stale_score=kw.get("min_stale_score", 40), limit=kw.get("limit", 50), page=kw.get("page", 1)),
    "follow_up": lambda **kw: _application_follow_up(days_threshold=kw.get("days_threshold", 14), min_stale_score=kw.get("min_stale_score", 40), limit=kw.get("limit", 50)),
    "draft_follow_up": lambda **kw: _draft_follow_up(kw["job_id"]),
    "recruiter_history": lambda **kw: _recruiter_history(),
    "interview_prep": lambda **kw: _interview_prep(kw["job_id"]),
    "add_round": lambda **kw: _add_interview_round(kw["job_id"], kw.get("round_type", "other"), kw.get("date", ""), kw.get("notes", ""), kw.get("status", "scheduled")),
    "list_rounds": lambda **kw: _list_interview_rounds(kw.get("job_id")),
    "compare_offers": lambda **kw: _compare_offers([jid.strip() for jid in (kw.get("job_ids") or "").split(",") if jid.strip()]),
    "apply": _do_apply,
    "batch_apply": _do_batch_apply,
}


# ---------------------------------------------------------------------------
# Unified MCP tool for application tracking
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_applications(
    action: str = "list",
    job_id: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    before_date: Optional[str] = None,
    dry_run: bool = True,
    days_threshold: int = 14,
    min_stale_score: int = 40,
    limit: int = 50,
    page: int = 1,
    # apply / batch_apply params
    answers: Optional[dict] = None,
    keywords: Optional[str] = None,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    salary_min: Optional[int] = None,
    salary_max: Optional[int] = None,
    sort_by: Optional[str] = None,
    freshness: Optional[int] = None,
    work_mode: Optional[str] = None,
    job_type: Optional[str] = None,
    company_type: Optional[str] = None,
    delay_ms: int = BATCH_APPLY_DEFAULT_DELAY_MS,
    max_concurrent: int = BATCH_APPLY_DEFAULT_CONCURRENCY,
    set_reminder_days: Optional[int] = None,
    filter_info: Optional[int] = None,
    # interview round params
    round_type: Optional[str] = None,
    date: Optional[str] = None,
    notes: Optional[str] = None,
    # offer comparison
    job_ids: Optional[str] = None,
) -> dict:
    """Unified application tracking — list, detail, purge, stale detection, follow-up, draft, recruiter CRM, interview prep, interview rounds, offer comparison, apply, and batch apply.

    Actions:
      - "list": List tracked applications with filtering (use status/date_from/date_to/limit/page/filter_info)
      - "detail": Get detailed status for ONE application from Naukri API (requires job_id)
      - "purge": Delete old applications from local tracking (requires before_date, use dry_run)
      - "stale": Detect stale applications needing follow-up (use days_threshold/min_stale_score/limit/page)
      - "follow_up": Cross-reference stale apps with inbox & reminders for prioritized action items
        (use days_threshold/min_stale_score/limit)
      - "draft_follow_up": Generate a follow-up message draft for a stale application (requires job_id).
        Returns a ready-to-send message with suggested subject line and days-since-applied context.
      - "recruiter_history": Aggregate per-company communication history from tracked applications.
        Groups by company, counts applications, tracks response status, and sorts by volume.
      - "interview_prep": Generate interview prep package for a job application (requires job_id).
        Parallel-fetches company intel (reviews, interview difficulty), mock interview topics,
        and fit/skill-match assessment. Returns a single prep bundle with matched/missing skills,
        sample questions, and company rating.
      - "add_round": Track an interview round for a job application (requires job_id, round_type).
        Stores rounds in interview_rounds.json. round_type: phone_screen/technical/system_design/hr/offer/other.
      - "list_rounds": List interview rounds, optionally filtered by job_id. Groups by job.
      - "compare_offers": Compare multiple job offers side by side (requires job_ids as comma-separated string).
        Returns application data, interview round counts, and latest round for each job.
      - "apply": Apply to a single job (requires job_id; optional answers for screening questions).
        If set_reminder_days is provided and apply succeeds, a follow-up reminder is auto-created.
      - "batch_apply": Search and apply to multiple jobs (requires keywords; optional location/experience/filters).
        If set_reminder_days is provided, reminders are auto-created for each successful application.

    Args:
        action: "list" | "detail" | "purge" | "stale" | "follow_up" | "draft_follow_up" | "recruiter_history" | "interview_prep" | "add_round" | "list_rounds" | "compare_offers" | "apply" | "batch_apply"
        job_id: Required for detail/apply/interview_prep/add_round — the Naukri job ID (e.g. "270226007446")
        status: (list) Filter by status ("applied", "needs_input", "already_applied", "error")
        date_from: (list) ISO date, include applications on/after this date (e.g. "2026-02-01")
        date_to: (list) ISO date, include applications on/before this date (e.g. "2026-02-28")
        before_date: (purge) ISO date (YYYY-MM-DD). Delete applications applied before this date.
        dry_run: (purge) If True (default), only preview — don't actually delete.
        days_threshold: (stale/follow_up) Consider apps older than N days (default 14)
        min_stale_score: (stale/follow_up) Minimum staleness score 0-100 to include (default 40)
        limit: Max results per page (default 50); batch_apply max jobs to apply to
        page: Page number for pagination (default 1)
        answers: (apply/batch_apply) Dict of answers for screening questions. Keys can be question ID
                 or question text substring: {"current ctc": "16", "notice period": "30"}
        keywords: (batch_apply) Job title or skills (e.g. "python developer")
        location: (batch_apply) City name (e.g. "Bangalore", "Remote")
        experience: (batch_apply) Years of experience filter
        salary_min: (batch_apply) Minimum salary in lakhs
        salary_max: (batch_apply) Maximum salary in lakhs
        sort_by: (batch_apply) "relevance" or "date"
        freshness: (batch_apply) Job age in days — 1, 3, 7, 15, 30
        work_mode: (batch_apply) "wfh", "hybrid", "wfo"
        job_type: (batch_apply) "fulltime", "parttime", "contract", "internship", "temporary"
        company_type: (batch_apply) "startup", "mnc", "indian_mnc", "corporate"
        delay_ms: (batch_apply) Delay in ms between submissions (default 500)
        max_concurrent: (batch_apply) Max parallel applications (default 3)
        set_reminder_days: (apply/batch_apply) If set, auto-create a follow-up reminder N days after
                           successful application. Ignored if apply fails.
        filter_info: Filter by source — 1=recruiter-active jobs, 2=naukri-synced, 3=external/manual. Applied to local tracking data.
        round_type: (add_round) "phone_screen", "technical", "system_design", "hr", "offer", "other"
        date: (add_round) ISO date/datetime for the interview round (defaults to now)
        notes: (add_round) Free-text notes about the round
        job_ids: (compare_offers) Comma-separated job IDs to compare (e.g. "123,456,789")

    Returns:
        - list: {status, total, count, page, has_more, summary: {total_all_statuses, by_status}, applications: [...]}
        - detail: {status, job_id, title, company, location, is_open, total_applicants,
                   recruiter_activity, match_rating, status_timeline, screening_questions, recruiter, ...}
        - purge: {status, purged_count, remaining_count, dry_run, sample_purged}
        - stale: {status, total_applications, total, count, page, has_more, stale_applications: [...]}
        - follow_up: {status, summary: {total_stale, with_recruiter_contact, with_pending_reminder},
                      stale_applications: [...], action_items: [{priority, action, job_id, company, title}]}
        - draft_follow_up: {status, job_id, company, title, days_since_applied, draft_message, suggested_subject}
        - recruiter_history: {status, total_companies, responsive_count, unresponsive_count,
                              companies: [{company, applications, statuses, first_applied, last_applied, has_response}]}
        - interview_prep: {status, job_id, company, title, applied_at, ars_score,
                           company_rating, interview_difficulty, sample_questions,
                           mock_topics, matched_skills, missing_skills}
        - add_round: {status, action: "round_added", job_id, round_type, total_rounds}
        - list_rounds: {status, total_rounds, jobs_with_rounds, rounds: [...]}
        - compare_offers: {status, count, offers: [{job_id, title, company, status, ars_score,
                           interview_rounds, latest_round}]}
        - apply: {status: "applied"/"needs_input"/"already_applied"/"error", job_id, ...}
        - batch_apply: {status: "success"/"partial_success"/"error", searched, filtered, applied,
                        already_applied, needs_input, errors, pending_questions, results}
        - {status: "error", message} on failure
    """
    # ── ISP: warn about params irrelevant to chosen action ────────────
    _provided = {
        "job_id": job_id, "status": status, "date_from": date_from,
        "date_to": date_to, "before_date": before_date, "dry_run": dry_run if not dry_run == True else None,
        "days_threshold": days_threshold if days_threshold != 14 else None,
        "min_stale_score": min_stale_score if min_stale_score != 40 else None,
        "limit": limit if limit != 50 else None, "page": page if page != 1 else None,
        "answers": answers, "keywords": keywords, "location": location,
        "experience": experience, "salary_min": salary_min, "salary_max": salary_max,
        "sort_by": sort_by, "freshness": freshness, "work_mode": work_mode,
        "job_type": job_type, "company_type": company_type,
        "delay_ms": delay_ms if delay_ms != BATCH_APPLY_DEFAULT_DELAY_MS else None,
        "max_concurrent": max_concurrent if max_concurrent != BATCH_APPLY_DEFAULT_CONCURRENCY else None,
        "set_reminder_days": set_reminder_days, "filter_info": filter_info,
        "round_type": round_type, "date": date, "notes": notes, "job_ids": job_ids,
    }
    _unused = validate_action_params(action, _provided, _VALID_PARAMS_PER_ACTION)

    # ── Pre-validation ──────────────────────────────────────────────────
    if action in ("detail", "draft_follow_up", "interview_prep", "apply", "add_round") and not job_id:
        return {"status": "error", "message": f"{action} requires job_id.", "error_code": "VALIDATION_ERROR"}
    if action == "purge" and not before_date:
        return {"status": "error", "message": "purge requires before_date (ISO YYYY-MM-DD).", "error_code": "VALIDATION_ERROR"}
    if action == "batch_apply" and not keywords:
        return {"status": "error", "message": "batch_apply requires keywords.", "error_code": "VALIDATION_ERROR"}
    if action == "compare_offers" and not job_ids:
        return {"status": "error", "message": "compare_offers requires job_ids (comma-separated).", "error_code": "VALIDATION_ERROR"}

    # ── Helper to attach unused_params to response ───────────────────
    def _attach_unused(result: dict) -> dict:
        if _unused and isinstance(result, dict):
            result["unused_params"] = _unused
        return result

    # ── Registry lookup ──────────────────────────────────────────────
    handler = _APPLICATION_REGISTRY.get(action)
    if handler:
        kw = {
            "status": status, "date_from": date_from, "date_to": date_to,
            "limit": limit, "page": page, "filter_info": filter_info,
            "job_id": job_id, "before_date": before_date, "dry_run": dry_run,
            "days_threshold": days_threshold, "min_stale_score": min_stale_score,
            "answers": answers, "keywords": keywords, "location": location,
            "experience": experience, "salary_min": salary_min, "salary_max": salary_max,
            "sort_by": sort_by, "freshness": freshness, "work_mode": work_mode,
            "job_type": job_type, "company_type": company_type,
            "delay_ms": delay_ms, "max_concurrent": max_concurrent,
            "set_reminder_days": set_reminder_days,
            "round_type": round_type, "date": date, "notes": notes,
            "job_ids": job_ids,
        }
        return _attach_unused(await handle_tool_action(lambda: handler(**kw), f"applications.{action}"))

    return {
        "status": "error",
        "message": f"Unknown action '{action}'. Use: {', '.join(_APPLICATION_REGISTRY)}",
        "error_code": "VALIDATION_ERROR",
    }


# ---------------------------------------------------------------------------
# Backward-compat re-exports: symbols that moved to saved_jobs.py / analytics.py
# but are imported by other modules via "from tracking import ..."
# ---------------------------------------------------------------------------
from naukri_server.tools.saved_jobs import (  # noqa: E402, F401
    _list_saved_jobs,
    _save_job,
    _unsave_job,
    _push_save_to_naukri,
    _sync_saved_jobs_from_naukri,
    naukri_saved_jobs,
)
from naukri_server.tools.analytics import _get_match_analytics  # noqa: E402, F401
