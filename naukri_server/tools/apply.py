import asyncio
import json
from typing import Optional

from mcp.server.fastmcp import Context

from naukri_server.interfaces import api_client
from naukri_server import kill_switch
from naukri_server.api import NaukriAPIError
from naukri_server.cache import _cache_lock, _load_cache, _save_cache, _cache_key
from naukri_server.config import (
    APPLY_TRAILER, APPLY_WORKFLOW_API, BATCH_APPLY_DEFAULT_DELAY_MS,
    BATCH_APPLY_PER_JOB_TIMEOUT,
    APPLY_JITTER_MIN_SECONDS, APPLY_JITTER_MAX_SECONDS, DAILY_APPLY_QUOTA,
    APPLY_THINK_TIME_MIN_SECONDS, APPLY_THINK_TIME_MAX_SECONDS,
    APPLY_THINK_TIME_MEDIAN_SECONDS, APPLY_THINK_TIME_SIGMA, logger,
)
from naukri_server.resilience import get_apply_rate_limiter, jittered_delay, human_think_time
from naukri_server.events import event_bus, ApplicationSubmitted
from naukri_server.models import ApplicationStatus
from naukri_server.tools.jobs import _extract_job_id
from naukri_server.tools.tracking import record_application, project_pending_questions
from naukri_server.validation import validate_limit
# Pure answer-handling helpers live in services.apply_service — re-exported
# under legacy underscored names so existing imports (tests, _cache_answers)
# keep working without touching every callsite.
from naukri_server.services.apply_service import (
    find_user_answer as _find_user_answer,
    format_answer as _format_answer,
)


# ============================================================================
# Tool 5: Apply to Job (REST API — two-phase)
# ============================================================================


async def _daily_quota_exceeded() -> tuple[bool, int]:
    """Return (exceeded, applied_today) against the HARD DAILY_APPLY_QUOTA.

    Counts today's locally-tracked successful applications. This is a hard
    circuit (not a string warning): once we've applied DAILY_APPLY_QUOTA times we
    STOP, both to respect Naukri's limit and because pushing past a daily cap is
    an easy automation tell. Best-effort: if the count can't be read we fail
    OPEN here (return not-exceeded) so a transient DB hiccup doesn't wedge all
    applies — the kill-switch + rate limiter remain the hard safety stops.
    """
    try:
        from datetime import datetime, timezone
        from naukri_server.database import count_daily_applied
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        applied = await count_daily_applied(today)
        return applied >= DAILY_APPLY_QUOTA, applied
    except Exception as exc:
        logger.warning("Daily-quota check failed (allowing apply): %s", exc)
        return False, 0


async def _verify_apply_readback(job_id: str) -> Optional[bool]:
    """Confirm an apply actually registered by reading the applied-jobs history.

    Returns:
        True  — job_id found in the recent applied-jobs history (confirmed).
        False — history fetched OK but job_id NOT present (suspicious — the POST
                claimed success but nothing registered, e.g. a soft block).
        None  — couldn't verify (read failed / API unavailable); caller should
                treat as "unverified", NOT as a failure.

    Gated by config.VERIFY_APPLY_READBACK; when disabled the caller skips this.
    """
    from naukri_server.config import APPLIED_JOBS_API
    if not APPLIED_JOBS_API:
        return None
    try:
        # First page only — a just-submitted apply is the most recent entry.
        params = {"pageSize": "20", "days": "1", "pageNumber": "1", "filterInfo": "2"}
        data = await api_client.get(APPLIED_JOBS_API, params)
    except Exception as e:
        logger.info("Apply read-back fetch failed for %s (treating as unverified): %s", job_id, e)
        return None
    if not isinstance(data, dict):
        return None
    details = data.get("applyDetails", []) or []
    target = str(job_id)
    for entry in details:
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get("jobId") or entry.get("job_id") or entry.get("id") or "")
        if eid == target:
            return True
    return False


