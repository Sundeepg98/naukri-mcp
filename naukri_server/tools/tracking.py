"""Application tracking tools — thin MCP dispatcher over ApplicationService."""

import json
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
# Pending screening questions: the persisted shape, and reading BOTH shapes
#
# A needs_input row records what the recruiter asked under
# `extra.pending_questions`. That field has TWO shapes on disk and both must
# keep working:
#
#   legacy int   -- the COUNT alone. Every row written before the text was
#                   persisted carries this. The questions themselves were
#                   returned to the caller and never stored, so for these rows
#                   the text is GONE and only a live re-fetch brings it back.
#   current list -- the questions themselves: text, type and options.
#
# Never an answer. An answer is personal data; it lives only in the local
# answer cache (questions.json), keyed by question text.
# ---------------------------------------------------------------------------

#: Key under `applications.extra` naming what a needs_input row waits on.
PENDING_QUESTIONS_KEY = "pending_questions"

_NO_COUNT_NOTE = "question count not recorded, re-fetch to see them"


def project_pending_questions(pending) -> list:
    """Project live pending questions into the shape persisted on the row.

    Keeps question TEXT, TYPE and OPTIONS. Drops everything else -- including,
    deliberately, any answer that happens to ride along on the input dict. The
    row records what was ASKED; what was answered belongs in the answer cache.
    """
    projected = []
    for item in pending or []:
        if isinstance(item, str):
            # A third shape reaches this from the JSON->SQLite migration, which
            # round-trips pending_questions as a list of bare question strings
            # (see tests/test_json_migration.py). Skipping non-dicts would drop
            # the text and then report text_recoverable=True over an empty list
            # -- the exact "silently shows nothing" failure this module exists
            # to prevent. The string IS the question, so keep it.
            projected.append({"question": item, "type": "", "options": {}})
            continue
        if not isinstance(item, dict):
            continue
        projected.append({
            "question": item.get("question") or "",
            "type": item.get("type") or "",
            "options": item.get("options") or {},
        })
    return projected


def read_pending_questions(raw) -> dict:
    """Read `extra.pending_questions` in EITHER shape, without ever lying.

    Returns {"count", "questions", "text_recoverable", "note"}.

    `text_recoverable` is the load-bearing field. A legacy row must NEVER be
    rendered as though the text were available: handing back an empty question
    list for a row genuinely blocked on three questions reads as "no
    questions", which is the opposite of the truth. When the text is gone the
    `note` says so in words and the count is still reported.
    """
    if isinstance(raw, list):
        questions = project_pending_questions(raw)
        return {
            "count": len(questions),
            "questions": questions,
            "text_recoverable": True,
            "note": None,
        }
    # bool subclasses int -- exclude it so True never reports as "1 question".
    if isinstance(raw, int) and not isinstance(raw, bool):
        count = max(0, raw)
        noun = "question" if count == 1 else "questions"
        pronoun = "it" if count == 1 else "them"
        return {
            "count": count,
            "questions": [],
            "text_recoverable": False,
            "note": (
                "%d %s, text not recoverable, re-fetch to see %s" % (count, noun, pronoun)
                if count else _NO_COUNT_NOTE
            ),
        }
    return {
        "count": 0,
        "questions": [],
        "text_recoverable": False,
        "note": _NO_COUNT_NOTE,
    }


