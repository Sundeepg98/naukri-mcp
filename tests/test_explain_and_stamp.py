"""`explain` is reachable from every scoring tool, and the stamp is named right.

TWO DEFECTS THIS FILE PINS.

1. `FitScore.explain()` returned the real arithmetic -- weights, the
   skills/experience components before bonuses, the bonus cap, the verdict
   band -- and NO tool in this package exposed it. The block existed and was
   unreachable.

2. The result stamp was called `policy_hash`, the same name as the FULL
   {scoring, candidate} fingerprint, while carrying the {scoring}-only one.
   One policy produced two different values under one field name, on the one
   field whose job is to say whether two scores are comparable.

WHAT MAKES THESE TESTS WORTH HAVING. A block that merely echoes constants
proves nothing, so the central test RE-DERIVES the score from the block's own
numbers: base.combined + bonuses.total, rounded, capped at 100, must equal
overall_score. If the block ever drifts from the arithmetic that actually ran,
that identity breaks.

And the OFF case is tested as hard as the ON case, because the whole premise
of the flag is that a default call stays byte-identical -- so the assertion is
that no `explain` key exists ANYWHERE in the result, at any depth.

All PURE: mocks for search / job detail / profile / apply. No network, no
browser. Mock patterns are the ones already used by test_one_engine.py,
test_smart_apply_deep.py and test_daily_brief_deep.py, not new ones.
"""

import json

import pytest
from unittest.mock import AsyncMock, patch

from tests.test_one_engine import _job_dict, _profile_dict
# Re-exported so pytest resolves it as a fixture in this module too. It binds a
# non-default policy through the REAL file -> loader -> policy chain.
from tests.test_one_engine import tilted_policy  # noqa: F401


# The fixture from test_one_engine scores 85 at the shipped policy:
#   skills 2 of 3 -> 66.7, experience 4y inside "2-5 years" -> 100
#   base 0.6*66.7 + 0.4*100 = 80.0, location bonus +5 -> 85
EXPECTED_SCORE = 85

# Every key the block must carry. Named explicitly so a silently shrunk
# explain() is a failure here and not a quiet loss of the thing being sold.
EXPLAIN_KEYS = {
    "weights", "base", "bonuses", "bonus_cap_applied", "score_ceiling_applied",
    "overall_score", "verdict_band", "skill_weighting", "scoring_hash",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_explain_paths(obj, path="$"):
    """Every path in a nested result at which an `explain` key appears.

    Recursive on purpose. "No explain key" has to mean no explain key at ANY
    depth -- a block that merely moved one level down is not absent, and a
    shallow check would call it absent.
    """
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}"
            if key == "explain":
                found.append(here)
            found.extend(find_explain_paths(value, here))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(find_explain_paths(item, f"{path}[{i}]"))
    return found


def assert_no_explain_anywhere(result, tool_name):
    paths = find_explain_paths(result)
    assert paths == [], (
        f"{tool_name} emitted an `explain` key with explain=False, at {paths}. "
        "Off must mean absent -- not None, not {}."
    )


def assert_block_is_real(block, tool_name):
    """The block reproduces the score it claims to explain.

    THIS is the test that makes the block worth shipping. Anything can echo a
    constant; only the real arithmetic re-derives the number.
    """
    assert isinstance(block, dict), f"{tool_name}: block is {type(block)}"
    missing = EXPLAIN_KEYS - set(block)
    assert not missing, f"{tool_name}: block is missing {sorted(missing)}"

    combined = block["base"]["combined"]
    bonus_total = block["bonuses"]["total"]
    derived = min(100, round(combined + bonus_total))
    assert derived == block["overall_score"], (
        f"{tool_name}: the block does not add up -- base.combined "
        f"{combined} + bonuses.total {bonus_total} -> {derived}, but the "
        f"block reports overall_score {block['overall_score']}"
    )

    # The weights are the ones the arithmetic used, not decoration.
    w = block["weights"]
    recomputed = block["base"]["skills"] * w["skills"] + \
        block["base"]["experience"] * w["experience"]
    assert abs(recomputed - combined) <= 0.1, (
        f"{tool_name}: weights {w} do not produce base.combined {combined} "
        f"from the components (got {recomputed})"
    )