async def _finalize_applied(job_id: str, *, title, company, tracking_extra,
                            base_result: dict) -> dict:
    """Record + return an 'applied' result, optionally verifying via read-back.

    When config.VERIFY_APPLY_READBACK is on, confirm the apply actually landed
    in the applied-jobs history. A failed/absent read-back DOWNGRADES the status
    to 'applied_unverified' and tags ``verified`` — it is never turned into a
    failure (the POST itself reported success). When verification is off, behaves
    exactly as before (status='applied').
    """
    from naukri_server.config import VERIFY_APPLY_READBACK
    status = "applied"
    verified: Optional[bool] = None
    if VERIFY_APPLY_READBACK:
        verified = await _verify_apply_readback(job_id)
        if verified is False:
            status = "applied_unverified"
            logger.warning("Apply to %s reported success but was NOT found on read-back "
                           "— marking applied_unverified", job_id)

    # Always record as "applied" in tracking — the POST reported success; an
    # unverified read-back is a confidence flag (in extra), not a different
    # application state, so dedup/quota counting stays correct.
    await record_application(job_id, title=title, company=company, status="applied",
                             extra={**(tracking_extra or {}),
                                    **({"verified": verified} if verified is not None else {})})
    await event_bus.emit(ApplicationSubmitted(
        job_id=job_id, company=company or "", title=title or "",
    ))
    result = {**base_result, "status": status, "job_id": job_id}
    if verified is not None:
        result["verified"] = verified
    return result


