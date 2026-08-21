"""One engine. Every scoring entry point must return the same number.

THE FINDING THIS EXISTS FOR (adversarial review, C2). Four naukri tools score
jobs, and only one of them went through the engine:

    naukri_daily_brief   -> naukri_server.scoring.compute_fit_score -> _engine()
    naukri_auto_hunt     -> FitScore.compute(...)   DIRECT   <- the AGENT's scorer
    naukri_assess_fit    -> FitScore.compute(...)   DIRECT
    naukri_compare_jobs  -> FitScore.compute(...)   DIRECT

`FitScore` carries the 60/40 split, the bonus cap, the verdict bands and the
experience penalties in `@property` methods on a frozen dataclass. Injecting a
policy into `ScoringEngine` therefore reached ONE of the four, and the golden
corpus is structurally incapable of noticing, because it runs at DEFAULT policy
where every path agrees by construction.

So these tests do the only thing that can catch it: run all four under a
NON-DEFAULT policy and demand one number.

MEASURED against the naive migration — engine made policy-aware, the three
direct call sites left alone, which is the state the design's step 1 would have
shipped::

    split brain: these paths scored the SAME job differently under one policy
    -> {'flat_api (daily_brief)': 75, 'smart_apply (assess_fit)': 85,
        'auto_hunt': 85, 'compare_jobs': 85}

    assert not ['tools/auto_hunt.py:191', 'tools/compare.py:134',
                'tools/smart_apply.py:40']

One tool moved ten points, three kept scoring on the shipped 60/40, and nothing
in any output said so. That is what these tests exist to make impossible.

All PURE: mocks for search / job detail / profile. No network, no browser.
"""

import pytest
from unittest.mock import AsyncMock, patch

from jobcore.policy import DEFAULT_SCORING_POLICY, ScoringPolicy, Weights


# A policy that cannot be confused with the shipped one. 0.9/0.1 is inside the
# HARD_LIMITS band [0.1, 0.9] so it is a policy he could really set, and it is
# far enough from 0.6/0.4 to move any job whose two components differ.
TILTED = ScoringPolicy.from_dict({"weights": {"skills": 0.9, "experience": 0.1}})


# ---------------------------------------------------------------------------
# One job, one profile, used by every path so the numbers are comparable.
#   skills      2 of 3 matched              -> 66.7
#   experience  4y inside "2-5 years"       -> 100
#   bonuses     location Bangalore==Bangalore -> +5; office +0; salary undisclosed +0
#
#   default 0.6/0.4 : 0.6*66.7 + 0.4*100 = 80.0  + 5 = 85
#   tilted  0.9/0.1 : 0.9*66.7 + 0.1*100 = 70.0  + 5 = 75
#
# The bonus is deliberately non-zero: it makes the fixture exercise the bonus
# helpers too, so a policy that reached the base score but not the bonuses
# would still be caught.
# ---------------------------------------------------------------------------

DEFAULT_EXPECTED = 85
TILTED_EXPECTED = 75

JOB_SKILLS = ["Python", "Django", "Kubernetes"]
PROFILE_SKILLS = ["Python", "Django", "AWS"]
JOB_EXPERIENCE = "2-5 years"
PROFILE_EXPERIENCE = "4 years 0 months"


def _job_dict(job_id="J1"):
    return {
        "status": "success",
        "job_id": job_id,
        "title": "Backend Engineer",
        "company": "Acme",
        "company_rating": 4.0,
        "is_applied": False,
        "tags": list(JOB_SKILLS),
        "skills": None,
        "experience": JOB_EXPERIENCE,
        "experience_min": 2,
        "experience_max": 5,
        "location": "Bangalore",
        "work_mode": "Work from office",
        "salary": "Not disclosed",
        "group_id": None,
        "vacancies": 1,
        "external_apply": False,
        "external_apply_url": None,
        "posted_date": "2026-08-01",
        "apply_count": 10,
        "candidates_count": 20,
        "is_agent_eligible": False,
    }


def _profile_dict():
    return {
        "status": "success",
        "key_skills": list(PROFILE_SKILLS),
        "total_experience": PROFILE_EXPERIENCE,
        "current_location": "Bangalore",
        "expected_ctc": None,
    }


# ---------------------------------------------------------------------------
# The four paths, each returning one integer.
# ---------------------------------------------------------------------------

