"""Skill gap analysis — find systematic gaps across multiple job listings."""

import asyncio
from collections import Counter
from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger
from naukri_server.scoring import normalize_skill, parse_skills


async def _skill_gap_analysis(
    keywords: Optional[str] = None,
    use_recommendations: bool = True,
    sample_size: int = 20,
    include_assessments: bool = True,
    timeout_seconds: int = 120,
) -> dict:
    """Analyze skill gaps across multiple jobs to find your most common missing skills.

    Fetches N jobs (via recommendations or search) and your profile in parallel,
    then computes which skills you're missing most often. Helps prioritize upskilling.
    When include_assessments is True, passed Naukri skill assessments boost the
    frequency of matched skills by 2x (they appear more valuable to recruiters).

    Args:
        keywords: Search keywords (required if use_recommendations is False)
        use_recommendations: If True, use personalized recommendations; if False, use search (default True)
        sample_size: Number of jobs to analyze (default 20, max 50)
        include_assessments: If True, fetch assessments and boost passed-skill frequency by 2x (default True)
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        - {status: "success", jobs_analyzed, skill_gaps: [{skill, frequency, percentage,
           sample_jobs}], strong_skills: [{skill, frequency, percentage, assessment_passed?}],
           assessments_used: int}
        - {status: "error", message}
    """
    if not use_recommendations and not keywords:
        return {"status": "error", "message": "keywords is required when use_recommendations is False.", "error_code": "VALIDATION_ERROR"}

    sample_size = min(sample_size, 50)

    async def _do_work() -> dict:
        from naukri_server.tools.search import naukri_search_jobs, naukri_get_recommendations
        from naukri_server.tools.profile import get_cached_profile
        from naukri_server.tools.assessments import _list_assessments

        # Parallel: fetch jobs + profile (+ assessments if enabled)
        if use_recommendations:
            jobs_coro = naukri_get_recommendations(limit=sample_size)
        else:
            jobs_coro = naukri_search_jobs(keywords=keywords, limit=sample_size)

        coros = [jobs_coro, get_cached_profile()]
        if include_assessments:
            coros.append(_list_assessments())

        gather_results = await asyncio.gather(*coros, return_exceptions=True)

        jobs_result = gather_results[0]
        profile_result = gather_results[1]
        assessment_result = gather_results[2] if include_assessments else None

        if isinstance(jobs_result, Exception) or jobs_result.get("status") == "error":
            msg = str(jobs_result) if isinstance(jobs_result, Exception) else jobs_result.get("message")
            return {"status": "error", "message": f"Failed to fetch jobs: {msg}", "error_code": "API_ERROR"}

        if isinstance(profile_result, Exception) or profile_result.get("status") == "error":
            msg = str(profile_result) if isinstance(profile_result, Exception) else profile_result.get("message")
            return {"status": "error", "message": f"Failed to fetch profile: {msg}", "error_code": "API_ERROR"}

        jobs = jobs_result.get("jobs", [])
        if not jobs:
            return {"status": "error", "message": "No jobs found to analyze.", "error_code": "NOT_FOUND"}

        profile_skills = parse_skills(profile_result.get("key_skills", []))

        # Build experience depth map for weighting gaps
        exp_map = {}
        for s in profile_result.get("skills_with_experience", []):
            if isinstance(s, dict):
                name = normalize_skill(s.get("skill", ""))
                years = s.get("experience_years", 0) or 0
                months = s.get("experience_months", 0) or 0
                exp_map[name] = years + months / 12

        # Extract passed assessment skills (normalized)
        passed_skills: set[str] = set()
        if include_assessments and assessment_result is not None:
            if not isinstance(assessment_result, Exception):
                if isinstance(assessment_result, dict) and assessment_result.get("status") == "success":
                    for assessment in assessment_result.get("assessments", []):
                        status = (assessment.get("status") or "").lower()
                        if status in ("passed", "completed"):
                            skill_name = (assessment.get("skill") or "").strip()
                            if skill_name:
                                passed_skills.add(normalize_skill(skill_name))
                else:
                    logger.warning("Assessments fetch failed (non-fatal): %s", assessment_result)
            else:
                logger.warning("Assessments fetch raised (non-fatal): %s", assessment_result)

        # Analyze each job
        missing_counter = Counter()  # skill -> count of jobs missing it
        matched_counter = Counter()  # skill -> count of jobs matching it
        missing_jobs = {}  # skill -> list of job titles where it's missing

        for job in jobs:
            job_skills = parse_skills(job.get("tags") or job.get("skills") or [])
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

        # Boost matched skill frequency by 2x for passed assessments
        if passed_skills:
            for skill in list(matched_counter):
                if normalize_skill(skill) in passed_skills:
                    matched_counter[skill] *= 2

        # Build skill gaps (sorted by frequency)
        skill_gaps = []
        for skill, freq in missing_counter.most_common(30):
            skill_gaps.append({
                "skill": skill,
                "frequency": freq,
                "percentage": round(freq / jobs_analyzed * 100),
                "sample_jobs": missing_jobs.get(skill, []),
            })

        # Build strong skills (sorted by boosted frequency)
        strong_skills = []
        for skill, freq in matched_counter.most_common(20):
            entry = {
                "skill": skill,
                "frequency": freq,
                "percentage": round(freq / jobs_analyzed * 100),
            }
            if normalize_skill(skill) in passed_skills:
                entry["assessment_passed"] = True
            strong_skills.append(entry)

        # For matched skills, add experience depth
        for skill_entry in strong_skills:
            skill_name = normalize_skill(skill_entry["skill"])
            skill_entry["your_experience_years"] = round(exp_map.get(skill_name, 0), 1)

        return {
            "status": "success",
            "jobs_analyzed": jobs_analyzed,
            "assessments_used": len(passed_skills),
            "skill_gaps": skill_gaps,
            "strong_skills": strong_skills,
        }

    try:
        return await asyncio.wait_for(_do_work(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return {"status": "partial_success", "message": f"Timed out after {timeout_seconds}s", "error_code": "TIMEOUT"}


naukri_skill_gap_analysis = _skill_gap_analysis