async def _apply_single(job_id: str, answers: Optional[dict] = None,
                         title: Optional[str] = None, company: Optional[str] = None,
                         tracking_extra: Optional[dict] = None) -> dict:
    """Core apply logic — POST to apply endpoint, handle questions from cache.

    Used by both naukri_apply (single) and naukri_batch_apply (parallel).
    """
    if answers is None:
        answers = {}
    job_id = _extract_job_id(job_id)

    # FAIL-CLOSED: if the global kill-switch is tripped (Akamai block / soft
    # lockout detected), do NOT submit — halt immediately. Surfacing this as a
    # halted status (not a generic error) lets batch/agent callers stop the run.
    try:
        kill_switch.guard()
    except kill_switch.KillSwitchTrippedError as e:
        await record_application(job_id, title=title, company=company,
                                 status="error",
                                 extra={"message": str(e), "halted": True, **(tracking_extra or {})})
        return {"status": "halted", "job_id": job_id, "message": str(e),
                "error_code": "KILL_SWITCH_TRIPPED", "block_kind": e.block_kind}

    # HARD daily-quota circuit — stop applying once the daily cap is reached
    # (previously only a non-blocking string "warning"). Pushing past the daily
    # limit is both a policy breach and an automation tell.
    exceeded, applied_today = await _daily_quota_exceeded()
    if exceeded:
        logger.warning("Daily apply quota reached (%d/%d) — refusing apply to %s",
                       applied_today, DAILY_APPLY_QUOTA, job_id)
        return {
            "status": "quota_reached",
            "job_id": job_id,
            "message": f"Daily apply quota reached ({applied_today}/{DAILY_APPLY_QUOTA}). "
                       "Applications halted until tomorrow.",
            "error_code": "DAILY_QUOTA_REACHED",
            "daily_applied": applied_today,
            "daily_quota": DAILY_APPLY_QUOTA,
        }

    try:
        # Throttle every apply (single AND batch route through here) so we never
        # exceed a human-plausible sustained cadence — a constant/burst cadence
        # is itself a bot tell. The limiter sleeps if the window is full.
        await get_apply_rate_limiter().acquire("apply")

        async with _cache_lock:
            cache = _load_cache()

        body = {
            "strJobsarr": [job_id],
            **APPLY_TRAILER,
        }

        if answers:
            apply_answers = _build_apply_answers(job_id, answers, cache)
            if apply_answers:
                body["applyData"] = {job_id: {"answers": apply_answers}}

        data = await api_client.post(
            APPLY_WORKFLOW_API,
            body,
        )

        jobs = data.get("jobs", [])
        if not jobs:
            msg = data.get("message", "")
            if "already" in msg.lower():
                await record_application(job_id, title=title, company=company,
                                         status="already_applied",
                                         extra={**(tracking_extra or {})})
                return {
                    "status": "already_applied",
                    "job_id": job_id,
                    "daily_applied": data.get("quotaDetails", {}).get("dailyApplied"),
                }
            await record_application(job_id, title=title, company=company,
                                     status="error",
                                     extra={"message": msg or "Unexpected response", **(tracking_extra or {})})
            return {"status": "error", "job_id": job_id, "message": msg or "Unexpected response", "error_code": "API_ERROR"}

        job_result = jobs[0]
        status_code = job_result.get("status")
        questionnaire = job_result.get("questionnaire", [])

        if status_code == 200:
            if questionnaire and answers:
                async with _cache_lock:
                    cache = _load_cache()
                    _cache_answers(questionnaire, answers, cache)
                    _save_cache(cache)
            return await _finalize_applied(
                job_id, title=title, company=company, tracking_extra=tracking_extra,
                base_result={"daily_applied": data.get("quotaDetails", {}).get("dailyApplied")},
            )

        if questionnaire:
            pending = []
            auto_answers = {}

            # Single lock for the whole cache read-modify-write cycle
            async with _cache_lock:
                cache = _load_cache()

                for q in questionnaire:
                    qid = str(q.get("questionId", ""))
                    q_name = q.get("questionName", "")
                    q_type = q.get("questionType", "")
                    options = q.get("answerOption", {})
                    cache_k = _cache_key(q_name, options)

                    cached = cache.get(cache_k)
                    if cached:
                        auto_answers[qid] = cached["answer"]
                        continue

                    user_answer = _find_user_answer(qid, q_name, answers)
                    if user_answer is not None:
                        formatted = _format_answer(user_answer, q_type, options)
                        auto_answers[qid] = formatted
                        cache[cache_k] = {
                            "questionType": q_type,
                            "questionName": q_name,
                            "answer": formatted,
                        }
                        continue

                    pending.append({
                        "question_id": qid,
                        "question": q_name,
                        "type": q_type,
                        "options": options,
                    })

                if not pending and auto_answers:
                    _save_cache(cache)
                elif pending:
                    _save_cache(cache)

            # Network calls OUTSIDE the lock
            if not pending and auto_answers:
                body["applyData"] = {job_id: {"answers": auto_answers}}
                data2 = await api_client.post(
                    APPLY_WORKFLOW_API,
                    body,
                )
                jobs2 = data2.get("jobs", [])
                if jobs2 and jobs2[0].get("status") == 200:
                    return await _finalize_applied(
                        job_id, title=title, company=company, tracking_extra=tracking_extra,
                        base_result={
                            "questions_answered": len(auto_answers),
                            "daily_applied": data2.get("quotaDetails", {}).get("dailyApplied"),
                        },
                    )

            if pending:
                # Persist the QUESTIONS, not len(pending). The list below is
                # returned to the caller either way; storing only its length
                # meant that if the caller did not answer immediately, what the
                # recruiter actually asked was gone -- unrecoverable without a
                # live re-fetch. Text, type and options only: never an answer.
                await record_application(job_id, title=title, company=company,
                                         status="needs_input",
                                         extra={"pending_questions": project_pending_questions(pending),
                                                **(tracking_extra or {})})
                return {
                    "status": "needs_input",
                    "job_id": job_id,
                    "questions": pending,
                    "auto_answered": len(auto_answers),
                    "daily_applied": data.get("quotaDetails", {}).get("dailyApplied"),
                }

        await record_application(job_id, title=title, company=company,
                                 status="error",
                                 extra={"message": f"Apply returned status {status_code}", **(tracking_extra or {})})
        return {"status": "error", "job_id": job_id, "message": f"Apply returned status {status_code}", "error_code": "API_ERROR"}

    except ValueError as e:
        await record_application(job_id, title=title, company=company,
                                 status="error",
                                 extra={"message": str(e), **(tracking_extra or {})})
        return {"status": "error", "job_id": job_id, "message": str(e), "error_code": "API_ERROR"}
    except NaukriAPIError as e:
        # If the apply call came back as a block (CAPTCHA/bot-check/login-wall),
        # TRIP the kill-switch: keep hammering the apply endpoint while blocked is
        # exactly what risks an account lockout. trip_and_halt flips the agent to
        # dry_run + notifies the operator. Then surface a halted status.
        block_kind = getattr(e, "block_kind", None)
        if block_kind:
            try:
                await kill_switch.trip_and_halt(
                    f"apply to {job_id} hit a block: {e.message}", block_kind=block_kind,
                )
            except Exception as trip_err:
                logger.error("Failed to trip kill-switch after apply block: %s", trip_err)
            await record_application(job_id, title=title, company=company,
                                     status="error",
                                     extra={"message": str(e), "halted": True,
                                            "block_kind": block_kind, **(tracking_extra or {})})
            return {"status": "halted", "job_id": job_id, "message": str(e),
                    "error_code": "KILL_SWITCH_TRIPPED", "block_kind": block_kind}
        await record_application(job_id, title=title, company=company,
                                 status="error",
                                 extra={"message": f"{type(e).__name__}: {e!r}", **(tracking_extra or {})})
        return {"status": "error", "job_id": job_id, "message": f"{type(e).__name__}: {e!r}",
                "error_code": "API_ERROR", "http_status": e.status}
    except Exception as e:
        await record_application(job_id, title=title, company=company,
                                 status="error",
                                 extra={"message": f"{type(e).__name__}: {e!r}", **(tracking_extra or {})})
        return {"status": "error", "job_id": job_id, "message": f"{type(e).__name__}: {e!r}", "error_code": "API_ERROR"}


