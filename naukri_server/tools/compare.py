"""Job comparison — side-by-side analysis of multiple jobs with fit scoring."""

import asyncio

from naukri_server.config import logger
from naukri_server.models import Job
from naukri_server.scoring import parse_skills, score_job


async def _compare_jobs(
    job_ids: list[str],
    timeout_seconds: int = 120,
) -> dict:
    """Compare multiple jobs side-by-side with fit scores.

    Fetches all job details and your profile in parallel, then returns a
    structured comparison covering salary, skills, company, location, work mode,
    and per-job fit assessment against your profile.

    Args:
        job_ids: List of 2-5 job IDs to compare
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        - {status: "success", count, jobs: [{job_id, title, company, salary,
           experience, location, work_mode, skills, fit_score, matched_skills,
           missing_skills, recommendation, ...}],
           common_skills, all_skills, best_match_job_id, average_fit_score,
           saga_steps, step_timings}
        - {status: "error", message}
    """
    logger.info("Comparing %d jobs: %s", len(job_ids), job_ids)
    if len(job_ids) < 2:
        return {"status": "error", "message": "Need at least 2 job IDs to compare.", "error_code": "VALIDATION_ERROR"}
    if len(job_ids) > 5:
        return {"status": "error", "message": "Maximum 5 jobs for comparison.", "error_code": "VALIDATION_ERROR"}

    async def _do_work() -> dict:
        from naukri_server.tools.jobs import naukri_get_job
        from naukri_server.tools.profile import get_cached_profile
        from naukri_server.sagas import SagaExecutor

        saga = SagaExecutor("compare_jobs")

        # Shared state across saga steps
        job_results = []
        profile_result = None
        profile_ok = False
        profile_skills = set()
        profile_exp = None
        profile_location = None
        profile_expected_ctc = None
        local_applied_ids = set()
        jobs = []
        errors = []
        all_skill_sets = []
        fit_scores = []

        async def step_fetch_jobs():
            nonlocal job_results, profile_result, profile_ok
            nonlocal profile_skills, profile_exp, profile_location, profile_expected_ctc
            nonlocal local_applied_ids

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

            # Load local tracking for cross-check
            try:
                from naukri_server.database import get_applied_job_ids
                local_applied_ids = await get_applied_job_ids()
            except Exception as e:
                logger.debug("Scoring failed for job cross-ref: %s", e)

            return {"fetched_jobs": len(job_results), "profile_ok": profile_ok}

        async def step_score_all():
            nonlocal jobs, errors, all_skill_sets, fit_scores

            for jid, r in zip(job_ids, job_results):
                if isinstance(r, Exception):
                    errors.append(f"Job {jid}: {type(r).__name__}: {r}")
                    continue
                if r.get("status") == "error":
                    errors.append(f"Job {jid}: {r.get('message')}")
                    continue

                job = Job.from_api_dict(r)
                skills = job.tags or r.get("skills") or []
                job_skills = parse_skills(skills)
                all_skill_sets.append(job_skills)

                job_entry = {
                    "job_id": job.job_id,
                    "title": job.title,
                    "company": job.company,
                    "company_rating": job.company_rating,
                    "salary": r.get("salary"),
                    "experience": r.get("experience"),
                    "location": job.location,
                    "work_mode": job.work_mode,
                    "skills": skills if isinstance(skills, list) else list(job_skills),
                    "group_id": r.get("group_id"),
                    "vacancies": r.get("vacancies"),
                    "is_applied": job.is_applied or jid in local_applied_ids,
                    "external_apply": r.get("external_apply"),
                    "external_apply_url": r.get("external_apply_url"),
                    "posted_date": r.get("posted_date"),
                    "apply_count": r.get("apply_count"),
                    "candidates_count": r.get("candidates_count"),
                }

                if profile_ok:
                    fit = score_job(
                        job_skills, profile_skills,
                        r.get("experience", ""), profile_exp,
                        job_location=job.location,
                        profile_location=profile_location,
                        job_work_mode=job.work_mode,
                        job_salary=r.get("salary"),
                        profile_expected_ctc=profile_expected_ctc,
                        experience_min=job.experience_min,
                        experience_max=job.experience_max,
                    )
                    job_entry["fit_score"] = fit.overall_score
                    job_entry["matched_skills"] = sorted(fit.skill_match.matched)
                    job_entry["missing_skills"] = sorted(fit.skill_match.missing)
                    job_entry["recommendation"] = fit.recommendation
                    job_entry["bonuses"] = {
                        "location": fit.bonuses.location,
                        "work_mode": fit.bonuses.work_mode,
                        "salary": fit.bonuses.salary,
                        "agent_eligible": fit.bonuses.agent_eligible,
                        "total": fit.bonuses.total,
                    } if fit._has_enrichment else None
                    fit_scores.append(fit.overall_score)

                jobs.append(job_entry)

            return {"scored": len(jobs), "errors": len(errors)}

        async def step_aggregate():
            if not jobs:
                raise ValueError(f"No jobs could be fetched. Errors: {errors}")

            # Compute skill overlap (using normalized sets)
            common_skills = sorted(set.intersection(*all_skill_sets)) if all_skill_sets else []
            all_skills = sorted(set.union(*all_skill_sets)) if all_skill_sets else []

            result = {
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

        saga.add_step("fetch_jobs", step_fetch_jobs)
        saga.add_step("score_all", step_score_all)
        saga.add_step("aggregate", step_aggregate)

        saga_result = await saga.run()

        if saga_result["status"] == "error":
            saga_errors = saga_result.get("errors", [])
            error_detail = saga_errors[0] if saga_errors else "unknown"
            return {
                "status": "error",
                "message": f"Compare saga failed at step '{saga_result.get('failed_step')}': {error_detail}",
                "error_code": "API_ERROR",
                "saga_steps": saga_result.get("completed_steps", []),
                "saga_errors": saga_errors,
            }

        # Build final result from aggregate step output
        aggregate_data = saga_result.get("results", {}).get("aggregate", {})
        return {
            "status": "success",
            **aggregate_data,
            "saga_steps": saga_result.get("completed_steps", []),
            "step_timings": saga_result.get("step_timings", {}),
        }

    try:
        return await asyncio.wait_for(_do_work(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return {"status": "partial_success", "message": f"Timed out after {timeout_seconds}s", "error_code": "TIMEOUT"}


naukri_compare_jobs = _compare_jobs
