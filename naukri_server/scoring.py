"""Shared fit scoring logic for job-profile matching."""

import re


def parse_skills(raw) -> set:
    """Normalize skills from any format (string, list, set) to a lowercase set."""
    if isinstance(raw, set):
        return {s.lower().strip() for s in raw if s}
    if isinstance(raw, str):
        return {s.strip().lower() for s in raw.split(",") if s.strip()}
    if isinstance(raw, (list, tuple)):
        return {s.lower().strip() for s in raw if isinstance(s, str) and s.strip()}
    return set()


def compute_fit_score(job_skills: set, profile_skills: set, job_exp_str: str, profile_exp) -> dict:
    """Compute fit score between a job and a candidate profile.

    Args:
        job_skills: Set of lowercase job skill strings
        profile_skills: Set of lowercase profile skill strings
        job_exp_str: Experience string like "3-5 years"
        profile_exp: Profile experience (may be string like "5 years 0 months" or numeric)

    Returns:
        {overall_score, skill_match: {score, matched, missing},
         experience_match: {score, your_experience, required}, recommendation}
    """
    # Skill match
    matched_skills = job_skills & profile_skills
    missing_skills = job_skills - profile_skills
    skill_score = (len(matched_skills) / len(job_skills) * 100) if job_skills else 50

    # Experience match
    exp_score = 50  # Default if can't determine
    if profile_exp is not None and job_exp_str:
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

    return {
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
    }