async def naukri_apply(
    job_id: str,
    answers: Optional[dict] = None,
) -> dict:
    """Apply to a single Naukri job. Two-phase flow:

    Phase 1 (no answers): Sends apply request. If the job has screening questions,
    returns them with options. If no questions, applies instantly.

    Phase 2 (with answers): Provide answers dict keyed by question text or question ID.
    Submits answers and completes application.

    Requires: a job_id from naukri_search_jobs or naukri_get_job results.

    Args:
        job_id: Naukri job ID (numeric string)
        answers: Dict of answers for screening questions. Keys can be:
                 - Question ID (from phase 1 response): {"12345": "5"}
                 - Question text substring: {"current ctc": "16", "notice period": "30"}
                 For radio/list/checkbox: value is the option text to select.
                 For text box: value is the string to enter.

    Returns:
        - {status: "applied"} — success
        - {status: "needs_input", questions: [...]} — answer the questions and call again
        - {status: "already_applied"} — already applied
        - {status: "error", message} — failure
    """
    job_id = _extract_job_id(job_id)

    # Check for duplicate application from local tracking
    from naukri_server.database import get_application
    existing_app = await get_application(str(job_id))
    if existing_app:
        # Allow retry if previous attempt needs_input and answers are provided
        if ApplicationStatus.from_string(existing_app.get("status", "")) == ApplicationStatus.NEEDS_INPUT and answers:
            pass  # Fall through to _apply_single for retry
        else:
            return {
                "status": "already_applied",
                "message": "You have already applied to this job (from local tracking).",
                "job_id": job_id,
            }

    return await _apply_single(job_id, answers, tracking_extra={"source": "single"})


_apply_job = naukri_apply


