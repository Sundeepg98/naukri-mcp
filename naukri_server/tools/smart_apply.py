"""Smart apply — job fit assessment before applying.

DDD: external job/profile API reads in _score_job route through safe_get.
Internal scored-result aggregation reads (e.g. fit_details from this same
module) stay on plain .get() — those are our owned shape.
"""

import asyncio
from typing import Optional

from mcp.server.fastmcp import Context

from naukri_server import mcp
from naukri_server.config import (
    APPLY_MIN_FIT_SCORE, BULK_FETCH_CONCURRENCY, DAILY_APPLY_QUOTA,
    DISPLAY_MIN_FIT_SCORE, MAX_BULK_JOBS,
)
from naukri_server.domain import safe_get
from naukri_server.domain.fit_score import FitScore
from naukri_server.error_handler import handle_tool_action
from naukri_server.models import Job
from naukri_server.scoring import parse_skills, _score_location, _score_work_mode, _score_salary



def _score_job(job_result: dict, profile_result: dict, is_agent_eligible: bool = False) -> dict:
    """Score a single job against a profile. Returns the fit assessment dict.

    job_result and profile_result come from naukri_get_job / get_cached_profile —
    external API responses, so all reads route through safe_get. The 'tags' and
    'skills' fall-through uses safe_get's multi-key form to capture both v3
    (skills) and v4 (tags) shapes.
    """
    job_skills_raw = safe_get(
        job_result, "tags", "skills",
        field_name="job.tags_or_skills", warn=False, default=[],
    )
    job_skills = parse_skills(job_skills_raw)
    profile_skills = parse_skills(safe_get(profile_result, "key_skills", field_name="key_skills", warn=False, default=[]))
    fit = FitScore.compute(
        job_skills, profile_skills,
        safe_get(job_result, "experience", field_name="job.experience", warn=False, default=""),
        safe_get(profile_result, "total_experience", field_name="profile.total_experience", warn=False),
        job_location=safe_get(job_result, "location", field_name="job.location", warn=False),
        profile_location=safe_get(profile_result, "current_location", field_name="profile.current_location", warn=False),
        job_work_mode=safe_get(job_result, "work_mode", field_name="job.work_mode", warn=False),
        job_salary=safe_get(job_result, "salary", field_name="job.salary", warn=False),
        profile_expected_ctc=safe_get(profile_result, "expected_ctc", field_name="profile.expected_ctc", warn=False),
        is_agent_eligible=is_agent_eligible,
        score_location_fn=_score_location,
        score_work_mode_fn=_score_work_mode,
        score_salary_fn=_score_salary,
    )
    return fit.to_dict()