def _row_pending_raw(row: dict):
    """Pull the raw pending_questions value off an application row.

    The DB packs every non-column field into the `extra` JSON TEXT column, so
    that is where it lives on a row read back from SQLite. Falls back to the
    top level because in-memory rows carry it flat.
    """
    extra = row.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (ValueError, TypeError):
            extra = None
    if isinstance(extra, dict) and PENDING_QUESTIONS_KEY in extra:
        return extra[PENDING_QUESTIONS_KEY]
    return row.get(PENDING_QUESTIONS_KEY)


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
    """Record an interview round in YOUR PRIVATE NOTEBOOK. Nothing reaches Naukri.

    THIS IS A LOCAL RECORD, NOT A CHANNEL TO NAUKRI OR TO THE RECRUITER.
    The round is written to the local `naukri.db` and stays there. It does not
    schedule anything, does not confirm or decline a slot, does not notify the
    recruiter, and never appears on naukri.com. The recruiter cannot see it.

    That is a limit of the platform, not of this server. Naukri exposes no
    jobseeker interview surface at all (measured 2026-08-23): four candidate
    interview endpoints do not exist, against family-local controls that do; a
    live application reports only Applied / Application Sent / Application
    Viewed, with no interview state among them; and the logged-in navigation
    has no interview tab. There is nothing to call. `tests/
    test_interview_surface_absent.py` is the standing record -- read it before
    building anything that claims to schedule.

    One thing does leave the machine, and it is a READ, not a write: the round
    emits `ApplicationInterviewScheduled`, whose subscriber runs the interview
    lifecycle workflow, which fetches company interview data from AmbitionBox
    over HTTP and sets a local reminder. So "local-only write" is exact;
    "makes no network call" would not be.

    Args:
        job_id: The Naukri job ID
        round_type: "phone_screen", "technical", "system_design", "hr", "offer", "other"
        date: ISO date/datetime for the round (defaults to now)
        notes: Free-text notes about the round
        status: "scheduled", "completed", or "cancelled" (default "scheduled")

    Returns:
        {status, action: "round_added", job_id, round_type, total_rounds, scope: "local"}
    """
    return await handle_tool_action(
        lambda: _add_interview_round(job_id, round_type, date, notes, status),
        "applications.add_round",
    )