def assert_stamped_not_policy(block, tool_name):
    """The block carries `scoring_hash` and NOT `policy_hash`.

    A result can only vouch for the arithmetic. `policy_hash` also covers the
    candidate block, which does not reach the result -- stamping it would
    claim more than the score knows.
    """
    assert block.get("scoring_hash"), f"{tool_name}: no scoring_hash on the block"
    assert "policy_hash" not in block, (
        f"{tool_name}: the block carries `policy_hash`. That is the FULL "
        "{scoring, candidate} fingerprint; a RESULT stamp is scoring-only."
    )


# ---------------------------------------------------------------------------
# 1. naukri_assess_fit -- one assessment, block under fit_assessment
# ---------------------------------------------------------------------------

def _assess_patches():
    return (
        patch("naukri_server.tools.jobs.naukri_get_job",
              new_callable=AsyncMock, return_value=_job_dict()),
        patch("naukri_server.tools.profile.get_cached_profile",
              new_callable=AsyncMock, return_value=_profile_dict()),
        patch("naukri_server.tools.jobs._fetch_match_score",
              new_callable=AsyncMock, return_value=None),
    )


async def _run_assess_fit(explain):
    from naukri_server.tools.smart_apply import naukri_assess_fit

    p1, p2, p3 = _assess_patches()
    with p1, p2, p3:
        return await naukri_assess_fit(job_id="J1", explain=explain)


class TestAssessFit:
    @pytest.mark.asyncio
    async def test_off_by_default_emits_no_explain_key(self):
        result = await _run_assess_fit(explain=False)
        assert result["status"] == "success", result
        assert result["fit_assessment"]["overall_score"] == EXPECTED_SCORE
        assert_no_explain_anywhere(result, "naukri_assess_fit")

    @pytest.mark.asyncio
    async def test_default_argument_is_off(self):
        """CONTROL. If the default flipped, the test above would still pass
        when called explicitly and the regression would ship."""
        from naukri_server.tools.smart_apply import naukri_assess_fit

        p1, p2, p3 = _assess_patches()
        with p1, p2, p3:
            result = await naukri_assess_fit(job_id="J1")
        assert_no_explain_anywhere(result, "naukri_assess_fit (no kwarg)")

    @pytest.mark.asyncio
    async def test_on_attaches_a_real_block(self):
        result = await _run_assess_fit(explain=True)
        assert result["status"] == "success", result
        block = result["fit_assessment"]["explain"]
        assert_block_is_real(block, "naukri_assess_fit")
        assert_stamped_not_policy(block, "naukri_assess_fit")
        assert block["overall_score"] == EXPECTED_SCORE


# ---------------------------------------------------------------------------
# 2. naukri_score_saved_jobs -- rows, block at row level
# ---------------------------------------------------------------------------

def _saved_jobs_patches():
    return (
        patch("naukri_server.tools.tracking._list_saved_jobs",
              new_callable=AsyncMock,
              return_value={"status": "success",
                            "saved_jobs": [{"job_id": "J1", "title": "Backend Engineer",
                                            "company": "Acme"}]}),
        patch("naukri_server.tools.profile.get_cached_profile",
              new_callable=AsyncMock, return_value=_profile_dict()),
        patch("naukri_server.tools.jobs.naukri_get_job",
              new_callable=AsyncMock, return_value=_job_dict()),
    )


async def _run_score_saved(explain):
    from naukri_server.tools.smart_apply import naukri_score_saved_jobs

    p1, p2, p3 = _saved_jobs_patches()
    with p1, p2, p3:
        return await naukri_score_saved_jobs(min_fit_score=0, timeout_seconds=10,
                                             explain=explain)


