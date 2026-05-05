"""Auto-hunt service — pure helpers for profile parsing and ranked-job assembly.

Extracted from tools/auto_hunt.py so the per-job scoring loop can stay focused
on orchestration (parallel I/O + filtering). The actual SearchPort + ApplyPort
orchestration stays in the tool because tests patch
naukri_server.tools.auto_hunt.naukri_auto_hunt at this exact path.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from naukri_server.domain import safe_get
from naukri_server.domain.fit_score import FitScore
from naukri_server.models import Job

__all__ = [
    "parse_profile_experience_years",
    "build_ranked_entry",
]


_EXP_NUMBER_RE = re.compile(r"(\d+)")


def parse_profile_experience_years(profile_exp: Optional[str]) -> float:
    """Parse 'N years M months' into a float year value.

    Returns 0.0 for empty / unrecognised input. Used by Job.matches_experience.

    Examples:
        '5 years 6 months' -> 5.5
        '7 years'          -> 7.0
        '0 years 8 months' -> ~0.667
        ''                 -> 0.0
    """
    nums = _EXP_NUMBER_RE.findall(str(profile_exp or ""))
    if len(nums) >= 2:
        return float(nums[0]) + float(nums[1]) / 12.0
    if nums:
        return float(nums[0])
    return 0.0


def build_ranked_entry(
    job: Job, job_dict: dict, fit: FitScore, profile_exp_years: float,
) -> dict:
    """Assemble the per-job dict that auto-hunt returns.

    All raw-dict access goes through ``safe_get`` so any future schema drift
    in the search-result shape gets logged at the boundary.
    """
    bonuses: dict[str, Any] | None
    if fit._has_enrichment:
        bonuses = {
            "location": fit.bonuses.location,
            "work_mode": fit.bonuses.work_mode,
            "salary": fit.bonuses.salary,
            "agent_eligible": fit.bonuses.agent_eligible,
            "total": fit.bonuses.total,
        }
    else:
        bonuses = None

    return {
        "job_id": job.job_id,
        "title": job.title,
        "company": job.company,
        "salary": safe_get(job_dict, "salary", field_name="salary", warn=False),
        "location": job.location,
        "work_mode": job.work_mode,
        "experience": safe_get(job_dict, "experience", field_name="experience", warn=False),
        "is_applied": job.is_applied,
        "salary_disclosed": job.salary_disclosed,
        "experience_match": job.matches_experience(profile_exp_years),
        "fit_score": fit.overall_score,
        "matched_skills": sorted(fit.skill_match.matched),
        "missing_skills": sorted(fit.skill_match.missing),
        "recommendation": fit.recommendation,
        "bonuses": bonuses,
    }
