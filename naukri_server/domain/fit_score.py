"""FitScore aggregate — encapsulates job-profile match scoring logic.

Decomposes the monolithic compute_fit_score() into four frozen dataclasses:
  SkillMatch  — skill overlap calculation
  ExperienceScore — experience fit with sqrt over-qualification penalty
  BonusScore — additive bonuses (location, work_mode, salary, agent)
  FitScore — aggregate combining all three into overall score + recommendation
"""

import math
import re
from dataclasses import dataclass
from typing import Optional


# ── Skill Match ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SkillMatch:
    """Immutable value object for skill overlap between job and profile.

    score property: percentage of job skills matched (50 default when no job skills).
    """
    matched: frozenset
    missing: frozenset
    job_skills: frozenset

    @classmethod
    def compute(cls, job_skills: set, profile_skills: set) -> "SkillMatch":
        matched = frozenset(job_skills & profile_skills)
        missing = frozenset(job_skills - profile_skills)
        return cls(matched=matched, missing=missing, job_skills=frozenset(job_skills))

    @property
    def score(self) -> float:
        if not self.job_skills:
            return 50.0
        return len(self.matched) / len(self.job_skills) * 100


# ── Experience Score ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExperienceScore:
    """Immutable value object for experience fit.

    Uses sqrt penalty for over-qualification (floor 60, not linear cliff).
    Under-qualification: linear 20-point penalty per missing year.
    """
    score: float
    profile_years: float
    min_required: Optional[float]
    max_required: Optional[float]

    @classmethod
    def compute(
        cls,
        job_exp_str: str,
        profile_exp,
        experience_min: Optional[int] = None,
        experience_max: Optional[int] = None,
    ) -> "ExperienceScore":
        """Compute experience score from job requirements and profile experience.

        Args:
            job_exp_str: Experience string like "3-5 years"
            profile_exp: Profile experience (string like "5 years 0 months" or numeric)
            experience_min: Numeric min experience (avoids regex round-trip)
            experience_max: Numeric max experience (avoids regex round-trip)
        """
        exp_score = 50.0  # Default if can't determine
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

            # Prefer numeric fields, fall back to regex
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

        return cls(
            score=exp_score,
            profile_years=p_exp,
            min_required=min_exp,
            max_required=max_exp,
        )


# ── Bonus Score ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BonusScore:
    """Immutable value object for additive bonus points.

    Up to +20 total: location (+5), work_mode (+5), salary (+5), agent (+5).
    """
    location: int
    work_mode: int
    salary: int
    agent_eligible: int

    @property
    def total(self) -> int:
        return self.location + self.work_mode + self.salary + self.agent_eligible

    @classmethod
    def compute(
        cls,
        job_location: Optional[str],
        profile_location: Optional[str],
        job_work_mode: Optional[str],
        job_salary: Optional[str],
        profile_expected_ctc,
        is_agent_eligible,
        score_location_fn,
        score_work_mode_fn,
        score_salary_fn,
    ) -> "BonusScore":
        """Compute all bonus scores using provided scoring functions.

        Scoring functions are injected to keep module-level helpers in scoring.py
        importable by tests while avoiding circular imports.
        """
        return cls(
            location=score_location_fn(job_location, profile_location),
            work_mode=score_work_mode_fn(job_work_mode),
            salary=score_salary_fn(job_salary, profile_expected_ctc),
            agent_eligible=5 if is_agent_eligible else 0,
        )