@mcp.tool()
async def naukri_list_interview_rounds(
    job_id: Optional[str] = None,
) -> dict:
    """List interview rounds from YOUR PRIVATE NOTEBOOK. Naukri is not consulted.

    Reads back only what `naukri_add_interview_round` wrote to the local
    `naukri.db`. This is NOT a view of interviews Naukri knows about -- Naukri
    exposes no jobseeker interview surface to read (see that tool's docstring
    and `tests/test_interview_surface_absent.py`). An empty result means you
    have recorded nothing here; it says nothing about your real interviews.

    Args:
        job_id: Optional Naukri job ID to filter rounds for a specific job

    Returns:
        {status, total_rounds, jobs_with_rounds, rounds, scope: "local"}
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
# What is each stalled application waiting for?
# ---------------------------------------------------------------------------


async def _pending_questions_report(limit: int = 50) -> dict:
    """Build the "what is each stuck application waiting for?" report.

    Read-only: one SELECT plus the local answer cache. No network, no writes.
    """
    from naukri_server.database import list_applications as db_list
    from naukri_server.cache import _load_cache, _cache_key

    limit = validate_limit(limit)
    rows, total = await db_list(status="needs_input", limit=limit, offset=0)

    try:
        cache = _load_cache()
    except Exception as exc:  # noqa: BLE001 - an unreadable cache must not
        # sink the report; it only costs the answered/unanswered marking.
        logger.warning("Answer cache unreadable, treating every question as unanswered: %s", exc)
        cache = {}

    entries = []
    for row in rows:
        read = read_pending_questions(_row_pending_raw(row))

        questions = []
        for q in read["questions"]:
            key = _cache_key(q["question"], q["options"])
            questions.append({**q, "answered": key in cache, "cache_key": key})

        if read["text_recoverable"]:
            unanswered = sum(1 for q in questions if not q["answered"])
            estimate = False
        else:
            # The text is gone, so which of them are already answered CANNOT be
            # known. Assume none are. That is the conservative direction: it can
            # only under-promise. Assuming the opposite would float a genuinely
            # expensive row to the top of a list sorted by cheapness.
            unanswered = read["count"]
            estimate = True

        entry = {
            "job_id": row.get("job_id"),
            "company": row.get("company"),
            "title": row.get("title"),
            "stalled_at": row.get("applied_at"),
            "question_count": read["count"],
            "unanswered_count": unanswered,
            "unanswered_is_estimate": estimate,
            "text_recoverable": read["text_recoverable"],
            "questions": questions,
        }
        if read["note"]:
            entry["note"] = read["note"]
        entries.append(entry)

    # Cheapest win first: fewest OUTSTANDING questions, then fewest questions
    # overall, then job_id so the order is total and stable. An application
    # blocked on one already-answered question is one action from done and must
    # never sit below one blocked on ten.
    entries.sort(key=lambda e: (e["unanswered_count"], e["question_count"], str(e["job_id"] or "")))

    with_text = sum(1 for e in entries if e["text_recoverable"])
    return {
        "status": "success",
        "total_needs_input": total,
        "count": len(entries),
        # The cheapest-first ordering is over the rows RETURNED. If `limit` cut
        # the set short, the true cheapest win could be in the tail that was
        # never fetched -- so say when that is possible rather than implying a
        # global ranking. (17 stalled rows today, against a default of 50.)
        "has_more": total > len(entries),
        "total_questions": sum(e["question_count"] for e in entries),
        "total_unanswered": sum(e["unanswered_count"] for e in entries),
        "with_question_text": with_text,
        "text_not_recoverable": len(entries) - with_text,
        "recovery_hint": (
            "Rows with text_recoverable=false stored only a count; their question "
            "text was never persisted. naukri_recover_pending_questions re-fetches it."
        ),
        "applications": entries,
    }


@mcp.tool()
async def naukri_pending_questions(limit: int = 50) -> dict:
    """Show what every stalled (needs_input) application is waiting for.

    Sorted cheapest-win first: fewest OUTSTANDING (unanswered) questions at the
    top, so an application one answer from done is never buried under one
    blocked on ten.

    Each question is marked `answered` when the local answer cache already
    holds a reply for it -- those need no new input from you, only a re-submit.

    Rows written before the question text was persisted carry `text_recoverable:
    false` and a `note` saying the count is all that survived. They report their
    COUNT honestly and an empty `questions` list; that is not "no questions".
    Call naukri_recover_pending_questions to re-fetch their text.

    Read-only: no network, no writes.

    Args:
        limit: Max applications to report (default 50)

    Returns:
        {status, total_needs_input, count, has_more, total_questions,
         total_unanswered, with_question_text, text_not_recoverable,
         applications: [...]}

        `has_more` true means `limit` truncated the set, so the ordering is
        cheapest-first WITHIN the returned rows only.
    """
    return await handle_tool_action(
        lambda: _pending_questions_report(limit=limit),
        "applications.pending_questions",
    )


@mcp.tool()
async def naukri_recover_pending_questions(
    job_ids: Optional[list] = None,
    confirm: bool = False,
    limit: int = 25,
) -> dict:
    """Re-fetch the screening questions for stalled rows that stored only a count.

    Those rows recorded HOW MANY questions blocked them and not WHAT they were,
    so the only way back is to ask Naukri again. This does that, and submits
    nothing: the re-fetch supplies no answers, so the reply comes back as the
    question list -- the same path that produced these rows in the first place.

    Gated. With `confirm=False` (the default) it touches the network NOT AT ALL:
    it names exactly which job ids would be re-fetched and returns.

    A row is recovered once. Rows whose text is already present are skipped.

    Args:
        job_ids: Restrict to these job ids. Omit for every recoverable row.
        confirm: False (default) previews and performs nothing. True re-fetches.
        limit: Max stalled rows to consider (default 25)

    Returns:
        confirm=False -> {status: "preview", would_refetch_count, would_refetch,
                          skipped_already_recovered, performed: "nothing"}
        confirm=True  -> {status, recovered_count, recovered, failed,
                          applied_unexpectedly, skipped_already_recovered}
    """
    # Function-scope import: tools.apply imports THIS module at module scope,
    # so a top-level import here would close the cycle. Same pattern
    # services/apply_service.py uses for _apply_single.
    from naukri_server.tools.apply import _recover_pending_questions

    return await handle_tool_action(
        lambda: _recover_pending_questions(job_ids=job_ids, confirm=confirm, limit=limit),
        "applications.recover_questions",
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
