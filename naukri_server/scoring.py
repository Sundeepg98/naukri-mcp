"""Shared fit scoring logic for job-profile matching.

RE-EXPORT SHIM. The implementation moved to ``jobcore.scoring``. This module
binds a :class:`jobcore.scoring.ScoringEngine` to Naukri's salary units and
re-exports the flat function API, so every existing call site and test import
still resolves:

    from naukri_server.scoring import normalize_skill, parse_skills, compute_fit_score
    from naukri_server.scoring import _score_location, _score_work_mode, _score_salary

The ``_score_*`` helpers keep their leading underscore and their exact
signatures because ``tools/auto_hunt.py``, ``tools/compare.py`` and
``tools/smart_apply.py`` import them by name and hand them to
``FitScore.compute()``.
"""

from typing import Optional

from jobcore.fit import FitScore
from jobcore.scoring import ScoringEngine

from naukri_server.domain.skill_taxonomy import DEFAULT_TAXONOMY, SKILL_ALIASES

__all__ = [
    "normalize_skill",
    "parse_skills",
    "compute_fit_score",
    "FitScore",
    "SKILL_ALIASES",
    "DEFAULT_TAXONOMY",
]

# The engine needs naukri_server.domain.salary, which imports
# naukri_server.config. Build it on first use rather than at import time so the
# module keeps the same import-order tolerance it had when the Salary import
# lived inside _score_salary().
_ENGINE: Optional[ScoringEngine] = None


def _engine() -> ScoringEngine:
    global _ENGINE
    if _ENGINE is None:
        from naukri_server.domain.salary import Salary

        _ENGINE = ScoringEngine(taxonomy=DEFAULT_TAXONOMY, salary_cls=Salary)
    return _ENGINE


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
    return _engine().score_location(job_location, profile_location)


def _score_work_mode(job_work_mode: Optional[str]) -> int:
    """Score work mode. Remote/WFH gets a bonus. Returns 0-5 bonus points."""
    return _engine().score_work_mode(job_work_mode)


def _score_salary(job_salary: Optional[str], profile_expected_ctc) -> int:
    """Score salary fit. Returns 0-5 bonus points.

    Only scores when both job salary and profile expected CTC are available.
    Accepts profile_expected_ctc as float or string (e.g., "15.0 Lacs").
    """
    return _engine().score_salary(job_salary, profile_expected_ctc)


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

    Delegates to jobcore's ScoringEngine and returns its dict representation.

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
    return _engine().compute_fit_score(
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
    )