async def naukri_batch_apply(
    keywords: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    salary_min: Optional[int] = None,
    salary_max: Optional[int] = None,
    sort_by: Optional[str] = None,
    freshness: Optional[int] = None,
    work_mode: Optional[str] = None,
    job_type: Optional[str] = None,
    company_type: Optional[str] = None,
    limit: int = 5,
    answers: Optional[dict] = None,
    delay_ms: int = BATCH_APPLY_DEFAULT_DELAY_MS,
    max_concurrent: int = 3,
    ctx: Context | None = None,
) -> dict:
    """Search and apply to multiple jobs, SERIALLY, at a human-like pace.

    Searches for jobs, filters out already-applied ones, then applies to the
    remaining jobs ONE AT A TIME with a randomized human-like think-time between
    applications. Screening questions are auto-answered from the cache or the
    provided answers dict.

    DECISION: stealth > throughput. Concurrent applies were removed — submitting
    several applications in parallel is an automation tell no human produces; the
    ``max_concurrent`` argument is now DEPRECATED and ignored (kept only so
    existing callers don't break). Cadence is paced by a log-normal think-time,
    not a fixed delay, so ``delay_ms`` is likewise deprecated (used only as a
    floor if larger than the sampled think-time).

    For applying to a single specific job, use naukri_apply instead.

    Args:
        keywords: Job title or skills (e.g., "python developer", "react node.js")
        location: City name (e.g., "Bangalore", "Mumbai", "Remote")
        experience: Years of experience filter
        salary_min: Minimum salary filter in lakhs (forwarded to search)
        salary_max: Maximum salary filter in lakhs (forwarded to search)
        sort_by: Sort order — "relevance" or "date" (forwarded to search)
        freshness: Job age in days — 1, 3, 7, 15, 30 (forwarded to search)
        work_mode: Work mode filter — "wfh", "hybrid", "wfo" (forwarded to search)
        job_type: Filter by job type - "fulltime", "parttime", "contract", "internship", "temporary"
        company_type: Filter by company type - "startup", "mnc", "indian_mnc", "corporate"
        limit: Max jobs to apply to (default 5, max 20)
        answers: Pre-filled answers for common screening questions.
                 Keys are question text substrings: {"current ctc": "16", "notice period": "15 days"}
        delay_ms: DEPRECATED. Applies are paced by a randomized human-like
                  think-time; this is only used as a minimum floor (ms) if it
                  exceeds the sampled think-time. Default 500.
        max_concurrent: DEPRECATED and IGNORED — applies are always serial now
                        (concurrent apply is an automation tell). Kept for
                        backward compatibility only.

    Returns:
        - {status: "success"/"partial_success"/"error", searched, filtered, applied, already_applied, needs_input, errors, pending_questions: [...], results: [...]}
    """
    from naukri_server.tools.search import naukri_search_jobs

    if answers is None:
        answers = {}
    limit = validate_limit(limit, max_allowed=20)
    if delay_ms < 0:
        return {"status": "error", "message": "delay_ms must be >= 0", "error_code": "VALIDATION_ERROR"}
    # max_concurrent is deprecated/ignored (serial applies). A bad value is no
    # longer an error — we simply don't use it for concurrency.

    # Step 1: Search
    search_result = await naukri_search_jobs(
        keywords, location, experience,
        salary_min=salary_min, salary_max=salary_max,
        sort_by=sort_by, freshness=freshness, work_mode=work_mode,
        job_type=job_type, company_type=company_type,
        limit=limit,
    )
    if search_result.get("status") != "success":
        return {"status": "error", "message": "Search failed", "search_result": search_result, "error_code": "API_ERROR"}

    all_jobs = search_result.get("jobs", [])
    if not all_jobs:
        return {"status": "error", "message": "No jobs found for this search", "error_code": "NOT_FOUND"}

    # Step 2: Filter out already-applied (from search results + local tracking)
    to_apply = [j for j in all_jobs if not j.get("is_applied")]

    # Also filter out jobs we've already applied to locally
    from naukri_server.database import get_applied_job_ids
    applied_ids = await get_applied_job_ids()
    before_local_filter = len(to_apply)
    to_apply = [j for j in to_apply if str(j.get("job_id")) not in applied_ids]
    skipped_duplicates = before_local_filter - len(to_apply)

    if not to_apply:
        return {
            "status": "success",
            "message": f"All {len(all_jobs)} jobs already applied to",
            "searched": len(all_jobs),
            "filtered": 0,
            "skipped_duplicates": skipped_duplicates,
        }

    # Step 3: SERIAL, human-paced apply (Phase 1 + auto-answer from cache).
    # DECISION stealth>throughput: one application at a time (no Semaphore /
    # gather — concurrent apply is an automation tell), with a randomized
    # log-normal think-time between applications (NOT a fixed delay). The
    # per-call RateLimiter inside _apply_single is the hard throughput cap; the
    # think-time shapes a human-plausible cadence on top of it.
    total = len(to_apply)
    if ctx:
        try:
            await ctx.info(f"Applying to {total} jobs serially (human-paced)")
        except Exception:
            pass

    # delay_ms (deprecated) acts only as a floor on the inter-application gap.
    delay_floor_seconds = max(0.0, delay_ms / 1000)

    results: list = []
    halted_early = False
    for i, j in enumerate(to_apply):
        # Human-like think-time BEFORE each application after the first. A real
        # person pauses to read/decide/fill; we sample that pause per-job.
        if i > 0:
            think = await human_think_time(
                APPLY_THINK_TIME_MEDIAN_SECONDS, APPLY_THINK_TIME_SIGMA,
                APPLY_THINK_TIME_MIN_SECONDS, APPLY_THINK_TIME_MAX_SECONDS,
            )
            # Honour the deprecated delay_ms floor + a small per-action jitter so
            # even the floor isn't a constant.
            if delay_floor_seconds > think:
                await jittered_delay(delay_floor_seconds - think,
                                     APPLY_JITTER_MIN_SECONDS, APPLY_JITTER_MAX_SECONDS)

        try:
            res = await asyncio.wait_for(
                _apply_single(j["job_id"], answers, j.get("title"), j.get("company"),
                              tracking_extra={"salary": j.get("salary"), "location": j.get("location"),
                                              "url": j.get("url"), "source": "batch"}),
                timeout=BATCH_APPLY_PER_JOB_TIMEOUT,
            )
        except asyncio.TimeoutError:
            res = {"status": "error", "job_id": j["job_id"],
                   "message": f"Timed out after {BATCH_APPLY_PER_JOB_TIMEOUT}s", "error_code": "TIMEOUT"}
        results.append(res)

        # FAIL-CLOSED short-circuit: if an apply was halted by the kill-switch
        # (block detected) or hit the hard daily quota, STOP the batch now rather
        # than firing more traffic. Remaining jobs are simply not attempted.
        if isinstance(res, dict) and res.get("status") in ("halted", "quota_reached"):
            halted_early = True
            logger.warning("Batch apply stopped early after job %d/%d: %s",
                           i + 1, total, res.get("status"))
            break

    # Step 4: Collect results
    applied = 0
    needs_input = 0
    errors = 0
    already = 0
    halted = 0
    quota_reached = 0
    pending_questions = {}  # deduplicated: question_text -> {info, job_ids}
    per_job = []

    for job_info, result in zip(to_apply, results):
        if isinstance(result, Exception):
            errors += 1
            per_job.append({
                "job_id": job_info["job_id"],
                "title": job_info.get("title"),
                "company": job_info.get("company"),
                "status": "error",
                "message": f"{type(result).__name__}: {result}",
                "error_code": "API_ERROR",
            })
            continue

        status = result.get("status")
        entry = {
            "job_id": job_info["job_id"],
            "title": job_info.get("title"),
            "company": job_info.get("company"),
            "salary": job_info.get("salary"),
            "location": job_info.get("location"),
            "status": status,
        }

        if status == "applied":
            applied += 1
        elif status == "already_applied":
            already += 1
        elif status == "needs_input":
            needs_input += 1
            entry["questions"] = result.get("questions", [])
            # Deduplicate questions across jobs
            for q in result.get("questions", []):
                q_key = q["question"]
                if q_key not in pending_questions:
                    pending_questions[q_key] = {
                        "question": q["question"],
                        "type": q["type"],
                        "options": q.get("options", {}),
                        "affects_jobs": [],
                    }
                pending_questions[q_key]["affects_jobs"].append(job_info["job_id"])
        elif status == "halted":
            halted += 1
            entry["message"] = result.get("message")
            entry["block_kind"] = result.get("block_kind")
        elif status == "quota_reached":
            quota_reached += 1
            entry["message"] = result.get("message")
        else:
            errors += 1
            entry["message"] = result.get("message")

        per_job.append(entry)

    # Determine final status based on outcomes. A kill-switch halt or daily-quota
    # stop is its own terminal status so the caller knows traffic was cut.
    if halted > 0:
        final_status = "halted"
    elif quota_reached > 0 and applied == 0:
        final_status = "quota_reached"
    elif applied > 0 and errors == 0 and needs_input == 0:
        final_status = "success"
    elif applied > 0 and (errors > 0 or needs_input > 0):
        final_status = "partial_success"
    elif applied == 0 and errors > 0:
        final_status = "error"
    elif applied == 0 and needs_input > 0 and errors == 0:
        final_status = "partial_success"
    else:
        final_status = "success"

    if ctx:
        try:
            await ctx.info(
                f"Batch apply complete: {applied} applied, {already} already_applied, "
                f"{needs_input} needs_input, {errors} errors"
                + (f", HALTED ({halted})" if halted else "")
                + (f", quota_reached ({quota_reached})" if quota_reached else "")
            )
        except Exception:
            pass

    result_payload = {
        "status": final_status,
        "searched": len(all_jobs),
        "filtered": len(to_apply),
        "attempted": len(results),
        "skipped_duplicates": skipped_duplicates,
        "applied": applied,
        "already_applied": already,
        "needs_input": needs_input,
        "errors": errors,
        "pending_questions": list(pending_questions.values()),
        "results": per_job,
    }
    if halted or quota_reached or halted_early:
        result_payload["halted_early"] = halted_early
        result_payload["halted"] = halted
        result_payload["quota_reached"] = quota_reached
        result_payload["not_attempted"] = max(0, len(to_apply) - len(results))
    return result_payload


