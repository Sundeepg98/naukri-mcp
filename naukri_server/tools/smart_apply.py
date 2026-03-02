"""Smart apply — job fit assessment before applying."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.config import DAILY_APPLY_QUOTA, BULK_FETCH_CONCURRENCY
from naukri_server.scoring import compute_fit_score, parse_skills



def _score_job(job_result: dict, profile_result: dict) -> dict:
    """Score a single job against a profile. Returns the fit assessment dict."""
    job_skills = parse_skills(job_result.get("tags") or job_result.get("skills") or [])
    profile_skills = parse_skills(profile_result.get("key_skills", []))
    return compute_fit_score(
        job_skills, profile_skills,
        job_result.get("experience", ""),
        profile_result.get("total_experience"),
        job_location=job_result.get("location"),
        profile_location=profile_result.get("current_location"),
        job_work_mode=job_result.get("work_mode"),
        job_salary=job_result.get("salary"),
        profile_expected_ctc=profile_result.get("expected_ctc"),
    )


async def _bulk_saved_scoring(min_fit_score: int = 0, timeout_seconds: int = 120) -> dict:
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
            _list_saved_jobs(limit=50, page=1),
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

        # Fetch full details for each saved job (need skills/experience for scoring)
        sem = asyncio.Semaphore(BULK_FETCH_CONCURRENCY)
        errors = []

        async def _fetch_detail(job):
            job_id = job.get("job_id")
            if not job_id:
                return None
            async with sem:
                try:
                    return await naukri_get_job(job_id_or_url=job_id)
                except Exception as e:
                    errors.append(f"job {job_id}: {e}")
                    return None

        detail_results = await asyncio.gather(
            *[_fetch_detail(j) for j in saved_jobs],
            return_exceptions=True,
        )

        # Score each job that has valid details
        scored = []
        for saved_job, detail in zip(saved_jobs, detail_results):
            if isinstance(detail, Exception):
                errors.append(f"job {saved_job.get('job_id')}: {detail}")
                continue
            if detail is None or (isinstance(detail, dict) and detail.get("status") == "error"):
                continue

            fit = _score_job(detail, profile_result)
            fit_score = fit.get("overall_score", 0)

            if fit_score >= min_fit_score:
                scored.append({
                    "job_id": saved_job.get("job_id"),
                    "title": detail.get("title") or saved_job.get("title"),
                    "company": detail.get("company") or saved_job.get("company"),
                    "salary": detail.get("salary") or saved_job.get("salary"),
                    "location": detail.get("location") or saved_job.get("location"),
                    "experience": detail.get("experience"),
                    "work_mode": detail.get("work_mode"),
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

    try:
        return await asyncio.wait_for(_do_work(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return {"status": "error", "message": f"Bulk scoring timed out after {timeout_seconds}s", "error_code": "API_ERROR"}


async def _apply_top_fits(
    min_fit_score: int = 60,
    limit: int = 10,
    set_reminder_days: Optional[int] = None,
    answers: Optional[dict] = None,
    timeout_seconds: int = 120,
) -> dict:
    """Score saved jobs and apply to top fits above min_fit_score."""
    from naukri_server.tools.apply import _apply_single
    from naukri_server.validation import validate_limit

    limit = validate_limit(limit, max_allowed=20)

    # Step 1: Get scored jobs
    scored_result = await _bulk_saved_scoring(min_fit_score=min_fit_score, timeout_seconds=timeout_seconds)
    if scored_result.get("status") == "error":
        return scored_result

    scored_jobs = scored_result.get("scored_jobs", [])
    if not scored_jobs:
        return {
            "status": "success",
            "message": f"No saved jobs with fit score >= {min_fit_score}",
            "applied": 0,
            "skipped": scored_result.get("total_saved", 0),
            "results": [],
        }

    # Step 2: Apply to top N
    to_apply = scored_jobs[:limit]
    results = []
    applied_count = 0
    errors = []

    for job in to_apply:
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
        except Exception as e:
            errors.append(f"Job {jid}: {type(e).__name__}: {e}")
            results.append({
                "job_id": jid,
                "title": job.get("title"),
                "fit_score": job.get("fit_score"),
                "apply_status": "error",
                "error": str(e),
            })

    result = {
        "status": "success" if applied_count > 0 else "error",
        "applied": applied_count,
        "attempted": len(to_apply),
        "total_scored": len(scored_jobs),
        "min_fit_score": min_fit_score,
        "results": results,
    }
    if errors:
        result["errors"] = errors
    if set_reminder_days and applied_count:
        result["reminder_days"] = set_reminder_days

    return result


@mcp.tool()
async def naukri_smart_apply(
    job_id: Optional[str] = None,
    action: Optional[str] = None,
    apply_if_fit: bool = False,
    min_fit_score: int = 60,
    answers: Optional[dict] = None,
    timeout_seconds: int = 120,
    set_reminder_days: Optional[int] = None,
    limit: int = 10,
) -> dict:
    """Assess job fit before applying — compares your profile against the job.

    Fetches job details and your profile in parallel, then computes:
    - Skill overlap with alias normalization (JS = JavaScript, k8s = Kubernetes)
    - Experience match
    - Location, work mode, and salary bonuses
    - Overall recommendation

    Actions:
      - None (default): Single-job assessment. Requires job_id.
      - "bulk_saved": Score all saved/bookmarked jobs against your profile.
                      Returns a ranked list sorted by fit_score descending.
                      Uses min_fit_score to filter results (default 60).
                      Does NOT require job_id.
      - "apply_top_fits": Score saved jobs and auto-apply to top fits.
                         Uses min_fit_score to filter, limit to cap applications.
                         Optionally sets follow-up reminders via set_reminder_days.

    Args:
        job_id: Naukri job ID (required for single-job assessment, ignored for bulk_saved)
        action: "bulk_saved" to score all saved jobs, or None for single-job assessment
        apply_if_fit: If True, automatically apply when fit score >= min_fit_score (single-job only)
        min_fit_score: Minimum fit score threshold (default 60, range 0-100)
        answers: Optional screening question answers (for auto-apply, single-job only)
        timeout_seconds: Timeout for bulk operations (default 120)
        set_reminder_days: Days until follow-up reminder (for apply_top_fits and single-job auto-apply)
        limit: Max jobs to apply to in apply_top_fits (default 10, max 20)

    Returns:
        Single-job:
        - {status: "success", fit_assessment: {overall_score, skill_match,
           experience_match, bonuses, recommendation}, job_summary, applied}
        - {status: "error", message}

        bulk_saved:
        - {status: "success", total_saved, scored_count, min_fit_score,
           scored_jobs: [{job_id, title, company, salary, location, fit_score, fit_details}, ...]}
        - {status: "error", message}

        apply_top_fits:
        - {status: "success", applied, attempted, total_scored, min_fit_score,
           results: [{job_id, title, company, fit_score, apply_status, reminder_set}]}
        - {status: "error", message}
    """
    if not 0 <= min_fit_score <= 100:
        return {"status": "error", "message": "min_fit_score must be between 0 and 100", "error_code": "VALIDATION_ERROR"}

    # ── Bulk saved scoring ───────────────────────────────────────────
    if action == "bulk_saved":
        return await _bulk_saved_scoring(min_fit_score=min_fit_score, timeout_seconds=timeout_seconds)

    # ── Apply top fits ────────────────────────────────────────────────
    if action == "apply_top_fits":
        return await _apply_top_fits(
            min_fit_score=min_fit_score,
            limit=limit,
            set_reminder_days=set_reminder_days,
            answers=answers,
            timeout_seconds=timeout_seconds,
        )

    # ── Unknown action ───────────────────────────────────────────────
    if action is not None:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: bulk_saved, apply_top_fits (or omit for single-job assessment)", "error_code": "VALIDATION_ERROR"}

    # ── Single-job assessment (default) ──────────────────────────────
    if not job_id:
        return {"status": "error", "message": "job_id is required for single-job assessment", "error_code": "VALIDATION_ERROR"}

    from naukri_server.tools.jobs import naukri_get_job
    from naukri_server.tools.jobs import _fetch_match_score
    from naukri_server.tools.profile import get_cached_profile
    from naukri_server.tools.apply import _apply_single

    # Parallel fetch job + profile
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

    # Fetch Naukri's own match score (non-blocking — don't fail if unavailable)
    naukri_match = None
    try:
        naukri_match = await _fetch_match_score(job_id)
    except Exception:
        pass

    # Compute fit — use tags-or-skills fallback, pass enrichment data
    fit = _score_job(job_result, profile_result)

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

    # Auto-apply if requested and fit
    if apply_if_fit and fit["overall_score"] >= min_fit_score:
        apply_result = await _apply_single(
            job_id=job_id, answers=answers,
            title=job_result.get("title"), company=job_result.get("company"),
            tracking_extra={"source": "smart_apply", "fit_score": fit["overall_score"]},
        )
        result["applied"] = apply_result.get("status") == "applied"
        result["apply_result"] = apply_result
        # Quota warning
        daily = apply_result.get("daily_applied")
        if daily is not None and daily >= DAILY_APPLY_QUOTA - 5:
            result["quota_warning"] = f"Daily quota: {daily}/{DAILY_APPLY_QUOTA} used. {DAILY_APPLY_QUOTA - daily} remaining."

    return result