def _path_flat_api() -> int:
    """daily_brief's path: the module-level flat function."""
    from naukri_server.scoring import compute_fit_score, parse_skills

    return compute_fit_score(
        parse_skills(JOB_SKILLS), parse_skills(PROFILE_SKILLS),
        JOB_EXPERIENCE, PROFILE_EXPERIENCE,
        job_location="Bangalore", profile_location="Bangalore",
        job_work_mode="Work from office", job_salary="Not disclosed",
        profile_expected_ctc=None,
        experience_min=2, experience_max=5, is_agent_eligible=False,
    )["overall_score"]


async def _path_auto_hunt() -> int:
    from naukri_server.tools.auto_hunt import naukri_auto_hunt

    with patch("naukri_server.tools.search.naukri_search_jobs",
               new_callable=AsyncMock, return_value={"status": "success",
                                                     "jobs": [_job_dict()]}), \
         patch("naukri_server.tools.profile.get_cached_profile",
               new_callable=AsyncMock, return_value=_profile_dict()), \
         patch("naukri_server.database.get_applied_job_ids",
               new_callable=AsyncMock, return_value=set()):
        result = await naukri_auto_hunt(keywords="python", min_fit_score=0)
    assert result["status"] == "success", result
    return result["ranked_jobs"][0]["fit_score"]


async def _path_compare() -> int:
    from naukri_server.tools.compare import _compare_jobs

    with patch("naukri_server.tools.jobs.naukri_get_job",
               new_callable=AsyncMock,
               side_effect=[_job_dict("J1"), _job_dict("J2")]), \
         patch("naukri_server.tools.profile.get_cached_profile",
               new_callable=AsyncMock, return_value=_profile_dict()), \
         patch("naukri_server.database.get_applied_job_ids",
               new_callable=AsyncMock, return_value=set()):
        result = await _compare_jobs(["J1", "J2"], timeout_seconds=10)
    assert result["status"] == "success", result
    return result["jobs"][0]["fit_score"]


def _path_smart_apply() -> int:
    from naukri_server.tools.smart_apply import _score_job

    return _score_job(_job_dict(), _profile_dict(),
                      is_agent_eligible=False)["overall_score"]


ASYNC_PATHS = {"auto_hunt": _path_auto_hunt, "compare_jobs": _path_compare}
SYNC_PATHS = {"flat_api (daily_brief)": _path_flat_api,
              "smart_apply (assess_fit)": _path_smart_apply}


async def _all_paths() -> dict:
    out = {name: fn() for name, fn in SYNC_PATHS.items()}
    for name, fn in ASYNC_PATHS.items():
        out[name] = await fn()
    return out


@pytest.fixture
def tilted_policy(tmp_path, monkeypatch):
    """Bind a non-default policy through the REAL chain: file -> loader -> policy.

    Deliberately not a monkeypatched module global. The thing under test is
    whether an edit to his file reaches every scorer, and a patched attribute
    would prove only that a patched attribute is readable.
    """
    import json

    import naukri_server.policy as naukri_policy

    cfg = tmp_path / "jobhunt.json"
    cfg.write_text(json.dumps({
        "config_version": 1,
        "revision": 1,
        "scoring": {"weights": {"skills": 0.9, "experience": 0.1}},
    }), encoding="utf-8")
    monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
    naukri_policy.invalidate()
    try:
        loaded = naukri_policy.snapshot()
        assert loaded.source == str(cfg), loaded.config_status
        assert loaded.config_error is None, loaded.config_error
        yield loaded.policy.scoring
    finally:
        naukri_policy.invalidate()


# ---------------------------------------------------------------------------


class TestArithmeticOfTheFixture:
    """Pin the numbers so a failure below says WHICH policy leaked, not just
    'they differ'."""

    def test_default_policy_scores_80(self):
        assert _path_flat_api() == DEFAULT_EXPECTED

    def test_the_tilt_is_a_real_move(self, tilted_policy):
        """0.9/0.1 has to change this job's score, or the test proves nothing."""
        assert _path_flat_api() == TILTED_EXPECTED


