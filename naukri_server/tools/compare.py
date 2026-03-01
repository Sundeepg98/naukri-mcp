"""Job comparison — side-by-side analysis of multiple jobs with fit scoring."""

import asyncio

from naukri_server import mcp
from naukri_server.config import logger
from naukri_server.scoring import compute_fit_score, parse_skills


@mcp.tool()
async def naukri_compare_jobs(job_ids: list[str]) -> dict:
    """Compare multiple jobs side-by-side with fit scores.

    Fetches all job details and your profile in parallel, then returns a
    structured comparison covering salary, skills, company, location, work mode,
    and per-job fit assessment against your profile.

    Args:
        job_ids: List of 2-5 job IDs to compare

    Returns:
        - {status: "success", count, jobs: [{job_id, title, company, salary,
           experience, location, work_mode, skills, fit_score, matched_skills,
           missing_skills, recommendation, ...}],
           common_skills, all_skills, best_match_job_id, average_fit_score}
        - {status: "error", message}
    """
    if len(job_ids) < 2:
        return {"status": "error", "message": "Need at least 2 job IDs to compare."}
    if len(job_ids) > 5:
        return {"status": "error", "message": "Maximum 5 jobs for comparison."}

    from naukri_server.tools.jobs import naukri_get_job
    from naukri_server.tools.profile import get_cached_profile

    # Parallel fetch all jobs + profile
    all_results = await asyncio.gather(
        *[naukri_get_job(job_id_or_url=jid) for jid in job_ids],
        get_cached_profile(),
        return_exceptions=True,
    )

    # Profile is the last result
    profile_result = all_results[-1]
    job_results = all_results[:-1]

    # Extract profile data (graceful if profile fetch fails)
    profile_ok = (
        not isinstance(profile_result, Exception)
        and isinstance(profile_result, dict)
        and profile_result.get("status") != "error"
    )
    if profile_ok:
        profile_skills = parse_skills(profile_result.get("key_skills", []))
        profile_exp = profile_result.get("total_experience")
        profile_location = profile_result.get("current_location")
        profile_expected_ctc = profile_result.get("expected_ctc")
    else:
        profile_skills = set()
        profile_exp = None
        profile_location = None
        profile_expected_ctc = None

    # Load local tracking for cross-check
    local_applied_ids = set()
    try:
        from naukri_server.tools.tracking import _load_json, APPLICATIONS_FILE, _applications_lock
        async with _applications_lock:
            local_apps = _load_json(APPLICATIONS_FILE)
            local_applied_ids = {str(a.get("job_id")) for a in local_apps}
    except Exception:
        pass  # Non-critical — just use API is_applied

    jobs = []
    errors = []
    all_skill_sets = []
    fit_scores = []

    for jid, r in zip(job_ids, job_results):
        if isinstance(r, Exception):
            errors.append(f"Job {jid}: {type(r).__name__}: {r}")
            continue
        if r.get("status") == "error":
            errors.append(f"Job {jid}: {r.get('message')}")
            continue

        skills = r.get("tags") or r.get("skills") or []
        job_skills = parse_skills(skills)
        all_skill_sets.append(job_skills)

        job_entry = {
            "job_id": jid,
            "title": r.get("title"),
            "company": r.get("company"),
            "company_rating": r.get("company_rating"),
            "salary": r.get("salary"),
            "experience": r.get("experience"),
            "location": r.get("location"),
            "work_mode": r.get("work_mode"),
            "skills": skills if isinstance(skills, list) else list(job_skills),
            "group_id": r.get("group_id"),
            "vacancies": r.get("vacancies"),
            "is_applied": r.get("is_applied") or jid in local_applied_ids,
            "external_apply": r.get("external_apply"),
            "external_apply_url": r.get("external_apply_url"),
        }

        if profile_ok:
            fit = compute_fit_score(
                job_skills, profile_skills,
                r.get("experience", ""), profile_exp,
                job_location=r.get("location"),
                profile_location=profile_location,
                job_work_mode=r.get("work_mode"),
                job_salary=r.get("salary"),
                profile_expected_ctc=profile_expected_ctc,
            )
            job_entry["fit_score"] = fit["overall_score"]
            job_entry["matched_skills"] = fit["skill_match"]["matched"]
            job_entry["missing_skills"] = fit["skill_match"]["missing"]
            job_entry["recommendation"] = fit["recommendation"]
            job_entry["bonuses"] = fit.get("bonuses")
            fit_scores.append(fit["overall_score"])

        jobs.append(job_entry)

    if not jobs:
        return {"status": "error", "message": f"No jobs could be fetched. Errors: {errors}"}

    # Compute skill overlap (using normalized sets)
    common_skills = sorted(set.intersection(*all_skill_sets)) if all_skill_sets else []
    all_skills = sorted(set.union(*all_skill_sets)) if all_skill_sets else []

    result = {
        "status": "success",
        "count": len(jobs),
        "jobs": jobs,
        "common_skills": common_skills,
        "all_skills": all_skills,
    }

    # Summary stats
    if fit_scores:
        result["average_fit_score"] = round(sum(fit_scores) / len(fit_scores))
        best_idx = fit_scores.index(max(fit_scores))
        result["best_match_job_id"] = jobs[best_idx]["job_id"]

    if errors:
        result["errors"] = errors

    return result