# ── FitScore Aggregate ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class FitScore:
    """Aggregate root — combines skill, experience, and bonus scores.

    Produces overall_score (capped at 100), recommendation string,
    and explanatory reasons list.
    """
    skill_match: SkillMatch
    experience: ExperienceScore
    bonuses: BonusScore
    # Raw inputs preserved for to_dict() output fidelity
    _profile_exp: object  # raw profile_exp value
    _job_exp_str: str  # raw job_exp_str value
    _has_enrichment: bool  # whether bonus breakdown should appear

    @property
    def overall_score(self) -> int:
        base_score = self.skill_match.score * 0.6 + self.experience.score * 0.4
        return min(100, round(base_score + self.bonuses.total))

    @property
    def recommendation(self) -> str:
        score = self.overall_score
        if score >= 80:
            return "Strong match \u2014 apply confidently"
        if score >= 60:
            return "Good match \u2014 worth applying"
        if score >= 40:
            return "Partial match \u2014 review missing skills before applying"
        return "Weak match \u2014 consider upskilling first"

    @property
    def reasons(self) -> list:
        reasons = []
        if self.skill_match.score < 50:
            missing_sample = sorted(self.skill_match.missing)[:3]
            reasons.append(f"Skill gap: missing {', '.join(missing_sample)}")
        if (
            self.experience.score < 70
            and self.experience.min_required is not None
            and self.experience.max_required is not None
        ):
            p = self.experience.profile_years
            mn = self.experience.min_required
            mx = self.experience.max_required
            if p < mn:
                reasons.append(
                    f"Under-experienced: {p:.0f}yr vs {mn:.0f}-{mx:.0f}yr required"
                )
            elif p > mx:
                reasons.append(
                    f"Over-experienced: {p:.0f}yr vs {mn:.0f}-{mx:.0f}yr range"
                )
        if self.bonuses.total == 0:
            reasons.append("No location/salary/work-mode bonuses")
        return reasons

    @classmethod
    def compute(
        cls,
        job_skills: set,
        profile_skills: set,
        job_exp_str: str,
        profile_exp,
        job_location: Optional[str] = None,
        profile_location: Optional[str] = None,
        job_work_mode: Optional[str] = None,
        job_salary: Optional[str] = None,
        profile_expected_ctc=None,
        experience_min: Optional[int] = None,
        experience_max: Optional[int] = None,
        is_agent_eligible=None,
        score_location_fn=None,
        score_work_mode_fn=None,
        score_salary_fn=None,
    ) -> "FitScore":
        """Factory method mirroring compute_fit_score() signature.

        score_location_fn, score_work_mode_fn, score_salary_fn are injected
        from scoring.py to preserve backward compatibility.
        """
        skill = SkillMatch.compute(job_skills, profile_skills)
        exp = ExperienceScore.compute(
            job_exp_str, profile_exp, experience_min, experience_max,
        )

        # Default no-op scoring functions when none provided
        _loc_fn = score_location_fn or (lambda jl, pl: 0)
        _wm_fn = score_work_mode_fn or (lambda wm: 0)
        _sal_fn = score_salary_fn or (lambda js, ctc: 0)

        bonus = BonusScore.compute(
            job_location, profile_location,
            job_work_mode,
            job_salary, profile_expected_ctc,
            is_agent_eligible,
            _loc_fn, _wm_fn, _sal_fn,
        )

        has_enrichment = (
            job_location is not None
            or job_work_mode is not None
            or job_salary is not None
            or is_agent_eligible is not None
        )

        return cls(
            skill_match=skill,
            experience=exp,
            bonuses=bonus,
            _profile_exp=profile_exp,
            _job_exp_str=job_exp_str,
            _has_enrichment=has_enrichment,
        )

    def to_dict(self) -> dict:
        """Produce the exact same dict shape as legacy compute_fit_score().

        Keys: overall_score, skill_match, experience_match, recommendation,
              reasons, and conditionally bonuses.
        """
        result = {
            "overall_score": self.overall_score,
            "skill_match": {
                "score": round(self.skill_match.score),
                "matched": sorted(self.skill_match.matched),
                "missing": sorted(self.skill_match.missing),
            },
            "experience_match": {
                "score": round(self.experience.score),
                "your_experience": self._profile_exp,
                "required": self._job_exp_str,
            },
            "recommendation": self.recommendation,
            "reasons": self.reasons,
        }

        if self._has_enrichment:
            result["bonuses"] = {
                "location": self.bonuses.location,
                "work_mode": self.bonuses.work_mode,
                "salary": self.bonuses.salary,
                "agent_eligible": self.bonuses.agent_eligible,
                "total": self.bonuses.total,
            }

        return result
