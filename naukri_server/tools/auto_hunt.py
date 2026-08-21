"""Auto hunt — one-call job hunting with fit scoring."""

import asyncio
from typing import Optional

from mcp.server.fastmcp import Context

from naukri_server import mcp
from naukri_server.config import logger, DISPLAY_MIN_FIT_SCORE
from naukri_server.domain import safe_get
from naukri_server.models import Job
from naukri_server.scoring import parse_skills, score_job
from naukri_server.services.auto_hunt_service import (
    parse_profile_experience_years,
    build_ranked_entry,
)
from naukri_server.validation import validate_limit


@mcp.tool()
async def naukri_auto_hunt(
    keywords: str,
    location: Optional[str] = None,
    min_fit_score: int = DISPLAY_MIN_FIT_SCORE,
    limit: int = 20,
    freshness: Optional[int] = 7,
    work_mode: Optional[str] = None,
    experience: Optional[int] = None,
    salary_min: Optional[int] = None,
    salary_max: Optional[int] = None,
    timeout_seconds: int = 120,
    ctx: Context | None = None,
) -> dict:
    """Automated job hunting — search, score against your profile, return ranked matches.

    Searches for jobs matching your criteria, then scores each against your profile
    for skill overlap, experience match, and location/salary/work-mode bonuses.
    Skills are alias-normalized (JS = JavaScript, k8s = Kubernetes).
    Returns only jobs above the minimum fit score, ranked from best to worst match.

    Does NOT auto-apply — use naukri_apply_top_fits or naukri_apply for that.

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

    # FAIL-CLOSED: halt auto-hunt when the kill-switch is tripped. Auto-hunt
    # drives search traffic and feeds the apply loop; both must stop on a block.
    from naukri_server import kill_switch
    try:
        kill_switch.guard()
    except kill_switch.KillSwitchTrippedError as e:
        return {"status": "halted", "message": str(e),
                "error_code": "KILL_SWITCH_TRIPPED", "block_kind": e.block_kind}

    async def _do_work() -> dict:
        from naukri_server.tools.search import naukri_search_jobs
        from naukri_server.tools.profile import get_cached_profile

        if ctx:
            try:
                await ctx.info(f"Searching '{keywords}' in '{location or 'any'}'")
            except Exception:
                pass

        # Parallel: first page of search + fetch profile
        per_page = min(limit, 20)  # Naukri API caps at 20 per page
        search_result, profile_result = await asyncio.gather(
            naukri_search_jobs(
                keywords=keywords, location=location, limit=limit,
                freshness=freshness, work_mode=work_mode,
                experience=experience, salary_min=salary_min, salary_max=salary_max,
                page=1,
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

        # Paginate: fetch up to 3 pages (60 jobs max)
        all_jobs = list(search_result.get("jobs", []))
        max_pages = min(3, (limit + per_page - 1) // per_page)  # enough pages to cover limit

        if ctx:
            try:
                await ctx.report_progress(
                    1, max_pages,
                    message=f"Page 1: {len(all_jobs)} jobs",
                )
            except Exception:
                pass

        if all_jobs and search_result.get("has_more", False) and max_pages > 1:
            for page_num in range(2, max_pages + 1):
                try:
                    next_result = await naukri_search_jobs(
                        keywords=keywords, location=location, limit=limit,
                        freshness=freshness, work_mode=work_mode,
                        experience=experience, salary_min=salary_min, salary_max=salary_max,
                        page=page_num,
                    )
                except Exception:
                    break  # Later pages failing is OK

                if isinstance(next_result, Exception) or next_result.get("status") == "error":
                    break

                page_jobs = next_result.get("jobs", [])
                if not page_jobs:
                    break
                all_jobs.extend(page_jobs)

                if ctx:
                    try:
                        await ctx.report_progress(
                            page_num, max_pages,
                            message=f"Page {page_num}: {len(page_jobs)} jobs",
                        )
                    except Exception:
                        pass

                if not next_result.get("has_more", False):
                    break

        jobs = all_jobs[:limit]  # Respect the original limit
        if not jobs:
            return {"status": "success", "jobs_found": 0, "jobs_matched": 0, "ranked_jobs": []}

        # Filter out already-applied jobs (API + local tracking)
        pre_filter_count = len(jobs)
        jobs = [j for j in jobs if not j.get("is_applied")]

        # Cross-reference with local applications tracking
        try:
            from naukri_server.database import get_applied_job_ids
            local_applied_ids = await get_applied_job_ids()
            jobs = [j for j in jobs if str(j.get("job_id")) not in local_applied_ids]
        except Exception as e:
            logger.warning("Job cross-ref failed — duplicate detection may be incomplete: %s", e)

        if not jobs:
            return {"status": "success", "jobs_found": pre_filter_count,
                    "jobs_matched": 0, "ranked_jobs": [],
                    "note": "All matching jobs already applied to."}

        profile_skills = parse_skills(safe_get(profile_result, "key_skills", field_name="key_skills", warn=False, default=[]))
        profile_exp = safe_get(profile_result, "total_experience", field_name="total_experience", warn=False)
        profile_location = safe_get(profile_result, "current_location", field_name="current_location", warn=False)
        profile_expected_ctc = safe_get(profile_result, "expected_ctc", field_name="expected_ctc", warn=False)

        # Parse profile experience to float for Job.matches_experience
        profile_exp_years = parse_profile_experience_years(profile_exp)

        if ctx:
            try:
                await ctx.info(f"Scoring {len(jobs)} jobs against profile")
            except Exception:
                pass

        # Score each job
        ranked = []
        for job_dict in jobs:
            job = Job.from_api_dict(job_dict)
            job_skills = parse_skills(job.tags or [])
            fit = score_job(
                job_skills, profile_skills,
                safe_get(job_dict, "experience", field_name="experience", warn=False, default=""),
                profile_exp,
                job_location=job.location,
                profile_location=profile_location,
                job_work_mode=job.work_mode,
                job_salary=safe_get(job_dict, "salary", field_name="salary", warn=False),
                profile_expected_ctc=profile_expected_ctc,
                experience_min=job.experience_min,
                experience_max=job.experience_max,
                is_agent_eligible=job.is_agent_eligible,
            )

            if fit.overall_score >= min_fit_score:
                ranked.append(build_ranked_entry(job, job_dict, fit, profile_exp_years))

        # Sort by fit score descending
        ranked.sort(key=lambda x: x["fit_score"], reverse=True)

        if ctx:
            try:
                await ctx.info(
                    f"Hunt complete: {len(ranked)} matches at >= {min_fit_score}"
                )
            except Exception:
                pass

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