async def _bulk_saved_scoring(
    min_fit_score: int = 0,
    timeout_seconds: int = 120,
    ctx: Context | None = None,
) -> dict:
    """Score all saved jobs against profile, return ranked list.

    Fetches saved jobs from local tracking, fetches full job details for each
    (to get skills/experience data), then scores each against the profile.
    Results are sorted by fit_score descending.
    """
    async def _do_work():
        from naukri_server.tools.jobs import naukri_get_job
        from naukri_server.tools.profile import get_cached_profile
        from naukri_server.tools.tracking import _list_saved_jobs

        # Parallel: saved jobs list + profile
        saved_result, profile_result = await asyncio.gather(
            _list_saved_jobs(limit=MAX_BULK_JOBS, page=1),
            get_cached_profile(),
            return_exceptions=True,
        )

        if isinstance(saved_result, Exception):
            return {"status": "error", "message": f"Failed to fetch saved jobs: {saved_result}", "error_code": "API_ERROR"}
        if isinstance(saved_result, dict) and saved_result.get("status") == "error":
            return {"status": "error", "message": f"Failed to fetch saved jobs: {saved_result.get('message')}", "error_code": "API_ERROR"}

        if isinstance(profile_result, Exception):
            return {"status": "error", "message": f"Failed to fetch profile: {profile_result}", "error_code": "API_ERROR"}
        if isinstance(profile_result, dict) and profile_result.get("status") == "error":
            return {"status": "error", "message": f"Failed to fetch profile: {profile_result.get('message')}", "error_code": "API_ERROR"}

        saved_jobs = saved_result.get("saved_jobs", [])
        if not saved_jobs:
            return {"status": "success", "total_saved": 0, "scored_count": 0, "min_fit_score": min_fit_score, "scored_jobs": [], "message": "No saved jobs found"}

        if ctx:
            try:
                await ctx.info(f"Fetching details for {len(saved_jobs)} saved jobs")
            except Exception:
                pass

        # Fetch full details for each saved job (need skills/experience for scoring)
        sem = asyncio.Semaphore(BULK_FETCH_CONCURRENCY)
        errors = []

        async def _fetch_detail(job):
            job_id = job.get("job_id")
            if not job_id:
                return None
            async with sem:
                try:
                    return await asyncio.wait_for(
                        naukri_get_job(job_id_or_url=job_id), timeout=30
                    )
                except asyncio.TimeoutError:
                    errors.append(f"job {job_id}: detail fetch timed out (30s)")
                    return None
                except Exception as e:
                    errors.append(f"job {job_id}: {e}")
                    return None

        detail_results = await asyncio.gather(
            *[_fetch_detail(j) for j in saved_jobs],
            return_exceptions=True,
        )

        if ctx:
            try:
                await ctx.info(f"Scoring {len(detail_results)} jobs against profile")
            except Exception:
                pass

        # Score each job that has valid details
        scored = []
        for saved_job, detail in zip(saved_jobs, detail_results):
            if isinstance(detail, Exception):
                errors.append(f"job {saved_job.get('job_id')}: {detail}")
                continue
            if detail is None or (isinstance(detail, dict) and detail.get("status") == "error"):
                continue

            job = Job.from_api_dict(detail)
            fit = _score_job(detail, profile_result, is_agent_eligible=job.is_agent_eligible)
            fit_score = fit.get("overall_score", 0)

            if fit_score >= min_fit_score:
                scored.append({
                    "job_id": saved_job.get("job_id"),
                    "title": job.title or saved_job.get("title"),
                    "company": job.company or saved_job.get("company"),
                    "salary": job.salary or saved_job.get("salary"),
                    "location": job.location or saved_job.get("location"),
                    "experience": job.experience,
                    "work_mode": job.work_mode,
                    "fit_score": fit_score,
                    "fit_details": fit,
                })

        scored.sort(key=lambda x: x["fit_score"], reverse=True)

        result = {
            "status": "success",
            "total_saved": len(saved_jobs),
            "scored_count": len(scored),
            "min_fit_score": min_fit_score,
            "scored_jobs": scored,
        }
        if errors:
            result["errors"] = errors

        return result

    return await handle_tool_action(
        lambda: asyncio.wait_for(_do_work(), timeout=timeout_seconds),
        "smart_apply.bulk_saved",
    )