_batch_apply = naukri_batch_apply


def _build_apply_answers(job_id: str, answers: dict, cache: dict) -> dict:
    """Build answers dict from user-provided answers (for direct submission)."""
    result = {}
    for key, value in answers.items():
        # If key looks like a question ID (numeric), use directly
        if str(key).isdigit():
            result[str(key)] = value if isinstance(value, list) else str(value)
    return result


def _cache_answers(questionnaire: list, answers: dict, cache: dict):
    """Cache successfully answered questions."""
    for q in questionnaire:
        qid = str(q.get("questionId", ""))
        q_name = q.get("questionName", "")
        q_type = q.get("questionType", "")
        options = q.get("answerOption", {})

        answer = _find_user_answer(qid, q_name, answers)
        if answer:
            cache_k = _cache_key(q_name, options)
            if cache_k not in cache:
                cache[cache_k] = {
                    "questionType": q_type,
                    "questionName": q_name,
                    "answer": _format_answer(answer, q_type, options),
                }


# ============================================================================
# Recovery: re-fetch the questions that were never persisted
# ============================================================================


def _questionnaire_to_pending(questionnaire) -> list:
    """Convert a raw Naukri questionnaire into the internal pending shape."""
    pending = []
    for q in questionnaire or []:
        if not isinstance(q, dict):
            continue
        pending.append({
            "question": q.get("questionName") or "",
            "type": q.get("questionType") or "",
            "options": q.get("answerOption") or {},
        })
    return pending


