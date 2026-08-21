"""FitScore aggregate — encapsulates job-profile match scoring logic.

RE-EXPORT SHIM. The implementation moved to ``jobcore.fit``:

  SkillMatch      — skill overlap calculation
  ExperienceScore — experience fit with sqrt over-qualification penalty
  BonusScore      — additive bonuses (location, work_mode, salary, agent)
  FitScore        — aggregate combining all three into overall score + recommendation

Every import path that worked before still works:

    from naukri_server.domain.fit_score import FitScore

The aggregate still accepts injected ``score_location_fn`` /
``score_work_mode_fn`` / ``score_salary_fn`` callables, and it now also accepts
``policy=`` — the numbers behind a score (the 60/40 split, the bonus cap, the
verdict bands, the experience penalties) are values, not literals.

**Production code must not construct it directly.** ``tools/auto_hunt.py``,
``tools/compare.py`` and ``tools/smart_apply.py`` used to, and each therefore
scored on the shipped defaults no matter what the operator had configured —
while ``daily_brief`` alone moved. All four now go through
``naukri_server.scoring.score_job``, which binds the base score and the three
bonus helpers to ONE policy object. ``tests/test_one_engine.py`` grep-bans a
direct construction outside that module and pins all four to one number under a
non-default policy.
"""

from jobcore.fit import (
    BonusScore,
    ExperienceScore,
    FitScore,
    SkillMatch,
)

__all__ = ["SkillMatch", "ExperienceScore", "BonusScore", "FitScore"]