class TestScoreSavedJobs:
    @pytest.mark.asyncio
    async def test_off_by_default_emits_no_explain_key(self):
        result = await _run_score_saved(explain=False)
        assert result["status"] == "success", result
        assert result["scored_jobs"], result
        assert_no_explain_anywhere(result, "naukri_score_saved_jobs")

    @pytest.mark.asyncio
    async def test_on_attaches_a_real_block_per_row(self):
        result = await _run_score_saved(explain=True)
        assert result["scored_jobs"], result
        for row in result["scored_jobs"]:
            block = row["explain"]
            assert_block_is_real(block, "naukri_score_saved_jobs")
            assert_stamped_not_policy(block, "naukri_score_saved_jobs")
            # Exactly one block per row: lifted onto the row, not left
            # duplicated inside fit_details.
            assert "explain" not in row["fit_details"]
            assert block["overall_score"] == row["fit_score"]


# ---------------------------------------------------------------------------
# 3. naukri_apply_top_fits -- the block rides through to the applied rows
# ---------------------------------------------------------------------------

async def _run_apply_top_fits(explain):
    from naukri_server.tools.smart_apply import naukri_apply_top_fits

    p1, p2, p3 = _saved_jobs_patches()
    with p1, p2, p3, \
            patch("naukri_server.tools.apply._apply_single",
                  new_callable=AsyncMock, return_value={"status": "applied"}):
        return await naukri_apply_top_fits(min_fit_score=0, limit=5,
                                           timeout_seconds=10, explain=explain)


class TestApplyTopFits:
    @pytest.mark.asyncio
    async def test_off_by_default_emits_no_explain_key(self):
        result = await _run_apply_top_fits(explain=False)
        assert result["results"], result
        assert_no_explain_anywhere(result, "naukri_apply_top_fits")

    @pytest.mark.asyncio
    async def test_on_attaches_a_real_block_per_row(self):
        result = await _run_apply_top_fits(explain=True)
        assert result["results"], result
        for row in result["results"]:
            block = row["explain"]
            assert_block_is_real(block, "naukri_apply_top_fits")
            assert_stamped_not_policy(block, "naukri_apply_top_fits")
            assert block["overall_score"] == row["fit_score"]


# ---------------------------------------------------------------------------
# 4. naukri_auto_hunt -- typed FitScore path, never calls to_dict
# ---------------------------------------------------------------------------

async def _run_auto_hunt(explain):
    from naukri_server.tools.auto_hunt import naukri_auto_hunt

    with patch("naukri_server.tools.search.naukri_search_jobs",
               new_callable=AsyncMock,
               return_value={"status": "success", "jobs": [_job_dict()]}), \
         patch("naukri_server.tools.profile.get_cached_profile",
               new_callable=AsyncMock, return_value=_profile_dict()), \
         patch("naukri_server.database.get_applied_job_ids",
               new_callable=AsyncMock, return_value=set()):
        return await naukri_auto_hunt(keywords="python", min_fit_score=0,
                                      explain=explain)


class TestAutoHunt:
    @pytest.mark.asyncio
    async def test_off_by_default_emits_no_explain_key(self):
        result = await _run_auto_hunt(explain=False)
        assert result["status"] == "success", result
        assert result["ranked_jobs"], result
        assert_no_explain_anywhere(result, "naukri_auto_hunt")

    @pytest.mark.asyncio
    async def test_on_attaches_a_real_block_per_row(self):
        result = await _run_auto_hunt(explain=True)
        assert result["ranked_jobs"], result
        for row in result["ranked_jobs"]:
            block = row["explain"]
            assert_block_is_real(block, "naukri_auto_hunt")
            assert_stamped_not_policy(block, "naukri_auto_hunt")
            assert block["overall_score"] == row["fit_score"]


# ---------------------------------------------------------------------------
# 5. naukri_compare_jobs -- typed FitScore path, through the @mcp.tool wrapper
# ---------------------------------------------------------------------------

