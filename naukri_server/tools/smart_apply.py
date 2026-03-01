"""Smart apply — job fit assessment before applying."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger
from naukri_server.scoring import compute_fit_score, parse_skills


@mcp.tool()
async def naukri_smart_apply(
    job_id: str,
    apply_if_fit: bool = False,
    answers: Optional[dict] = None,
) -> dict:
    """Assess job fit before applying — compares your profile against the job.

    Fetches job details and your profile in parallel, then computes:
    - Skill overlap with alias normalization (JS = JavaScript, k8s = Kubernetes)
    - Experience match
    - Location, work mode, and salary bonuses
    - Overall recommendation

    Args:
        job_id: Naukri job ID
        apply_if_fit: If True, automatically apply when fit score >= 60%
        answers: Optional screening question answers (for auto-apply)

    Returns:
        - {status: "success", fit_assessment: {overall_score, skill_match,
           experience_match, bonuses, recommendation}, job_summary, applied}
        - {status: "error", message}
    """
    from naukri_server.tools.jobs import naukri_get_job
    from naukri_server.tools.profile import naukri_get_profile
    from naukri_server.tools.apply import _apply_single

    # Parallel fetch job + profile
    job_result, profile_result = await asyncio.gather(
        naukri_get_job(job_id_or_url=job_id),
        naukri_get_profile(),
        return_exceptions=True,
    )

    if isinstance(job_result, Exception) or job_result.get("status") == "error":
        msg = str(job_result) if isinstance(job_result, Exception) else job_result.get("message")
        return {"status": "error", "message": f"Failed to fetch job: {msg}"}

    if isinstance(profile_result, Exception) or profile_result.get("status") == "error":
        msg = str(profile_result) if isinstance(profile_result, Exception) else profile_result.get("message")
        return {"status": "error", "message": f"Failed to fetch profile: {msg}"}

    # Compute fit — use tags-or-skills fallback, pass enrichment data
    job_skills = parse_skills(job_result.get("tags") or job_result.get("skills") or [])
    profile_skills = parse_skills(profile_result.get("key_skills", []))
    fit = compute_fit_score(
        job_skills, profile_skills,
        job_result.get("experience", ""),
        profile_result.get("total_experience"),
        job_location=job_result.get("location"),
        profile_location=profile_result.get("current_location"),
        job_work_mode=job_result.get("work_mode"),
        job_salary=job_result.get("salary"),
        profile_expected_ctc=profile_result.get("expected_ctc"),
    )

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
    }

    # Auto-apply if requested and fit
    if apply_if_fit and fit["overall_score"] >= 60:
        apply_result = await _apply_single(
            job_id=job_id, answers=answers,
            title=job_result.get("title"), company=job_result.get("company"),
            tracking_extra={"source": "smart_apply", "fit_score": fit["overall_score"]},
        )
        result["applied"] = apply_result.get("status") == "applied"
        result["apply_result"] = apply_result
        # Quota warning
        daily = apply_result.get("daily_applied")
        if daily is not None and daily >= 45:
            result["quota_warning"] = f"Daily quota: {daily}/50 used. {50 - daily} remaining."

    return result
