"""Application tracking tools — thin MCP dispatcher over ApplicationService."""

from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action
from naukri_server.interfaces import api_client  # noqa: F401 — re-exported for test patching
from naukri_server.models import validate_action_params
from naukri_server.config import (
    logger, BATCH_APPLY_DEFAULT_DELAY_MS, BATCH_APPLY_DEFAULT_CONCURRENCY,
)

# Import business logic from service layer
from naukri_server.services.application_service import (  # noqa: F401
    record_application,
    list_applications as _list_applications,
    get_application_detail as _get_application_detail,
    purge_applications as _purge_applications,
    compute_follow_up_priority as _compute_follow_up_priority,
    interview_prep as _interview_prep,
    draft_follow_up as _draft_follow_up,
    recruiter_history as _recruiter_history,
    get_stale_applications as _get_stale_applications,
    application_follow_up as _application_follow_up,
    add_interview_round as _add_interview_round,
    list_interview_rounds as _list_interview_rounds,
    compare_offers as _compare_offers,
    VALID_ROUND_TYPES as _VALID_ROUND_TYPES,
    VALID_ROUND_STATUSES as _VALID_ROUND_STATUSES,
)

# Apply orchestration — extracted to ApplyService
from naukri_server.services.apply_service import (  # noqa: F401
    apply_with_saga as _do_apply,
    batch_apply_with_reminders as _do_batch_apply,
)


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
    """[Deprecated — use individual tools: naukri_list_applications, naukri_get_application, naukri_purge_applications, naukri_stale_applications, naukri_follow_up_priority, naukri_draft_follow_up, naukri_recruiter_history, naukri_interview_prep, naukri_add_interview_round, naukri_list_interview_rounds, naukri_compare_offers, naukri_apply, naukri_batch_apply]

    Unified application tracking — list, detail, purge, stale detection, follow-up, draft, recruiter CRM, interview prep, interview rounds, offer comparison, apply, and batch apply.

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
        round_type: phone_screen/technical/system_design/hr/offer/other.
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
        answers: (apply/batch_apply) Dict of answers for screening questions
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
        filter_info: Filter by source — 1=recruiter-active jobs, 2=naukri-synced, 3=external/manual.
        round_type: (add_round) "phone_screen", "technical", "system_design", "hr", "offer", "other"
        date: (add_round) ISO date/datetime for the interview round (defaults to now)
        notes: (add_round) Free-text notes about the round
        job_ids: (compare_offers) Comma-separated job IDs to compare (e.g. "123,456,789")

    Returns:
        - list: {status, total, count, page, has_more, summary, applications: [...]}
        - detail: {status, job_id, title, company, ...}
        - purge: {status, purged_count, remaining_count, dry_run, sample_purged}
        - stale: {status, total_applications, total, count, page, has_more, stale_applications: [...]}
        - follow_up: {status, summary, stale_applications, action_items}
        - draft_follow_up: {status, job_id, company, title, days_since_applied, draft_message, suggested_subject}
        - recruiter_history: {status, total_companies, responsive_count, unresponsive_count, companies}
        - interview_prep: {status, job_id, company, title, ...}
        - add_round: {status, action: "round_added", job_id, round_type, total_rounds}
        - list_rounds: {status, total_rounds, jobs_with_rounds, rounds}
        - compare_offers: {status, count, offers}
        - apply: {status: "applied"/"needs_input"/"already_applied"/"error", ...}
        - batch_apply: {status, searched, filtered, applied, ...}
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
# Single-purpose tool functions (replacements for action-parameter pattern)
# ---------------------------------------------------------------------------


@mcp.tool()
async def naukri_list_applications(
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    page: int = 1,
    filter_info: Optional[int] = None,
) -> dict:
    """List tracked job applications with filtering and pagination.

    Args:
        status: Filter by status — "applied", "needs_input", "already_applied", "error"
        date_from: ISO date, include applications on/after this date
        date_to: ISO date, include applications on/before this date
        limit: Max results per page (default 50)
        page: Page number (default 1)
        filter_info: Source filter — 1=recruiter-active, 2=naukri-synced, 3=external

    Returns:
        {status, total, count, page, has_more, summary, applications: [...]}
    """
    return await handle_tool_action(
        lambda: _list_applications(status=status, date_from=date_from, date_to=date_to, limit=limit, page=page, filter_info=filter_info),
        "applications.list",
    )


@mcp.tool()
async def naukri_get_application(
    job_id: str,
) -> dict:
    """Get detailed status for a single application from Naukri API.

    Args:
        job_id: The Naukri job ID (e.g. "270226007446")

    Returns:
        {status, job_id, title, company, applied_at, ...}
    """
    return await handle_tool_action(
        lambda: _get_application_detail(job_id),
        "applications.detail",
    )


@mcp.tool()
async def naukri_purge_applications(
    before_date: str,
    dry_run: bool = True,
) -> dict:
    """Delete old applications from local tracking.

    Args:
        before_date: ISO date (YYYY-MM-DD). Delete applications applied before this date.
        dry_run: If True (default), only preview — don't actually delete.

    Returns:
        {status, purged_count, remaining_count, dry_run, sample_purged}
    """
    return await handle_tool_action(
        lambda: _purge_applications(before_date, dry_run),
        "applications.purge",
    )


@mcp.tool()
async def naukri_stale_applications(
    days_threshold: int = 14,
    min_stale_score: int = 40,
    limit: int = 50,
    page: int = 1,
) -> dict:
    """Detect stale applications that may need follow-up action.

    Args:
        days_threshold: Consider apps older than N days stale (default 14)
        min_stale_score: Minimum staleness score 0-100 to include (default 40)
        limit: Max results per page (default 50)
        page: Page number (default 1)

    Returns:
        {status, total_applications, total, count, page, has_more, stale_applications: [...]}
    """
    return await handle_tool_action(
        lambda: _get_stale_applications(days_threshold=days_threshold, min_stale_score=min_stale_score, limit=limit, page=page),
        "applications.stale",
    )


@mcp.tool()
async def naukri_follow_up_priority(
    days_threshold: int = 14,
    min_stale_score: int = 40,
    limit: int = 10,
) -> dict:
    """Cross-reference stale apps with inbox and reminders for prioritized follow-up action items.

    Args:
        days_threshold: Consider apps older than N days (default 14)
        min_stale_score: Minimum staleness score 0-100 to include (default 40)
        limit: Max action items to return (default 10)

    Returns:
        {status, summary, stale_applications, action_items}
    """
    return await handle_tool_action(
        lambda: _application_follow_up(days_threshold=days_threshold, min_stale_score=min_stale_score, limit=limit),
        "applications.follow_up",
    )


@mcp.tool()
async def naukri_draft_follow_up(
    job_id: str,
) -> dict:
    """Generate a follow-up message draft for a stale application.

    Returns a ready-to-send message with suggested subject line and days-since-applied context.

    Args:
        job_id: The Naukri job ID to draft a follow-up for

    Returns:
        {status, job_id, company, title, days_since_applied, draft_message, suggested_subject}
    """
    return await handle_tool_action(
        lambda: _draft_follow_up(job_id),
        "applications.draft_follow_up",
    )


@mcp.tool()
async def naukri_recruiter_history() -> dict:
    """Aggregate per-company communication history from tracked applications.

    Groups by company, counts applications, tracks response status, and sorts by volume.

    Returns:
        {status, total_companies, responsive_count, unresponsive_count, companies}
    """
    return await handle_tool_action(
        lambda: _recruiter_history(),
        "applications.recruiter_history",
    )


@mcp.tool()
async def naukri_interview_prep(
    job_id: str,
) -> dict:
    """Generate interview prep package for a job application.

    Parallel-fetches company intel (reviews, interview difficulty), mock interview topics,
    and fit/skill-match assessment.

    Args:
        job_id: The Naukri job ID to prepare for

    Returns:
        {status, job_id, company, title, matched_skills, missing_skills, sample_questions, company_rating}
    """
    return await handle_tool_action(
        lambda: _interview_prep(job_id),
        "applications.interview_prep",
    )


@mcp.tool()
async def naukri_add_interview_round(
    job_id: str,
    round_type: str = "other",
    date: str = "",
    notes: str = "",
    status: str = "scheduled",
) -> dict:
    """Track an interview round for a job application.

    Args:
        job_id: The Naukri job ID
        round_type: "phone_screen", "technical", "system_design", "hr", "offer", "other"
        date: ISO date/datetime for the round (defaults to now)
        notes: Free-text notes about the round
        status: "scheduled", "completed", or "cancelled" (default "scheduled")

    Returns:
        {status, action: "round_added", job_id, round_type, total_rounds}
    """
    return await handle_tool_action(
        lambda: _add_interview_round(job_id, round_type, date, notes, status),
        "applications.add_round",
    )


@mcp.tool()
async def naukri_list_interview_rounds(
    job_id: Optional[str] = None,
) -> dict:
    """List interview rounds, optionally filtered by job_id. Groups by job.

    Args:
        job_id: Optional Naukri job ID to filter rounds for a specific job

    Returns:
        {status, total_rounds, jobs_with_rounds, rounds}
    """
    return await handle_tool_action(
        lambda: _list_interview_rounds(job_id),
        "applications.list_rounds",
    )


@mcp.tool()
async def naukri_compare_offers(
    job_ids: str,
) -> dict:
    """Compare multiple job offers side by side.

    Returns application data, interview round counts, and latest round for each job.

    Args:
        job_ids: Comma-separated job IDs to compare (e.g. "123,456,789")

    Returns:
        {status, count, offers}
    """
    return await handle_tool_action(
        lambda: _compare_offers([jid.strip() for jid in job_ids.split(",") if jid.strip()]),
        "applications.compare_offers",
    )


@mcp.tool()
async def naukri_apply(
    job_id: str,
    answers: Optional[dict] = None,
    set_reminder_days: Optional[int] = None,
) -> dict:
    """Apply to a single job on Naukri.

    If set_reminder_days is provided and apply succeeds, a follow-up reminder is auto-created.

    Args:
        job_id: The Naukri job ID to apply to
        answers: Dict of answers for screening questions (from a prior needs_input response)
        set_reminder_days: Auto-create a follow-up reminder N days after successful application

    Returns:
        {status: "applied"/"needs_input"/"already_applied"/"error", job_id, ...}
    """
    return await handle_tool_action(
        lambda: _do_apply(job_id=job_id, answers=answers, set_reminder_days=set_reminder_days),
        "applications.apply",
    )


@mcp.tool()
async def naukri_batch_apply(
    keywords: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    salary_min: Optional[int] = None,
    salary_max: Optional[int] = None,
    freshness: Optional[int] = None,
    limit: int = 50,
    work_mode: Optional[str] = None,
    job_type: Optional[str] = None,
    company_type: Optional[str] = None,
    sort_by: Optional[str] = None,
    delay_ms: int = BATCH_APPLY_DEFAULT_DELAY_MS,
    max_concurrent: int = BATCH_APPLY_DEFAULT_CONCURRENCY,
    set_reminder_days: Optional[int] = None,
    answers: Optional[dict] = None,
) -> dict:
    """Search and apply to multiple jobs in bulk.

    If set_reminder_days is provided, reminders are auto-created for each successful application.

    Args:
        keywords: Job title or skills (e.g. "python developer")
        location: City name (e.g. "Bangalore", "Remote")
        experience: Years of experience filter
        salary_min: Minimum salary in lakhs
        salary_max: Maximum salary in lakhs
        freshness: Job age in days — 1, 3, 7, 15, 30
        limit: Max jobs to apply to (default 50)
        work_mode: "wfh", "hybrid", "wfo"
        job_type: "fulltime", "parttime", "contract", "internship", "temporary"
        company_type: "startup", "mnc", "indian_mnc", "corporate"
        sort_by: "relevance" or "date"
        delay_ms: Delay in ms between submissions (default 500)
        max_concurrent: Max parallel applications (default 3)
        set_reminder_days: Auto-create follow-up reminders N days after each successful application
        answers: Dict of answers for screening questions

    Returns:
        {status, searched, filtered, applied, results: [...]}
    """
    return await handle_tool_action(
        lambda: _do_batch_apply(
            keywords=keywords, location=location, experience=experience,
            salary_min=salary_min, salary_max=salary_max, sort_by=sort_by,
            freshness=freshness, work_mode=work_mode, job_type=job_type,
            company_type=company_type, limit=limit, answers=answers,
            delay_ms=delay_ms, max_concurrent=max_concurrent,
            set_reminder_days=set_reminder_days,
        ),
        "applications.batch_apply",
    )


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
