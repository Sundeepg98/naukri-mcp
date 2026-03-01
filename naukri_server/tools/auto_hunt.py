"""Auto hunt — one-call job hunting with fit scoring."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger
from naukri_server.scoring import compute_fit_score


@mcp.tool()
async def naukri_auto_hunt(
    keywords: str,
    location: Optional[str] = None,
    min_fit_score: int = 60,
    limit: int = 20,
    freshness: Optional[int] = 7,
    work_mode: Optional[str] = None,
) -> dict:
    """Automated job hunting — search, score against your profile, return ranked matches.

    Searches for jobs matching your criteria, then scores each against your profile
    for skill overlap and experience match. Returns only jobs above the minimum
    fit score, ranked from best to worst match.

    Does NOT auto-apply — use naukri_smart_apply or naukri_apply for that.

    Args:
        keywords: Job search keywords (e.g., "Python developer", "data engineer")
        location: City or region filter (optional)
        min_fit_score: Minimum fit score 0-100 to include in results (default 60)
        limit: Max jobs to search and score (default 20)
        freshness: Only jobs posted in last N days (default 7)
        work_mode: "wfh" (remote), "hybrid", "wfo" (office) — optional

    Returns:
        - {status: "success", jobs_found, jobs_matched, ranked_jobs: [{job_id, title,
           company, salary, location, work_mode, fit_score, matched_skills,
           missing_skills, recommendation}]}
        - {status: "error", message}
    """
    from naukri_server.tools.search import naukri_search_jobs
    from naukri_server.tools.profile import naukri_get_profile

    # Parallel: search jobs + fetch profile
    search_result, profile_result = await asyncio.gather(
        naukri_search_jobs(
            keywords=keywords, location=location, limit=limit,
            freshness=freshness, work_mode=work_mode,
        ),
        naukri_get_profile(),
        return_exceptions=True,
    )

    if isinstance(search_result, Exception) or search_result.get("status") == "error":
        msg = str(search_result) if isinstance(search_result, Exception) else search_result.get("message")
        return {"status": "error", "message": f"Search failed: {msg}"}

    if isinstance(profile_result, Exception) or profile_result.get("status") == "error":
        msg = str(profile_result) if isinstance(profile_result, Exception) else profile_result.get("message")
        return {"status": "error", "message": f"Profile fetch failed: {msg}"}

    jobs = search_result.get("jobs", [])
    if not jobs:
        return {"status": "success", "jobs_found": 0, "jobs_matched": 0, "ranked_jobs": []}

    profile_skills = set(s.lower() for s in profile_result.get("key_skills", []))
    profile_exp = profile_result.get("total_experience")

    # Score each job
    ranked = []
    for job in jobs:
        job_skills = set(s.lower() for s in (job.get("skills") or []))
        fit = compute_fit_score(
            job_skills, profile_skills,
            job.get("experience", ""),
            profile_exp,
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
            })

    # Sort by fit score descending
    ranked.sort(key=lambda x: x["fit_score"], reverse=True)

    return {
        "status": "success",
        "jobs_found": len(jobs),
        "jobs_matched": len(ranked),
        "ranked_jobs": ranked,
    }
