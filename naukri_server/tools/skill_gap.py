"""Skill gap analysis — find systematic gaps across multiple job listings."""

import asyncio
from collections import Counter
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger


@mcp.tool()
async def naukri_skill_gap_analysis(
    keywords: Optional[str] = None,
    use_recommendations: bool = True,
    sample_size: int = 20,
) -> dict:
    """Analyze skill gaps across multiple jobs to find your most common missing skills.

    Fetches N jobs (via recommendations or search) and your profile in parallel,
    then computes which skills you're missing most often. Helps prioritize upskilling.

    Args:
        keywords: Search keywords (required if use_recommendations is False)
        use_recommendations: If True, use personalized recommendations; if False, use search (default True)
        sample_size: Number of jobs to analyze (default 20, max 50)

    Returns:
        - {status: "success", jobs_analyzed, skill_gaps: [{skill, frequency, percentage,
           sample_jobs}], strong_skills: [{skill, frequency, percentage}]}
        - {status: "error", message}
    """
    if not use_recommendations and not keywords:
        return {"status": "error", "message": "keywords is required when use_recommendations is False."}

    sample_size = min(sample_size, 50)

    from naukri_server.tools.search import naukri_search_jobs, naukri_get_recommendations
    from naukri_server.tools.profile import naukri_get_profile

    # Parallel: fetch jobs + profile
    if use_recommendations:
        jobs_coro = naukri_get_recommendations(limit=sample_size)
    else:
        jobs_coro = naukri_search_jobs(keywords=keywords, limit=sample_size)

    jobs_result, profile_result = await asyncio.gather(
        jobs_coro,
        naukri_get_profile(),
        return_exceptions=True,
    )

    if isinstance(jobs_result, Exception) or jobs_result.get("status") == "error":
        msg = str(jobs_result) if isinstance(jobs_result, Exception) else jobs_result.get("message")
        return {"status": "error", "message": f"Failed to fetch jobs: {msg}"}

    if isinstance(profile_result, Exception) or profile_result.get("status") == "error":
        msg = str(profile_result) if isinstance(profile_result, Exception) else profile_result.get("message")
        return {"status": "error", "message": f"Failed to fetch profile: {msg}"}

    jobs = jobs_result.get("jobs", [])
    if not jobs:
        return {"status": "error", "message": "No jobs found to analyze."}

    profile_skills = set(s.lower() for s in profile_result.get("key_skills", []))

    # Analyze each job
    missing_counter = Counter()  # skill -> count of jobs missing it
    matched_counter = Counter()  # skill -> count of jobs matching it
    missing_jobs = {}  # skill -> list of job titles where it's missing

    for job in jobs:
        job_skills = set(s.lower() for s in (job.get("tags") or job.get("skills") or []))
        if not job_skills:
            continue

        matched = job_skills & profile_skills
        missing = job_skills - profile_skills
        job_label = f"{job.get('title', '?')} @ {job.get('company', '?')}"

        for skill in missing:
            missing_counter[skill] += 1
            if skill not in missing_jobs:
                missing_jobs[skill] = []
            if len(missing_jobs[skill]) < 5:  # Cap sample jobs at 5
                missing_jobs[skill].append(job_label)

        for skill in matched:
            matched_counter[skill] += 1

    jobs_analyzed = len(jobs)

    # Build skill gaps (sorted by frequency)
    skill_gaps = []
    for skill, freq in missing_counter.most_common(30):
        skill_gaps.append({
            "skill": skill,
            "frequency": freq,
            "percentage": round(freq / jobs_analyzed * 100),
            "sample_jobs": missing_jobs.get(skill, []),
        })

    # Build strong skills (sorted by frequency)
    strong_skills = []
    for skill, freq in matched_counter.most_common(20):
        strong_skills.append({
            "skill": skill,
            "frequency": freq,
            "percentage": round(freq / jobs_analyzed * 100),
        })

    return {
        "status": "success",
        "jobs_analyzed": jobs_analyzed,
        "skill_gaps": skill_gaps,
        "strong_skills": strong_skills,
    }