async def _run_compare(explain):
    from naukri_server.tools.jobs import _tool_compare_jobs

    with patch("naukri_server.tools.jobs.naukri_get_job",
               new_callable=AsyncMock,
               side_effect=[_job_dict("J1"), _job_dict("J2")]), \
         patch("naukri_server.tools.profile.get_cached_profile",
               new_callable=AsyncMock, return_value=_profile_dict()), \
         patch("naukri_server.database.get_applied_job_ids",
               new_callable=AsyncMock, return_value=set()):
        return await _tool_compare_jobs(job_ids=["J1", "J2"], timeout_seconds=10,
                                        explain=explain)


class TestCompareJobs:
    @pytest.mark.asyncio
    async def test_off_by_default_emits_no_explain_key(self):
        result = await _run_compare(explain=False)
        assert result["status"] == "success", result
        assert result["jobs"], result
        assert_no_explain_anywhere(result, "naukri_compare_jobs")

    @pytest.mark.asyncio
    async def test_on_attaches_a_real_block_per_row(self):
        result = await _run_compare(explain=True)
        assert result["jobs"], result
        for row in result["jobs"]:
            block = row["explain"]
            assert_block_is_real(block, "naukri_compare_jobs")
            assert_stamped_not_policy(block, "naukri_compare_jobs")
            assert block["overall_score"] == row["fit_score"]


# ---------------------------------------------------------------------------
# 6. naukri_daily_brief -- flat-dict path via compute_fit_score
# ---------------------------------------------------------------------------

async def _run_daily_brief(explain):
    """Same harness test_daily_brief_deep uses, parameterised by `explain`.

    compute_fit_score is deliberately NOT patched here -- the existing brief
    tests stub it with a constant, and a constant cannot exercise a block whose
    only value is that it holds the real arithmetic.
    """
    from tests.test_daily_brief_deep import (
        GATHER_PATCHES, _all_good_results, _patch_all_17,
    )

    jobs = [{"title": "Backend Engineer", "tags": ["Python", "Django", "Kubernetes"],
             "experience": "2-5 years"}]
    results_list = _all_good_results(recs_jobs=jobs)
    profile_data = {
        "status": "success",
        "key_skills": ["Python", "Django", "AWS"],
        "total_experience": "4 years 0 months",
    }
    assert len(GATHER_PATCHES) >= 18, "brief harness shape moved"

    with patch("naukri_server.tools.early_access._detect_new_roles",
               return_value=([], 0)), \
         patch("naukri_server.tools.profile.get_cached_profile",
               new=AsyncMock(return_value=profile_data)):
        active = []
        for target, mock in _patch_all_17(results_list):
            p = patch(target, new=mock)
            p.start()
            active.append(p)
        try:
            from naukri_server.tools.daily_brief import naukri_daily_brief
            return await naukri_daily_brief(explain=explain)
        finally:
            for p in active:
                p.stop()


class TestDailyBrief:
    @pytest.mark.asyncio
    async def test_off_by_default_emits_no_explain_key(self):
        result = await _run_daily_brief(explain=False)
        scored = [j for j in result["recommendations"]["jobs"] if "fit_score" in j]
        assert scored, "the brief scored nothing, so the flag was never exercised"
        assert_no_explain_anywhere(result, "naukri_daily_brief")

    @pytest.mark.asyncio
    async def test_on_attaches_a_real_block_per_scored_job(self):
        result = await _run_daily_brief(explain=True)
        scored = [j for j in result["recommendations"]["jobs"] if "fit_score" in j]
        assert scored, "the brief scored nothing, so the flag was never exercised"
        for job in scored:
            block = job["explain"]
            assert_block_is_real(block, "naukri_daily_brief")
            assert_stamped_not_policy(block, "naukri_daily_brief")
            assert block["overall_score"] == job["fit_score"]