async def _refetch_questions_only(job_id: str) -> dict:
    """Re-fetch ONE job screening questionnaire WITHOUT answering any of it.

    THE GUARANTEE IS STRUCTURAL, NOT AN INTENTION. This function takes no
    `answers` parameter, never reads the answer cache, and builds its request
    body from exactly two things: `strJobsarr` and APPLY_TRAILER. `applyData`
    is the ONLY field the apply endpoint accepts answers through, and no code
    path in this function constructs it -- there is no branch to audit, because
    the key is never assigned anywhere in the body.

    It deliberately does NOT route through `_apply_single`. That function CAN
    submit even when called with no answers at all: every question already in
    the local answer cache goes into `auto_answers`, and its
    `if not pending and auto_answers:` branch then POSTs those answers and
    completes the application. With answers already cached for the common
    questions, a "re-fetch with no answers" through that function would have
    applied to real jobs. That is the whole reason this separate probe exists.

    Returns one of:
        {"status": "recovered", "questions": [...]} -- questionnaire came back
        {"status": "applied_unexpectedly", ...}     -- see the caution below
        {"status": "unrecoverable", "message": ...} -- nothing usable came back

    CAUTION, and it is real. Naukri exposes no read-only questionnaire
    endpoint; the only way to see the questions is to open the apply workflow,
    which is the same call that produced these rows. If a posting NO LONGER
    asks screening questions, that call returns status 200, and 200 means
    APPLIED. Supplying no answers cannot prevent that. This function does not
    paper over it -- it reports `applied_unexpectedly` and the caller records
    the row as applied, because that is what is true.
    """
    # The same fail-closed rails as the apply path, CALLED here (not altered):
    # this hits the apply endpoint, so a tripped kill-switch must stop it too.
    kill_switch.guard()
    await get_apply_rate_limiter().acquire("apply")

    body = {
        "strJobsarr": [job_id],
        **APPLY_TRAILER,
    }
    data = await api_client.post(APPLY_WORKFLOW_API, body)

    jobs = data.get("jobs", [])
    if not jobs:
        return {"status": "unrecoverable", "job_id": job_id,
                "message": data.get("message") or "No job entry in apply-workflow response"}

    job_result = jobs[0]
    questionnaire = job_result.get("questionnaire", [])
    if questionnaire:
        return {"status": "recovered", "job_id": job_id,
                "questions": _questionnaire_to_pending(questionnaire)}

    if job_result.get("status") == 200:
        return {"status": "applied_unexpectedly", "job_id": job_id,
                "message": "Job no longer asks screening questions, so opening the "
                           "apply workflow completed the application. No answers "
                           "were supplied or submitted."}

    return {"status": "unrecoverable", "job_id": job_id,
            "message": "Apply workflow returned status %s with no questionnaire"
                       % (job_result.get("status"),)}


