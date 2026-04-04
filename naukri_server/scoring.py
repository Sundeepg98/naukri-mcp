"""Shared fit scoring logic for job-profile matching."""

import math
import re
from typing import Optional

from naukri_server.domain.skill_taxonomy import SKILL_ALIASES, DEFAULT_TAXONOMY


def normalize_skill(skill: str) -> str:
    """Normalize a skill string to its canonical form via alias lookup."""
    return DEFAULT_TAXONOMY.normalize(skill)


def parse_skills(raw) -> set:
    """Normalize skills from any format (string, list, set) to a canonical lowercase set.

    Handles comma-separated strings, lists, tuples, and sets.
    All skills are normalized through SKILL_ALIASES (e.g., "JS" -> "javascript").
    """
    return set(DEFAULT_TAXONOMY.parse_set(raw))


# ── Bonus Scoring Helpers ────────────────────────────────────────────────────

def _score_location(job_location: Optional[str], profile_location: Optional[str]) -> int:
    """Score location match. Returns 0 or 5 bonus points."""
    if not job_location or not profile_location:
        return 0
    jl = job_location.lower().strip()
    pl = profile_location.lower().strip()
    # Exact city match (substring to handle "Bangalore/Bengaluru" vs "Bangalore")
    if pl in jl or jl in pl:
        return 5
    # Remote is universally acceptable
    if any(w in jl for w in ("remote", "wfh", "work from home", "anywhere")):
        return 5
    return 0


def _score_work_mode(job_work_mode: Optional[str]) -> int:
    """Score work mode. Remote/WFH gets a bonus. Returns 0-5 bonus points."""
    if not job_work_mode:
        return 0
    wm = job_work_mode.lower().strip()
    if wm in ("wfh", "remote", "work from home"):
        return 5
    if wm == "hybrid":
        return 3
    return 0  # Office — no penalty, just no bonus


def _score_salary(job_salary: Optional[str], profile_expected_ctc) -> int:
    """Score salary fit. Returns 0-5 bonus points.

    Only scores when both job salary and profile expected CTC are available.
    Accepts profile_expected_ctc as float or string (e.g., "15.0 Lacs").
    """
    if not job_salary or profile_expected_ctc is None:
        return 0
    if "not disclosed" in job_salary.lower():
        return 0
    # Parse profile CTC to float if string
    if isinstance(profile_expected_ctc, str):
        ctc_nums = re.findall(r'(\d+(?:\.\d+)?)', profile_expected_ctc)
        if not ctc_nums:
            return 0
        profile_expected_ctc = float(ctc_nums[0])
    elif not isinstance(profile_expected_ctc, (int, float)):
        return 0
    nums = re.findall(r'(\d+(?:\.\d+)?)', job_salary)
    if len(nums) < 2:
        return 0
    try:
        job_max = float(nums[-1])
        if job_max > 200:  # Likely wrong unit — can't compare
            return 0
        if job_max >= profile_expected_ctc:
            return 5  # Meets or exceeds expectation
        elif job_max >= profile_expected_ctc * 0.8:
            return 3  # Within 20%
        return 0  # Below expectation
    except (ValueError, IndexError):
        return 0


# ── Main Scoring Function ───────────────────────────────────────────────────

