"""FitScore aggregate — encapsulates job-profile match scoring logic.

RE-EXPORT SHIM. The implementation moved to ``jobcore.fit``:

  SkillMatch      — skill overlap calculation
  ExperienceScore — experience fit with sqrt over-qualification penalty
  BonusScore      — additive bonuses (location, work_mode, salary, agent)
  FitScore        — aggregate combining all three into overall score + recommendation

Every import path that worked before still works:

    from naukri_server.domain.fit_score import FitScore

``FitScore.compute()`` still takes injected ``score_location_fn`` /
``score_work_mode_fn`` / ``score_salary_fn`` callables, so the call sites in
``tools/auto_hunt.py``, ``tools/compare.py`` and ``tools/smart_apply.py`` are
unchanged.
"""

from jobcore.fit import (
    BonusScore,
    ExperienceScore,
    FitScore,
    SkillMatch,
)

__all__ = ["SkillMatch", "ExperienceScore", "BonusScore", "FitScore"]
