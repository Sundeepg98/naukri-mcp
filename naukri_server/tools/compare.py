"""Job comparison — side-by-side analysis of multiple jobs."""

import asyncio

from naukri_server import mcp
from naukri_server.config import logger


@mcp.tool()
async def naukri_compare_jobs(job_ids: list[str]) -> dict:
    """Compare multiple jobs side-by-side.

    Fetches all job details in parallel and returns a structured comparison
    covering salary, skills, company, location, and work mode.

    Args:
        job_ids: List of 2-5 job IDs to compare

    Returns:
        - {status: "success", count, jobs: [{job_id, title, company, salary,
           experience, location, work_mode, skills, company_rating, group_id}],
           common_skills, all_skills}
        - {status: "error", message}
    """
    if len(job_ids) < 2:
        return {"status": "error", "message": "Need at least 2 job IDs to compare."}
    if len(job_ids) > 5:
        return {"status": "error", "message": "Maximum 5 jobs for comparison."}

    from naukri_server.tools.jobs import naukri_get_job

    # Parallel fetch all jobs
    results = await asyncio.gather(
        *[naukri_get_job(job_id_or_url=jid) for jid in job_ids],
        return_exceptions=True,
    )

    jobs = []
    errors = []
    all_skill_sets = []

    for jid, r in zip(job_ids, results):
        if isinstance(r, Exception):
            errors.append(f"Job {jid}: {type(r).__name__}: {r}")
            continue
        if r.get("status") == "error":
            errors.append(f"Job {jid}: {r.get('message')}")
            continue

        skills = r.get("skills", [])
        all_skill_sets.append(set(s.lower() for s in skills))

        jobs.append({
            "job_id": jid,
            "title": r.get("title"),
            "company": r.get("company"),
            "company_rating": r.get("company_rating"),
            "salary": r.get("salary"),
            "experience": r.get("experience"),
            "location": r.get("location"),
            "work_mode": r.get("work_mode"),
            "skills": skills,
            "group_id": r.get("group_id"),
            "vacancies": r.get("vacancies"),
            "is_applied": r.get("is_applied"),
        })

    if not jobs:
        return {"status": "error", "message": f"No jobs could be fetched. Errors: {errors}"}

    # Compute skill overlap
    common_skills = sorted(set.intersection(*all_skill_sets)) if all_skill_sets else []
    all_skills = sorted(set.union(*all_skill_sets)) if all_skill_sets else []

    result = {
        "status": "success",
        "count": len(jobs),
        "jobs": jobs,
        "common_skills": common_skills,
        "all_skills": all_skills,
    }
    if errors:
        result["errors"] = errors

    return result