# ---------------------------------------------------------------------------
# 7. The block is not a constant -- it tracks the policy that made it
# ---------------------------------------------------------------------------

class TestTheBlockTracksThePolicy:
    def test_the_arithmetic_check_CAN_fail(self):
        """CONTROL for assert_block_is_real. A checker that cannot fail
        certifies nothing, so here it is, failing on a tampered block."""
        from naukri_server.scoring import compute_fit_score, parse_skills
        from tests.test_one_engine import (
            JOB_SKILLS, PROFILE_SKILLS, JOB_EXPERIENCE, PROFILE_EXPERIENCE,
        )

        d = compute_fit_score(
            parse_skills(JOB_SKILLS), parse_skills(PROFILE_SKILLS),
            JOB_EXPERIENCE, PROFILE_EXPERIENCE,
            experience_min=2, experience_max=5, explain=True,
        )
        block = dict(d["explain"])
        assert_block_is_real(block, "untampered")

        block["overall_score"] = block["overall_score"] + 7
        with pytest.raises(AssertionError, match="does not add up"):
            assert_block_is_real(block, "tampered")

    def test_the_weights_in_the_block_move_with_the_policy(self, tilted_policy):
        """Under 0.9/0.1 the block must report 0.9/0.1 AND re-derive the tilted
        score. A block echoing the shipped 60/40 would pass a keys-only check
        and be wrong about every number that matters."""
        from naukri_server.scoring import compute_fit_score, parse_skills
        from tests.test_one_engine import (
            JOB_SKILLS, PROFILE_SKILLS, JOB_EXPERIENCE, PROFILE_EXPERIENCE,
        )

        d = compute_fit_score(
            parse_skills(JOB_SKILLS), parse_skills(PROFILE_SKILLS),
            JOB_EXPERIENCE, PROFILE_EXPERIENCE,
            experience_min=2, experience_max=5, explain=True,
        )
        block = d["explain"]
        assert block["weights"] == {"skills": 0.9, "experience": 0.1}
        assert_block_is_real(block, "tilted policy")
        assert_stamped_not_policy(block, "tilted policy")
        # And the stamp on the flat dict is the same one the block carries.
        assert d["scoring_hash"] == block["scoring_hash"]


# ---------------------------------------------------------------------------
# 8. The two hashes are different, and the agent gate uses the FULL one
# ---------------------------------------------------------------------------

class TestTheTwoHashesAreNotOneField:
    def test_scoring_hash_and_policy_hash_differ_for_one_policy(self):
        """The defect in one line: the same policy, two hashes. That is why
        they cannot share a field name."""
        from naukri_server import policy as naukri_policy

        snap = naukri_policy.snapshot()
        assert snap.policy_hash != snap.scoring_hash, (
            "policy_hash and scoring_hash are equal, so this test can no "
            "longer tell a mixed-up field from a correct one"
        )

    def test_policy_stamp_carries_both_named(self):
        from naukri_server import policy as naukri_policy

        stamp = naukri_policy.policy_stamp()
        assert set(stamp) == {"policy_rev", "policy_hash", "scoring_hash"}
        snap = naukri_policy.snapshot()
        assert stamp["policy_hash"] == snap.policy_hash
        assert stamp["scoring_hash"] == snap.scoring_hash

    def test_a_section_readout_carries_the_bridge_field(self):
        """The narrowed readout has to carry scoring_hash too, or matching a
        stored score against it needs a second, wider call."""
        from naukri_server import policy as naukri_policy

        out = naukri_policy.report(section="scoring")
        assert "scoring_hash" in out
        assert "policy_hash" in out
        assert out["scoring_hash"] == naukri_policy.snapshot().scoring_hash