async def _recover_pending_questions(job_ids=None, confirm: bool = False,
                                     limit: int = 25) -> dict:
    """Re-fetch questions for needs_input rows that persisted only a count.

    Gated: `confirm=False` (the default) makes NO network call and NO write. It
    names exactly which job ids would be re-fetched and returns.

    A row is recovered once: rows whose question text is already present are
    skipped, so a second run has nothing left to do.
    """
    from naukri_server.database import list_applications as db_list
    from naukri_server.tools.tracking import read_pending_questions, _row_pending_raw

    limit = validate_limit(limit)
    rows, _total = await db_list(status="needs_input", limit=limit, offset=0)

    wanted = {str(j) for j in job_ids} if job_ids else None

    targets, already = [], []
    for row in rows:
        jid = str(row.get("job_id") or "")
        if wanted is not None and jid not in wanted:
            continue
        read = read_pending_questions(_row_pending_raw(row))
        if read["text_recoverable"]:
            already.append(jid)
        else:
            targets.append({
                "job_id": jid,
                "company": row.get("company"),
                "title": row.get("title"),
                "question_count": read["count"],
            })

    not_eligible = (sorted(wanted - {t["job_id"] for t in targets} - set(already))
                    if wanted is not None else [])

    if not confirm:
        # PERFORMS NOTHING. No api_client call is reachable from this branch.
        return {
            "status": "preview",
            "confirm_required": True,
            "would_refetch_count": len(targets),
            "would_refetch": targets,
            "skipped_already_recovered": already,
            "not_needs_input": not_eligible,
            "performed": "nothing",
            "note": ("Preview only: no network call was made and nothing was written. "
                     "Re-run with confirm=True to re-fetch these questions. The "
                     "re-fetch supplies no answers and submits none."),
        }

    recovered, failed, applied_unexpectedly = [], [], []
    for target in targets:
        jid = target["job_id"]
        try:
            result = await _refetch_questions_only(jid)
        except kill_switch.KillSwitchTrippedError as e:
            return {
                "status": "halted",
                "message": str(e),
                "error_code": "KILL_SWITCH_TRIPPED",
                "block_kind": e.block_kind,
                "recovered_count": len(recovered),
                "recovered": recovered,
                "applied_unexpectedly": applied_unexpectedly,
                "failed": failed,
                "skipped_already_recovered": already,
                "not_needs_input": not_eligible,
            }
        except Exception as e:  # noqa: BLE001 - one bad job must not stop the sweep
            logger.warning("Question re-fetch failed for %s: %s", jid, e)
            failed.append({"job_id": jid, "message": "%s: %s" % (type(e).__name__, e)})
            continue

        if result["status"] == "recovered":
            questions = project_pending_questions(result["questions"])
            await record_application(jid, status="needs_input",
                                     extra={"pending_questions": questions})
            recovered.append({"job_id": jid, "question_count": len(questions)})
        elif result["status"] == "applied_unexpectedly":
            # Record what actually happened. Leaving it as needs_input would
            # make recovery re-open the same job forever.
            await record_application(jid, status="applied",
                                     extra={"recovery_note": result["message"]})
            applied_unexpectedly.append(result)
        else:
            failed.append({"job_id": jid, "message": result.get("message")})

    return {
        "status": "success" if not failed else "partial_success",
        "confirmed": True,
        "recovered_count": len(recovered),
        "recovered": recovered,
        "applied_unexpectedly": applied_unexpectedly,
        "failed": failed,
        "skipped_already_recovered": already,
        "not_needs_input": not_eligible,
    }