async def _apply_top_fits(
    min_fit_score: int = APPLY_MIN_FIT_SCORE,
    limit: int = 10,
    set_reminder_days: Optional[int] = None,
    answers: Optional[dict] = None,
    timeout_seconds: int = 120,
    ctx: Context | None = None,
) -> dict:
    """Score saved jobs and apply to top fits above min_fit_score."""
    from naukri_server.tools.apply import _apply_single
    from naukri_server.validation import validate_limit
    from naukri_server.sagas import SagaExecutor

    limit = validate_limit(limit, max_allowed=20)

    saga = SagaExecutor("apply_top_fits")
    scored_jobs = []
    to_apply = []
    results = []
    applied_count = 0
    errors = []

    async def step_score():
        nonlocal scored_jobs
        scored_result = await _bulk_saved_scoring(
            min_fit_score=min_fit_score,
            timeout_seconds=timeout_seconds,
            ctx=ctx,
        )
        if scored_result.get("status") == "error":
            raise RuntimeError(scored_result.get("message", "Scoring failed"))

        scored_jobs = scored_result.get("scored_jobs", [])
        return {
            "total_saved": scored_result.get("total_saved", 0),
            "scored_count": len(scored_jobs),
        }

    async def step_apply_batch():
        nonlocal to_apply, results, applied_count, errors

        if not scored_jobs:
            return {"applied": 0, "attempted": 0}

        # Prioritize agent-eligible jobs at equal scores
        scored_jobs.sort(key=lambda x: (
            not x.get("fit_details", {}).get("bonuses", {}).get("agent_eligible", 0),
            -x["fit_score"]
        ))

        to_apply = scored_jobs[:limit]

        if ctx:
            try:
                await ctx.info(
                    f"Scored {len(scored_jobs)} jobs, applying to top {len(to_apply)}"
                )
            except Exception:
                pass

        for i, job in enumerate(to_apply, start=1):
            if ctx:
                try:
                    await ctx.report_progress(
                        i, len(to_apply),
                        message=f"Applying to {job.get('title')} at {job.get('company')}",
                    )
                except Exception:
                    pass
            jid = job.get("job_id")
            if not jid:
                continue
            try:
                apply_result = await _apply_single(
                    job_id=jid,
                    answers=answers,
                    title=job.get("title"),
                    company=job.get("company"),
                    tracking_extra={"source": "apply_top_fits", "fit_score": job.get("fit_score")},
                )
                status = apply_result.get("status")

                # Auto-retry needs_input if general answers were provided
                if status == "needs_input" and answers:
                    apply_result = await _apply_single(
                        job_id=jid,
                        answers=answers,
                        title=job.get("title"),
                        company=job.get("company"),
                        tracking_extra={"source": "apply_top_fits_retry", "fit_score": job.get("fit_score")},
                    )
                    status = apply_result.get("status")

                entry = {
                    "job_id": jid,
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "fit_score": job.get("fit_score"),
                    "apply_status": status,
                }

                if status == "applied":
                    applied_count += 1
                    # Auto-reminder
                    if set_reminder_days:
                        from naukri_server.tools.reminders import _set_reminder
                        try:
                            await _set_reminder(
                                job_id=jid,
                                days=set_reminder_days,
                                note=f"Follow up on {job.get('company', 'unknown')} application",
                            )
                            entry["reminder_set"] = True
                        except Exception:
                            entry["reminder_set"] = False

                results.append(entry)

                # FAIL-CLOSED: stop the batch if a block tripped the kill-switch
                # or we hit the hard daily quota — don't keep hammering.
                if status in ("halted", "quota_reached"):
                    entry["message"] = apply_result.get("message")
                    break
            except Exception as e:
                errors.append(f"Job {jid}: {type(e).__name__}: {e}")
                results.append({
                    "job_id": jid,
                    "title": job.get("title"),
                    "fit_score": job.get("fit_score"),
                    "apply_status": "error",
                    "error": str(e),
                })

        return {"applied": applied_count, "attempted": len(to_apply)}

    saga.add_step("score_saved", step_score)
    saga.add_step("apply_batch", step_apply_batch)

    saga_result = await saga.run()

    if saga_result["status"] == "error":
        saga_errors = saga_result.get("errors", [])
        error_detail = saga_errors[0] if saga_errors else "unknown"
        return {
            "status": "error",
            "message": f"Apply top fits saga failed at step '{saga_result.get('failed_step')}': {error_detail}",
            "error_code": "API_ERROR",
            "saga_steps": saga_result.get("completed_steps", []),
            "saga_errors": saga_errors,
        }

    # Build result — scored_jobs may be empty if none met threshold
    if not scored_jobs:
        return {
            "status": "success",
            "message": f"No saved jobs with fit score >= {min_fit_score}",
            "applied": 0,
            "skipped": saga_result.get("results", {}).get("score_saved", {}).get("total_saved", 0),
            "results": [],
            "saga_steps": saga_result.get("completed_steps", []),
            "step_timings": saga_result.get("step_timings", {}),
        }

    result = {
        "status": "success" if applied_count > 0 else "error",
        "applied": applied_count,
        "attempted": len(to_apply),
        "total_scored": len(scored_jobs),
        "min_fit_score": min_fit_score,
        "results": results,
        "saga_steps": saga_result.get("completed_steps", []),
        "step_timings": saga_result.get("step_timings", {}),
    }
    if errors:
        result["errors"] = errors
    if set_reminder_days and applied_count:
        result["reminder_days"] = set_reminder_days

    return result


# =====================================================================
# Single-purpose tools
# =====================================================================