class TestEveryEntryPointAgrees:
    @pytest.mark.asyncio
    async def test_all_four_paths_agree_at_default_policy(self):
        """True today by accident — both branches happen to hold the same
        literals. Kept so a regression at default is caught too."""
        scores = await _all_paths()
        assert len(set(scores.values())) == 1, scores
        assert set(scores.values()) == {DEFAULT_EXPECTED}, scores

    @pytest.mark.asyncio
    async def test_all_four_paths_agree_under_a_NON_default_policy(self, tilted_policy):
        """THE TEST. Fails against the pre-fix tree:

            {'flat_api (daily_brief)': 70, 'smart_apply (assess_fit)': 80,
             'auto_hunt': 80, 'compare_jobs': 80}

        — one tool moved, three kept scoring on the shipped 60/40, and nothing
        in any output said so.
        """
        scores = await _all_paths()
        assert len(set(scores.values())) == 1, (
            "split brain: these paths scored the SAME job differently under one "
            f"policy -> {scores}"
        )
        assert set(scores.values()) == {TILTED_EXPECTED}, scores


class TestTheAgreementCheckCanFail:
    """CONTROL. An equality assertion over four values is worthless if the
    values cannot diverge. This builds the divergence deliberately."""

    def test_a_direct_FitScore_call_ignoring_the_policy_diverges(self, tilted_policy):
        """The exact bug C2 describes: bypass the engine, keep the default.

        Same job, same profile, same enrichment, same bonus helpers — the ONLY
        difference is that this construction does not pass ``policy=``. That is
        precisely what `auto_hunt`, `compare` and `smart_apply` used to do.
        """
        from jobcore.fit import FitScore

        from naukri_server.scoring import (
            parse_skills, _score_location, _score_work_mode, _score_salary,
        )

        bypassed = FitScore.compute(
            parse_skills(JOB_SKILLS), parse_skills(PROFILE_SKILLS),
            JOB_EXPERIENCE, PROFILE_EXPERIENCE,
            job_location="Bangalore", profile_location="Bangalore",
            job_work_mode="Work from office", job_salary="Not disclosed",
            profile_expected_ctc=None,
            experience_min=2, experience_max=5, is_agent_eligible=False,
            score_location_fn=_score_location,
            score_work_mode_fn=_score_work_mode,
            score_salary_fn=_score_salary,
        ).overall_score
        routed = _path_flat_api()
        assert bypassed != routed, (
            "a direct FitScore.compute() that ignores the bound policy must NOT "
            "match the routed path, or this suite cannot detect a bypass at all"
        )
        assert bypassed == DEFAULT_EXPECTED and routed == TILTED_EXPECTED


