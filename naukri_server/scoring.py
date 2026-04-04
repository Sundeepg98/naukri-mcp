"""Shared fit scoring logic for job-profile matching."""

import re
from typing import Optional

from naukri_server.domain.fit_score import FitScore
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
    from naukri_server.domain.salary import Salary

    if not job_salary or profile_expected_ctc is None:
        return 0
    # Parse profile CTC to float if string
    if isinstance(profile_expected_ctc, str):
        ctc_nums = re.findall(r'(\d+(?:\.\d+)?)', profile_expected_ctc)
        if not ctc_nums:
            return 0
        profile_expected_ctc = float(ctc_nums[0])
    elif not isinstance(profile_expected_ctc, (int, float)):
        return 0

    salary = Salary.from_string(job_salary)
    if not salary.is_disclosed:
        return 0
    return salary.compare_to_ctc(profile_expected_ctc)


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

    Delegates to FitScore aggregate and returns its dict representation.

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
    return FitScore.compute(
        job_skills=job_skills,
        profile_skills=profile_skills,
        job_exp_str=job_exp_str,
        profile_exp=profile_exp,
        job_location=job_location,
        profile_location=profile_location,
        job_work_mode=job_work_mode,
        job_salary=job_salary,
        profile_expected_ctc=profile_expected_ctc,
        experience_min=experience_min,
        experience_max=experience_max,
        is_agent_eligible=is_agent_eligible,
        score_location_fn=_score_location,
        score_work_mode_fn=_score_work_mode,
        score_salary_fn=_score_salary,
    ).to_dict()