class TestTheAgentGateStillUsesTheFullHash:
    """THE HOLE THIS GUARDS. `candidate.skills` moves every score without
    touching the arithmetic, so it does NOT move `scoring_hash`. If the agent's
    gate were "fixed" to compare the scoring hash, inflating the skills list
    would sail past the forced approval cycle in silence."""

    def test_candidate_skills_move_the_gate_hash(self, tmp_path, monkeypatch):
        from naukri_server import agent
        import naukri_server.policy as naukri_policy

        cfg = tmp_path / "jobhunt.json"

        def _write(skills):
            cfg.write_text(json.dumps({
                "config_version": 1, "revision": 1,
                "candidate": {"skills": skills},
            }), encoding="utf-8")
            naukri_policy.invalidate()

        monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
        try:
            _write(["python", "django"])
            before = agent.current_policy_hash()
            before_scoring = naukri_policy.snapshot().scoring_hash

            _write(["python", "django", "kubernetes", "aws", "react", "go"])
            after = agent.current_policy_hash()
            after_scoring = naukri_policy.snapshot().scoring_hash

            assert before is not None and after is not None
            assert after != before, (
                "inflating candidate.skills did not move the gate hash -- the "
                "forced approval cycle would not fire, which is the hole"
            )
            # And the reason the gate cannot use the scoring hash:
            assert after_scoring == before_scoring, (
                "candidate.skills moved the SCORING hash; if that ever becomes "
                "true the gate could narrow, but until then it must not"
            )
        finally:
            naukri_policy.invalidate()


class TestTheOldStateKeyIsStillHonoured:
    """His live box has a state file written under the OLD key. Reading it as
    'never seen' would burn a spurious approval cycle on the next run."""

    def test_last_scoring_hash_is_read_when_last_policy_hash_is_absent(
            self, tmp_path, monkeypatch):
        from naukri_server import agent

        state = tmp_path / "agent_policy_state.json"
        monkeypatch.setattr(agent, "POLICY_STATE_PATH", state)
        state.write_text(
            json.dumps({"last_scoring_hash": agent.current_policy_hash()}),
            encoding="utf-8",
        )

        mode, reason = agent._effective_mode({"mode": "auto"}, "c1")
        assert mode == "auto", (
            "the old state key was ignored, so an existing state file reads as "
            f"'never seen' and forces a needless approval cycle: {reason}"
        )
        assert reason is None

    def test_the_new_key_is_read_too(self, tmp_path, monkeypatch):
        from naukri_server import agent

        state = tmp_path / "agent_policy_state.json"
        monkeypatch.setattr(agent, "POLICY_STATE_PATH", state)
        state.write_text(
            json.dumps({"last_policy_hash": agent.current_policy_hash()}),
            encoding="utf-8",
        )

        mode, reason = agent._effective_mode({"mode": "auto"}, "c1")
        assert mode == "auto"
        assert reason is None

    def test_the_new_key_WINS_over_a_stale_old_one(self, tmp_path, monkeypatch):
        """CONTROL. If the fallback were an unconditional read of the old key,
        a stale leftover would force an approval cycle forever."""
        from naukri_server import agent

        state = tmp_path / "agent_policy_state.json"
        monkeypatch.setattr(agent, "POLICY_STATE_PATH", state)
        state.write_text(
            json.dumps({"last_policy_hash": agent.current_policy_hash(),
                        "last_scoring_hash": "stale-value-from-an-old-run"}),
            encoding="utf-8",
        )

        mode, reason = agent._effective_mode({"mode": "auto"}, "c1")
        assert mode == "auto", f"the stale old key won over the new one: {reason}"

    def test_neither_key_present_still_forces_the_cycle(self, tmp_path, monkeypatch):
        """CONTROL for the two above. The fallback must not turn a
        never-seen state into a free pass."""
        from naukri_server import agent

        state = tmp_path / "agent_policy_state.json"
        monkeypatch.setattr(agent, "POLICY_STATE_PATH", state)
        state.write_text(json.dumps({}), encoding="utf-8")

        mode, reason = agent._effective_mode({"mode": "auto"}, "c1")
        assert mode == "approval"
        assert reason