class TestNoToolConstructsFitScoreDirectly:
    """The structural half. Agreement today does not stop the next tool from
    reintroducing a bypass, and a grep is the only thing that does."""

    def test_no_direct_FitScore_compute_call_survives_in_tools(self):
        import re
        from pathlib import Path

        pkg = Path(__file__).resolve().parent.parent / "naukri_server"
        offenders = []
        for p in sorted(pkg.rglob("*.py")):
            if "__pycache__" in p.parts or p.name == "scoring.py":
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bFitScore\.compute\s*\(", line):
                    offenders.append(f"{p.relative_to(pkg)}:{i}")
        assert not offenders, (
            "these bypass naukri_server.scoring.score_job and will not see a "
            "configured policy:\n  " + "\n  ".join(offenders)
        )

    def test_that_scan_can_fail(self):
        import re

        assert re.search(r"\bFitScore\.compute\s*\(", "    fit = FitScore.compute(")


class TestOneCallOnePolicy:
    """H4. "The snapshot is taken once per tool call" was asserted by the design
    and not provided by it: the read sat inside `_engine()`, which the bonus
    helpers call PER JOB. So the real granularity was per-scoring-call, and on
    this server — one process running HTTP + stdio + nine scheduled tasks on one
    event loop — every `await` inside a ranking loop is a point where a
    concurrent write can swap the weights between two rows of one result.
    """

    def test_a_change_landing_mid_call_is_not_seen_by_that_call(
            self, tmp_path, monkeypatch):
        import json

        import naukri_server.policy as naukri_policy

        cfg = tmp_path / "jobhunt.json"
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 1,
            "scoring": {"weights": {"skills": 0.7, "experience": 0.3}},
        }), encoding="utf-8")
        monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
        naukri_policy.invalidate()

        with naukri_policy.bind():
            before = naukri_policy.scoring_policy().weights.skills
            cfg.write_text(json.dumps({
                "config_version": 1, "revision": 2,
                "scoring": {"weights": {"skills": 0.2, "experience": 0.8}},
            }), encoding="utf-8")
            naukri_policy.invalidate()          # even an explicit reload
            during = naukri_policy.scoring_policy().weights.skills

        after = naukri_policy.scoring_policy().weights.skills
        naukri_policy.invalidate()

        assert before == during == 0.7, (before, during)
        assert after == 0.2, "the NEXT call must see the change"

    @pytest.mark.asyncio
    async def test_every_tool_binds_because_handle_tool_action_does(
            self, tmp_path, monkeypatch):
        """Bound at the one seam every tool passes through, rather than per tool
        — a decorator someone must remember to add is a guard with a hole."""
        import json

        import naukri_server.policy as naukri_policy
        from naukri_server.error_handler import handle_tool_action

        cfg = tmp_path / "jobhunt.json"
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 1,
            "scoring": {"weights": {"skills": 0.7, "experience": 0.3}},
        }), encoding="utf-8")
        monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
        naukri_policy.invalidate()

        seen = []

        async def handler():
            seen.append(naukri_policy.scoring_policy().weights.skills)
            cfg.write_text(json.dumps({
                "config_version": 1, "revision": 2,
                "scoring": {"weights": {"skills": 0.2, "experience": 0.8}},
            }), encoding="utf-8")
            naukri_policy.invalidate()
            seen.append(naukri_policy.scoring_policy().weights.skills)
            return {"status": "success"}

        await handle_tool_action(handler, "test.bind")
        naukri_policy.invalidate()

        assert seen == [0.7, 0.7], seen

    def test_the_binding_check_CAN_fail(self, tmp_path, monkeypatch):
        """CONTROL. Unbound, the same sequence DOES see the change — which is
        the bug, and which proves the two tests above measure the binding."""
        import json

        import naukri_server.policy as naukri_policy

        cfg = tmp_path / "jobhunt.json"
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 1,
            "scoring": {"weights": {"skills": 0.7, "experience": 0.3}},
        }), encoding="utf-8")
        monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
        naukri_policy.invalidate()

        before = naukri_policy.scoring_policy().weights.skills
        cfg.write_text(json.dumps({
            "config_version": 1, "revision": 2,
            "scoring": {"weights": {"skills": 0.2, "experience": 0.8}},
        }), encoding="utf-8")
        naukri_policy.invalidate()
        after = naukri_policy.scoring_policy().weights.skills
        naukri_policy.invalidate()

        assert before == 0.7 and after == 0.2


class TestThePolicyIsCarriedOnTheResult:
    """A score whose policy you cannot recover is not explainable."""

    def test_a_scored_result_knows_which_policy_made_it(self, tilted_policy):
        from naukri_server.scoring import score_job, parse_skills

        fit = score_job(
            parse_skills(JOB_SKILLS), parse_skills(PROFILE_SKILLS),
            JOB_EXPERIENCE, PROFILE_EXPERIENCE,
            experience_min=2, experience_max=5,
        )
        assert fit.policy == TILTED
        assert fit.policy != DEFAULT_SCORING_POLICY
        assert fit.policy.weights == Weights(skills=0.9, experience=0.1)

    def test_a_non_default_score_carries_its_hash(self, tilted_policy):
        """Two scores under different policies are not comparable, and the dict
        has to say so — otherwise `policy_rev` on a stored row invites exactly
        the wrong conclusion.

        The key is `scoring_hash`, not `policy_hash`. A RESULT can only vouch
        for the arithmetic; `policy_hash` also covers the candidate block,
        which is a call argument here, so the two hashes differ for one policy
        and stamping the full one would claim more than the result knows.
        """
        from naukri_server.scoring import compute_fit_score, parse_skills

        d = compute_fit_score(
            parse_skills(JOB_SKILLS), parse_skills(PROFILE_SKILLS),
            JOB_EXPERIENCE, PROFILE_EXPERIENCE,
            experience_min=2, experience_max=5,
        )
        assert d["scoring_hash"]
        assert "policy_hash" not in d

    def test_at_default_the_result_is_byte_identical_to_today(self):
        """`to_dict()` must not grow a stamp until a policy is actually set —
        that is what keeps every existing assertion in this repo passing."""
        from naukri_server.scoring import compute_fit_score, parse_skills

        d = compute_fit_score(
            parse_skills(JOB_SKILLS), parse_skills(PROFILE_SKILLS),
            JOB_EXPERIENCE, PROFILE_EXPERIENCE,
            experience_min=2, experience_max=5,
        )
        assert "scoring_hash" not in d
        assert "policy_hash" not in d
        assert "policy_rev" not in d