@mcp.tool()
async def naukri_assess_fit(
    job_id: str,
    apply_if_fit: bool = False,
    min_fit_score: int = DISPLAY_MIN_FIT_SCORE,
    answers: Optional[dict] = None,
    set_reminder_days: Optional[int] = None,
) -> dict:
    """Assess fit for a single job — skill overlap, experience, location, salary.

    Fetches job details and profile in parallel, scores fit (0-100).
    Optionally auto-applies when score >= min_fit_score.

    Args:
        job_id: Naukri job ID
        apply_if_fit: Auto-apply if fit score meets threshold
        min_fit_score: Minimum score to trigger auto-apply (0-100)
        answers: Screening question answers for auto-apply
        set_reminder_days: Days until follow-up reminder after applying

    Returns:
        {status, job_summary, fit_assessment: {overall_score, skill_match,
         experience_match, bonuses, recommendation}, applied, naukri_match}
    """
    if not 0 <= min_fit_score <= 100:
        return {"status": "error", "message": "min_fit_score must be between 0 and 100", "error_code": "VALIDATION_ERROR"}

    async def _single_assess():
        from naukri_server.tools.jobs import naukri_get_job
        from naukri_server.tools.jobs import _fetch_match_score
        from naukri_server.tools.profile import get_cached_profile
        from naukri_server.tools.apply import _apply_single

        job_result, profile_result = await asyncio.gather(
            naukri_get_job(job_id_or_url=job_id),
            get_cached_profile(),
            return_exceptions=True,
        )

        if isinstance(job_result, Exception) or job_result.get("status") == "error":
            msg = str(job_result) if isinstance(job_result, Exception) else job_result.get("message")
            return {"status": "error", "message": f"Failed to fetch job: {msg}", "error_code": "API_ERROR"}

        if isinstance(profile_result, Exception) or profile_result.get("status") == "error":
            msg = str(profile_result) if isinstance(profile_result, Exception) else profile_result.get("message")
            return {"status": "error", "message": f"Failed to fetch profile: {msg}", "error_code": "API_ERROR"}

        naukri_match = None
        try:
            naukri_match = await _fetch_match_score(job_id)
        except Exception:
            pass

        fit = _score_job(job_result, profile_result, is_agent_eligible=job_result.get("is_agent_eligible", False))

        result = {
            "status": "success",
            "job_summary": {
                "title": job_result.get("title"),
                "company": job_result.get("company"),
                "salary": job_result.get("salary", ""),
                "experience": job_result.get("experience", ""),
                "location": job_result.get("location"),
                "work_mode": job_result.get("work_mode"),
            },
            "fit_assessment": fit,
            "applied": False,
            "naukri_match": naukri_match,
        }

        if apply_if_fit and fit["overall_score"] >= min_fit_score:
            apply_result = await _apply_single(
                job_id=job_id, answers=answers,
                title=job_result.get("title"), company=job_result.get("company"),
                tracking_extra={"source": "assess_fit", "fit_score": fit["overall_score"]},
            )
            result["applied"] = apply_result.get("status") == "applied"
            result["apply_result"] = apply_result
            daily = apply_result.get("daily_applied")
            if daily is not None and daily >= DAILY_APPLY_QUOTA - 5:
                result["quota_warning"] = f"Daily quota: {daily}/{DAILY_APPLY_QUOTA} used. {DAILY_APPLY_QUOTA - daily} remaining."
            if result["applied"] and set_reminder_days:
                from naukri_server.tools.reminders import _set_reminder
                try:
                    await _set_reminder(
                        job_id=job_id,
                        days=set_reminder_days,
                        note=f"Follow up on {job_result.get('company', 'unknown')} application",
                    )
                    result["reminder_set"] = True
                except Exception:
                    result["reminder_set"] = False

        return result

    return await handle_tool_action(_single_assess, "assess_fit")


@mcp.tool()
async def naukri_score_saved_jobs(
    min_fit_score: int = DISPLAY_MIN_FIT_SCORE,
    timeout_seconds: int = 120,
    ctx: Context | None = None,
) -> dict:
    """Score all saved/bookmarked jobs against your profile.

    Returns a ranked list sorted by fit_score descending, filtered
    by min_fit_score.

    Args:
        min_fit_score: Only return jobs scoring at or above this (0-100)
        timeout_seconds: Timeout for the bulk operation

    Returns:
        {status, total_saved, scored_count, min_fit_score,
         scored_jobs: [{job_id, title, company, salary, location,
                        fit_score, fit_details}, ...]}
    """
    if not 0 <= min_fit_score <= 100:
        return {"status": "error", "message": "min_fit_score must be between 0 and 100", "error_code": "VALIDATION_ERROR"}

    return await _bulk_saved_scoring(
        min_fit_score=min_fit_score,
        timeout_seconds=timeout_seconds,
        ctx=ctx,
    )


@mcp.tool()
async def naukri_apply_top_fits(
    min_fit_score: int = APPLY_MIN_FIT_SCORE,
    limit: int = 10,
    set_reminder_days: Optional[int] = None,
    answers: Optional[dict] = None,
    timeout_seconds: int = 120,
    ctx: Context | None = None,
) -> dict:
    """Score saved jobs and auto-apply to top matches above min_fit_score.

    Prioritizes agent-eligible jobs at equal scores. Optionally sets
    follow-up reminders on successful applications.

    Args:
        min_fit_score: Minimum fit score to apply (0-100, default 70)
        limit: Max jobs to apply to (default 10, max 20)
        set_reminder_days: Days until follow-up reminder after applying
        answers: Screening question answers for auto-apply
        timeout_seconds: Timeout for the bulk operation

    Returns:
        {status, applied, attempted, total_scored, min_fit_score,
         results: [{job_id, title, company, fit_score, apply_status,
                    reminder_set}, ...]}
    """
    if not 0 <= min_fit_score <= 100:
        return {"status": "error", "message": "min_fit_score must be between 0 and 100", "error_code": "VALIDATION_ERROR"}

    return await _apply_top_fits(
        min_fit_score=min_fit_score,
        limit=limit,
        set_reminder_days=set_reminder_days,
        answers=answers,
        timeout_seconds=timeout_seconds,
        ctx=ctx,
    )
