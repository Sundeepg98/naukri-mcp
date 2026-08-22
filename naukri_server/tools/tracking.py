"""Application tracking tools — thin MCP dispatcher over ApplicationService."""

from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action
from naukri_server.validation import validate_limit
from naukri_server.interfaces import api_client  # noqa: F401 — re-exported for test patching
from naukri_server.config import (
    logger, BATCH_APPLY_DEFAULT_DELAY_MS, BATCH_APPLY_DEFAULT_CONCURRENCY,
    STALE_MIN_SCORE, STALE_THRESHOLD_DAYS,
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
    before_date: Optional[str] = None,
    dry_run: bool = True,
) -> dict:
    """Archive and delete old applications from local tracking.

    THE ONLY path that removes an application. A sync never deletes.

    Only applications with a TRUSTWORTHY apply date are eligible: `applied_at`
    is the timestamp the row was recorded, not the date you applied, so rows
    that carry no real `applied_date` from Naukri are never removed. The reply
    reports how many were protected that way, so a zero is never ambiguous.

    Every removed row is copied to `applications_archive` first.

    Args:
        before_date: ISO date (YYYY-MM-DD). Remove applications APPLIED before
            this date. Omit to use the configured retention horizon
            (`retention.auto_purge_days`).
        dry_run: If True (default), only preview — don't actually delete.

    Returns:
        {status, purged_count, remaining_count, dry_run, sample_purged,
         protected_no_apply_date, archived, note}
    """
    return await handle_tool_action(
        lambda: _purge_applications(before_date, dry_run),
        "applications.purge",
    )


@mcp.tool()
async def naukri_stale_applications(
    days_threshold: int = STALE_THRESHOLD_DAYS,
    min_stale_score: int = STALE_MIN_SCORE,
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
    days_threshold: int = STALE_THRESHOLD_DAYS,
    min_stale_score: int = STALE_MIN_SCORE,
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
async def naukri_recruiter_history(limit: int = 20) -> dict:
    """Aggregate per-company communication history from tracked applications.

    Groups by company, counts applications, tracks response status, and sorts by volume.

    Args:
        limit: How many company rows to return, most-applied first (default 20).
            The three *_count fields always describe EVERY company you have
            applied to, not just the rows returned here.

    Returns:
        {status, total_companies, responsive_count, unresponsive_count,
         returned, has_more, companies}
    """
    return await handle_tool_action(
        lambda: _recruiter_history(limit=validate_limit(limit)),
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
        max_concurrent: DEPRECATED and IGNORED. Applies are strictly serial
            (concurrency is an automation tell); kept only so existing callers
            do not break
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
)
from naukri_server.tools.analytics import _get_match_analytics  # noqa: E402, F401
