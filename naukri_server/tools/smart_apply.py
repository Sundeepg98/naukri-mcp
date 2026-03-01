"""Smart apply — job fit assessment before applying."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger


@mcp.tool()
async def naukri_smart_apply(
    job_id: str,
    apply_if_fit: bool = False,
    answers: Optional[dict] = None,
) -> dict:
    """Assess job fit before applying — compares your profile against the job.

    Fetches job details and your profile in parallel, then computes:
    - Skill overlap (matched vs missing)
    - Experience match
    - Overall recommendation

    Args:
        job_id: Naukri job ID
        apply_if_fit: If True, automatically apply when fit score >= 60%
        answers: Optional screening question answers (for auto-apply)

    Returns:
        - {status: "success", fit_assessment: {score, skill_match, experience_match,
           recommendation}, job_summary, applied}
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

    # Extract data
    job_skills = set(s.lower() for s in job_result.get("skills", []))
    profile_skills = set(s.lower() for s in profile_result.get("key_skills", []))

    job_exp_str = job_result.get("experience", "")
    profile_exp = profile_result.get("total_experience")

    job_salary = job_result.get("salary", "")

    # Skill match
    matched_skills = job_skills & profile_skills
    missing_skills = job_skills - profile_skills
    skill_score = (len(matched_skills) / len(job_skills) * 100) if job_skills else 50

    # Experience match
    exp_score = 50  # Default if can't determine
    if profile_exp is not None and job_exp_str:
        import re
        exp_nums = re.findall(r'(\d+)', str(job_exp_str))
        # Parse profile experience — may be "5 years 0 months" or numeric
        p_exp_match = re.findall(r'(\d+)', str(profile_exp))
        p_exp = float(p_exp_match[0]) if p_exp_match else 0
        if len(exp_nums) >= 2:
            min_exp, max_exp = float(exp_nums[0]), float(exp_nums[1])
            if min_exp <= p_exp <= max_exp:
                exp_score = 100
            elif p_exp < min_exp:
                exp_score = max(0, 100 - (min_exp - p_exp) * 20)
            else:
                exp_score = max(50, 100 - (p_exp - max_exp) * 10)

    # Overall score
    overall_score = round(skill_score * 0.6 + exp_score * 0.4)

    # Recommendation
    if overall_score >= 80:
        recommendation = "Strong match — apply confidently"
    elif overall_score >= 60:
        recommendation = "Good match — worth applying"
    elif overall_score >= 40:
        recommendation = "Partial match — review missing skills before applying"
    else:
        recommendation = "Weak match — consider upskilling first"

    result = {
        "status": "success",
        "job_summary": {
            "title": job_result.get("title"),
            "company": job_result.get("company"),
            "salary": job_salary,
            "experience": job_exp_str,
            "location": job_result.get("location"),
            "work_mode": job_result.get("work_mode"),
        },
        "fit_assessment": {
            "overall_score": overall_score,
            "skill_match": {
                "score": round(skill_score),
                "matched": sorted(matched_skills),
                "missing": sorted(missing_skills),
            },
            "experience_match": {
                "score": round(exp_score),
                "your_experience": profile_exp,
                "required": job_exp_str,
            },
            "recommendation": recommendation,
        },
        "applied": False,
    }

    # Auto-apply if requested and fit
    if apply_if_fit and overall_score >= 60:
        apply_result = await _apply_single(
            job_id=job_id, answers=answers,
            title=job_result.get("title"), company=job_result.get("company"),
            tracking_extra={"source": "smart_apply", "fit_score": overall_score},
        )
        result["applied"] = apply_result.get("status") == "applied"
        result["apply_result"] = apply_result

    return result
