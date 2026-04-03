"""Auto hunt — one-call job hunting with fit scoring."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger
from naukri_server.scoring import compute_fit_score, parse_skills
from naukri_server.validation import validate_limit


@mcp.tool()
async def naukri_auto_hunt(
    keywords: str,
    location: Optional[str] = None,
    min_fit_score: int = 60,
    limit: int = 20,
    freshness: Optional[int] = 7,
    work_mode: Optional[str] = None,
    experience: Optional[int] = None,
    salary_min: Optional[int] = None,
    salary_max: Optional[int] = None,
    timeout_seconds: int = 120,
) -> dict:
    """Automated job hunting — search, score against your profile, return ranked matches.

    Searches for jobs matching your criteria, then scores each against your profile
    for skill overlap, experience match, and location/salary/work-mode bonuses.
    Skills are alias-normalized (JS = JavaScript, k8s = Kubernetes).
    Returns only jobs above the minimum fit score, ranked from best to worst match.

    Does NOT auto-apply — use naukri_smart_apply or naukri_apply for that.

    Args:
        keywords: Job search keywords (e.g., "Python developer", "data engineer")
        location: City or region filter (optional)
        min_fit_score: Minimum fit score 0-100 to include in results (default 60)
        limit: Max jobs to search and score (default 20)
        freshness: Only jobs posted in last N days (default 7)
        work_mode: "wfh" (remote), "hybrid", "wfo" (office) — optional
        experience: Filter by years of experience (optional)
        salary_min: Minimum salary in lakhs (optional)
        salary_max: Maximum salary in lakhs (optional)
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        - {status: "success", jobs_found, jobs_matched, ranked_jobs: [{job_id, title,
           company, salary, location, work_mode, fit_score, matched_skills,
           missing_skills, recommendation}]}
        - {status: "error", message}
    """
    limit = validate_limit(limit)
    if not 0 <= min_fit_score <= 100:
        return {"status": "error", "message": "min_fit_score must be between 0 and 100", "error_code": "VALIDATION_ERROR"}

    async def _do_work() -> dict:
        from naukri_server.tools.search import naukri_search_jobs
        from naukri_server.tools.profile import get_cached_profile

        # Parallel: search jobs + fetch profile
        search_result, profile_result = await asyncio.gather(
            naukri_search_jobs(
                keywords=keywords, location=location, limit=limit,
                freshness=freshness, work_mode=work_mode,
                experience=experience, salary_min=salary_min, salary_max=salary_max,
            ),
            get_cached_profile(),
            return_exceptions=True,
        )

        if isinstance(search_result, Exception) or search_result.get("status") == "error":
            msg = str(search_result) if isinstance(search_result, Exception) else search_result.get("message")
            return {"status": "error", "message": f"Search failed: {msg}", "error_code": "API_ERROR"}

        if isinstance(profile_result, Exception) or profile_result.get("status") == "error":
            msg = str(profile_result) if isinstance(profile_result, Exception) else profile_result.get("message")
            return {"status": "error", "message": f"Profile fetch failed: {msg}", "error_code": "API_ERROR"}

        jobs = search_result.get("jobs", [])
        if not jobs:
            return {"status": "success", "jobs_found": 0, "jobs_matched": 0, "ranked_jobs": []}

        # Filter out already-applied jobs (API + local tracking)
        pre_filter_count = len(jobs)
        jobs = [j for j in jobs if not j.get("is_applied")]

        # Cross-reference with local applications tracking
        try:
            from naukri_server.tools.tracking import _load_json, APPLICATIONS_FILE, _applications_lock
            async with _applications_lock:
                local_apps = _load_json(APPLICATIONS_FILE)
                local_applied_ids = {str(a.get("job_id")) for a in local_apps}
            jobs = [j for j in jobs if str(j.get("job_id")) not in local_applied_ids]
        except Exception as e:
            logger.warning("Job cross-ref failed — duplicate detection may be incomplete: %s", e)

        if not jobs:
            return {"status": "success", "jobs_found": pre_filter_count,
                    "jobs_matched": 0, "ranked_jobs": [],
                    "note": "All matching jobs already applied to."}

        profile_skills = parse_skills(profile_result.get("key_skills", []))
        profile_exp = profile_result.get("total_experience")
        profile_location = profile_result.get("current_location")
        profile_expected_ctc = profile_result.get("expected_ctc")

        # Score each job
        ranked = []
        for job in jobs:
            job_skills = parse_skills(job.get("tags") or job.get("skills") or [])
            fit = compute_fit_score(
                job_skills, profile_skills,
                job.get("experience", ""),
                profile_exp,
                job_location=job.get("location"),
                profile_location=profile_location,
                job_work_mode=job.get("work_mode"),
                job_salary=job.get("salary"),
                profile_expected_ctc=profile_expected_ctc,
                experience_min=job.get("experience_min"),
                experience_max=job.get("experience_max"),
                is_agent_eligible=job.get("is_agent_eligible"),
            )

            if fit["overall_score"] >= min_fit_score:
                ranked.append({
                    "job_id": job.get("job_id"),
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "salary": job.get("salary"),
                    "location": job.get("location"),
                    "work_mode": job.get("work_mode"),
                    "experience": job.get("experience"),
                    "is_applied": job.get("is_applied"),
                    "fit_score": fit["overall_score"],
                    "matched_skills": fit["skill_match"]["matched"],
                    "missing_skills": fit["skill_match"]["missing"],
                    "recommendation": fit["recommendation"],
                    "bonuses": fit.get("bonuses"),
                })

        # Sort by fit score descending
        ranked.sort(key=lambda x: x["fit_score"], reverse=True)

        return {
            "status": "success",
            "jobs_found": len(jobs),
            "jobs_matched": len(ranked),
            "ranked_jobs": ranked,
        }

    try:
        return await asyncio.wait_for(_do_work(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return {"status": "partial_success", "message": f"Timed out after {timeout_seconds}s", "error_code": "TIMEOUT"}