def compute_fit_score(
    job_skills: set,
    profile_skills: set,
    job_exp_str: str,
    profile_exp,
    # Optional enrichment (backward compatible — all default to None)
    job_location: Optional[str] = None,
    profile_location: Optional[str] = None,
    job_work_mode: Optional[str] = None,
    job_salary: Optional[str] = None,
    profile_expected_ctc=None,
    # Numeric experience fields (avoid regex round-trip when available)
    experience_min: Optional[int] = None,
    experience_max: Optional[int] = None,
    # Agent eligibility bonus (backward compatible — defaults to None)
    is_agent_eligible=None,
) -> dict:
    """Compute fit score between a job and a candidate profile.

    Base score: 60% skills + 40% experience (unchanged from v1).
    Additive bonuses: +5 location match, +5 remote/WFH, +5 salary fit (max +15).
    Overall score capped at 100.

    Args:
        job_skills: Set of normalized job skill strings
        profile_skills: Set of normalized profile skill strings
        job_exp_str: Experience string like "3-5 years"
        profile_exp: Profile experience (string like "5 years 0 months" or numeric)
        job_location: Job city/location string (optional)
        profile_location: User's current location (optional)
        job_work_mode: "WFH", "Hybrid", "Office", etc. (optional)
        job_salary: Salary string like "15-20 LPA" (optional)
        profile_expected_ctc: Expected CTC in LPA (optional)

    Returns:
        {overall_score, skill_match, experience_match, bonuses (if data provided),
         recommendation}
    """
    # ── Skill match (unchanged) ──
    matched_skills = job_skills & profile_skills
    missing_skills = job_skills - profile_skills
    skill_score = (len(matched_skills) / len(job_skills) * 100) if job_skills else 50

    # ── Experience match ──
    exp_score = 50  # Default if can't determine
    p_exp = 0.0
    min_exp = max_exp = None
    if profile_exp is not None and (job_exp_str or experience_min is not None):
        p_exp_match = re.findall(r'(\d+)', str(profile_exp))
        if len(p_exp_match) >= 2:
            p_exp = float(p_exp_match[0]) + float(p_exp_match[1]) / 12.0
        elif p_exp_match:
            p_exp = float(p_exp_match[0])
        else:
            p_exp = 0.0

        # Prefer numeric fields (avoid regex round-trip), fall back to regex
        if experience_min is not None and experience_max is not None:
            min_exp, max_exp = float(experience_min), float(experience_max)
        elif job_exp_str:
            exp_nums = re.findall(r'(\d+)', str(job_exp_str))
            if len(exp_nums) >= 2:
                min_exp, max_exp = float(exp_nums[0]), float(exp_nums[1])
            else:
                min_exp = max_exp = None
        else:
            min_exp = max_exp = None

        if min_exp is not None and max_exp is not None:
            if min_exp <= p_exp <= max_exp:
                exp_score = 100
            elif p_exp < min_exp:
                exp_score = max(0, 100 - (min_exp - p_exp) * 20)
            else:
                exp_score = max(60, 100 - math.sqrt(max(0, p_exp - max_exp)) * 15)

    # ── Base score (unchanged formula) ──
    base_score = skill_score * 0.6 + exp_score * 0.4

    # ── Additive bonuses (new) ──
    loc_bonus = _score_location(job_location, profile_location)
    wm_bonus = _score_work_mode(job_work_mode)
    sal_bonus = _score_salary(job_salary, profile_expected_ctc)
    agent_bonus = 5 if is_agent_eligible else 0
    total_bonus = loc_bonus + wm_bonus + sal_bonus + agent_bonus

    overall_score = min(100, round(base_score + total_bonus))

    # ── Recommendation ──
    if overall_score >= 80:
        recommendation = "Strong match — apply confidently"
    elif overall_score >= 60:
        recommendation = "Good match — worth applying"
    elif overall_score >= 40:
        recommendation = "Partial match — review missing skills before applying"
    else:
        recommendation = "Weak match — consider upskilling first"

    # ── Recommendation reasons ──
    reasons = []
    if skill_score < 50:
        reasons.append(f"Skill gap: missing {', '.join(list(missing_skills)[:3])}")
    if exp_score < 70 and min_exp is not None and max_exp is not None:
        if p_exp < min_exp:
            reasons.append(f"Under-experienced: {p_exp:.0f}yr vs {min_exp:.0f}-{max_exp:.0f}yr required")
        elif p_exp > max_exp:
            reasons.append(f"Over-experienced: {p_exp:.0f}yr vs {min_exp:.0f}-{max_exp:.0f}yr range")
    if total_bonus == 0:
        reasons.append("No location/salary/work-mode bonuses")

    result = {
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
        "reasons": reasons,
    }

    # Include bonus breakdown when any enrichment data was provided
    if job_location is not None or job_work_mode is not None or job_salary is not None or is_agent_eligible is not None:
        result["bonuses"] = {
            "location": loc_bonus,
            "work_mode": wm_bonus,
            "salary": sal_bonus,
            "agent_eligible": agent_bonus,
            "total": total_bonus,
        }

    return result
