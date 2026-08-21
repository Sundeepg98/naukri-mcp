"""Shared fit scoring logic for job-profile matching.

RE-EXPORT SHIM over ``jobcore.scoring``, plus the ONE function every tool in
this package scores through.

WHY score_job() EXISTS
----------------------

Until 2026-08-21 four tools scored jobs and only one of them went through the
engine::

    naukri_daily_brief   -> compute_fit_score -> _engine()
    naukri_auto_hunt     -> FitScore.compute(...)   DIRECT   <- the AGENT's scorer
    naukri_assess_fit    -> FitScore.compute(...)   DIRECT
    naukri_compare_jobs  -> FitScore.compute(...)   DIRECT

``FitScore`` is a frozen dataclass whose 60/40 split, bonus cap, verdict bands
and experience penalties live in ``@property`` methods — unreachable from
``ScoringEngine.__init__``. So injecting a policy into the engine reached one of
the four, and the golden corpus could not see it, because the corpus runs at
DEFAULT policy where both branches hold the same literals.

``score_job()`` is now the single entry point. It binds the base score AND the
three bonus helpers to the SAME policy object, so there is no way to score the
base under one policy and the bonuses under another. ``tests/test_one_engine.py``
runs all four paths under a non-default policy and demands one number, and
grep-bans a direct ``FitScore.compute`` outside this module.

The engine is keyed by the policy's fingerprint hash, not built once and cached
forever: under a mutable policy a module-global engine is a stale-engine bug.
Rebuilding costs a taxonomy lookup and a frozen dataclass, and only happens when
the hash actually moves.
"""

from typing import Optional

from jobcore.fit import FitScore
from jobcore.policy import DEFAULT_SCORING_POLICY, ScoringPolicy
from jobcore.scoring import ScoringEngine

from naukri_server.domain.skill_taxonomy import DEFAULT_TAXONOMY, SKILL_ALIASES

__all__ = [
    "normalize_skill",
    "parse_skills",
    "compute_fit_score",
    "score_job",
    "current_policy",
    "FitScore",
    "SKILL_ALIASES",
    "DEFAULT_TAXONOMY",
]

# policy -> engine. Keyed rather than singleton: see the module docstring.
# ScoringPolicy is a frozen dataclass with FrozenMap/tuple fields, so it is
# hashable AND compares by value — the key is the policy itself rather than a
# digest of it, which removes any question of two policies colliding.
_ENGINES: dict[ScoringPolicy, ScoringEngine] = {}

#: Policy changes are rare (he edits a file), so this never grows in practice.
#: Bounded anyway, because an unbounded cache keyed on caller-supplied objects
#: is a leak waiting for the one caller that builds a policy per job.
_MAX_ENGINES = 8


def current_policy() -> ScoringPolicy:
    """The scoring policy for this call, from the bound config snapshot.

    Import-time safe and failure-safe: if the config layer is unavailable for
    any reason, this returns the shipped defaults — which are today's literals,
    so the server keeps scoring exactly as it does with no config file at all.
    """
    try:
        from naukri_server import policy as _policy

        return _policy.scoring_policy()
    except Exception:  # pragma: no cover - the loader already swallows
        return DEFAULT_SCORING_POLICY


def _engine(policy: Optional[ScoringPolicy] = None) -> ScoringEngine:
    pol = policy if policy is not None else current_policy()
    engine = _ENGINES.get(pol)
    if engine is None:
        # Imported here, not at module scope: naukri_server.domain.salary
        # imports naukri_server.config, and this module has to keep the
        # import-order tolerance it had when the import lived inside
        # _score_salary().
        from naukri_server.domain.salary import Salary

        # salary_cls=, never salary_config=: the latter mints a new dynamic
        # `ConfiguredSalary` subclass on every rebuild (jobcore scoring.py's
        # type() call), and a rebuild path that creates classes is a leak.
        engine = ScoringEngine(taxonomy=DEFAULT_TAXONOMY, salary_cls=Salary,
                               policy=pol)
        if len(_ENGINES) >= _MAX_ENGINES:
            _ENGINES.pop(next(iter(_ENGINES)))
        _ENGINES[pol] = engine
    return engine


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
#
# Kept, with their leading underscore and their exact signatures, because tests
# and any out-of-tree caller import them by name. Production tools no longer
# pass them to FitScore.compute() — score_job() binds them from one policy.

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


# ── The single scoring entry point ──────────────────────────────────────────

def score_job(
    job_skills: set,
    profile_skills: set,
    job_exp_str: str,
    profile_exp,
    *,
    job_location: Optional[str] = None,
    profile_location: Optional[str] = None,
    job_work_mode: Optional[str] = None,
    job_salary: Optional[str] = None,
    profile_expected_ctc=None,
    experience_min: Optional[int] = None,
    experience_max: Optional[int] = None,
    is_agent_eligible=None,
    policy: Optional[ScoringPolicy] = None,
) -> FitScore:
    """Score one job against one profile under the currently bound policy.

    Every tool in this package scores through here. Returns the typed
    :class:`jobcore.fit.FitScore` aggregate, which carries the policy that
    produced it — so a stored score stays interpretable after the policy moves.

    Use :func:`compute_fit_score` when a flat dict is what the tool result wants.
    """
    return _engine(policy).fit_score(
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

    Base score: 60% skills + 40% experience — under the SHIPPED policy. Those
    numbers are policy now, not literals; ``naukri_config()`` reports what they
    currently are and where they came from.

    Additive bonuses: +5 location match, +5 remote/WFH, +5 salary fit,
    +5 agent-eligible, capped in total. Overall score capped at 100.

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
         recommendation}. Under a NON-default policy the dict also carries
        policy_hash, so two scores can be told apart when they are not
        comparable.
    """
    return score_job(
        job_skills, profile_skills, job_exp_str, profile_exp,
        job_location=job_location,
        profile_location=profile_location,
        job_work_mode=job_work_mode,
        job_salary=job_salary,
        profile_expected_ctc=profile_expected_ctc,
        experience_min=experience_min,
        experience_max=experience_max,
        is_agent_eligible=is_agent_eligible,
    ).to_dict()
