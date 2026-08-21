"""THE INVARIANT, run as an attack rather than asserted as a property.

    No sequence of config writes, from any server, may grant autonomous apply
    authority.

naukri is the only server in the family that can submit an application with no
human in the loop, so this is where the invariant is load-bearing. The
adversarial review traced a live five-call path through the config file:

    1. servers.naukri.agent.enabled     : true      -> agent armed
    2. servers.naukri.agent.mode        : "auto"    -> applies with no approval
    3. servers.naukri.agent.min_fit_score: 0        -> every job is a candidate
    4. servers.naukri.agent.blocklist.enabled: false-> blocked companies eligible
    5. servers.naukri.agent.searches    : [...]     -> arbitrary queries

ending at fifteen real applications a day on his live account, authorised
entirely through keys the first design declared freely writable. A SIXTH path
ran through a sibling server and never touched the agent block at all: writing
every canonical skill into `candidate.skills` drives |matched|/|job_skills| to
100 for every job in existence, which drives this agent's selector.

These tests RUN all six, through both surfaces that can reach the file:
`apply_patch` (the tool path) and a hand-edited document (the text-editor path,
which is the workflow the whole design exists to serve and which takes no lock
and honours no compare-and-swap).

Each guard is paired with a CONTROL that shows it CAN fail. Six bugs in this
codebase this week were checks that could not fail; an assertion that nothing
bad happened is worthless unless something bad is reachable.

MEASURED, twice, against builds where the guards are absent:

1. Tier table rebuilt exactly as the ORIGINAL design specified it (the five
   agent keys as tier A, no deny-by-default agent subtree, no tier-C leaf
   name), then the same hand-edited file loaded::

       SHIPPED     enabled=False mode=dry_run min_fit=70 blocklist=True
                   searches=0 per_search_limit=20   refusals=6   ARMED: False
       PERMISSIVE  enabled=True  mode=auto    min_fit=0  blocklist=False
                   searches=1 per_search_limit=200  refusals=0   ARMED: True

   One saved file, in Notepad, arms the agent completely.

2. naukri's own two guards reverted (no MIN_AGENT_FIT_FLOOR enforcement, no
   approval-cycle downgrade, shallow config.update):

       11 failed, 20 passed

All PURE: temp config files, no network, no browser, no live account.
"""

import json

import pytest

from jobcore.config import MIN_AGENT_FIT_FLOOR


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _write_config(path, body: dict):
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """An empty-but-present config file, bound as THE config for this test."""
    import naukri_server.policy as naukri_policy

    cfg = tmp_path / "jobhunt.json"
    _write_config(cfg, {"config_version": 1, "revision": 1})
    monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
    naukri_policy.invalidate()
    try:
        yield cfg
    finally:
        naukri_policy.invalidate()


def _reload(cfg):
    import naukri_server.policy as naukri_policy

    naukri_policy.invalidate()
    return naukri_policy.snapshot()


ESCALATION_STEPS = [
    ("enabled", {"servers": {"naukri": {"agent": {"enabled": True}}}}),
    ("mode", {"servers": {"naukri": {"agent": {"mode": "auto"}}}}),
    ("min_fit_score", {"servers": {"naukri": {"agent": {"min_fit_score": 0}}}}),
    ("blocklist.enabled",
     {"servers": {"naukri": {"agent": {"blocklist": {"enabled": False}}}}}),
    ("searches", {"servers": {"naukri": {"agent": {"searches": [
        {"name": "anything", "keywords": "anything", "min_fit_score": 0,
         "enabled": True}]}}}}),
]

FULL_ESCALATION = {
    "config_version": 1,
    "revision": 1,
    "servers": {"naukri": {"agent": {
        "enabled": True,
        "mode": "auto",
        "min_fit_score": 0,
        "blocklist": {"enabled": False, "companies": [], "title_keywords": []},
        "searches": [{"name": "anything", "keywords": "anything",
                      "min_fit_score": 0, "enabled": True}],
        "per_search_limit": 200,
    }}},
}


# ---------------------------------------------------------------------------
# 1. The tool path — every step refused BY NAME
# ---------------------------------------------------------------------------

class TestTheWritePathRefusesEveryStep:
    @pytest.mark.parametrize("label,patch", ESCALATION_STEPS,
                             ids=[s[0] for s in ESCALATION_STEPS])
    def test_step_is_refused(self, config_file, label, patch):
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(patch, actor="test")
        assert result["status"] == "refused", (label, result)
        assert result.get("refusals"), result
        blob = " ".join(result["refusals"]).lower()
        assert "tier c" in blob or "not loadable" in blob or "refus" in blob, result

    def test_confirm_widen_does_not_buy_it(self, config_file):
        """Tier C is not a ratchet with extra friction. There is no flag."""
        import naukri_server.policy as naukri_policy

        for _, patch in ESCALATION_STEPS:
            result = naukri_policy.apply_patch(patch, actor="test",
                                               confirm_widen=True)
            assert result["status"] == "refused", (patch, result)

    def test_a_sibling_server_cannot_write_naukris_section(self, config_file):
        """Section scoping, from the other side of the family."""
        from jobcore import config as jobcore_config

        result = jobcore_config.apply_patch(
            {"servers": {"naukri": {"display_min_score": 10}}},
            path=config_file, actor="uplers",
            allowed_sections=("candidate", "scoring", "servers.uplers"),
        )
        assert result["status"] == "refused", result

    def test_the_write_path_CAN_succeed(self, config_file):
        """CONTROL. If apply_patch refused everything the tests above would pass
        while proving nothing about tiers."""
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(
            {"servers": {"naukri": {"display_min_score": 75}}}, actor="test")
        assert result["status"] == "ok", result
        assert _reload(config_file).policy.server("naukri")["display_min_score"] == 75


# ---------------------------------------------------------------------------
# 2. The text-editor path — the file is not the authority
# ---------------------------------------------------------------------------
#
# This is the half a write-path guard cannot cover. Notepad calls no function.

class TestAHandEditedFileCannotArmTheAgent:
    def test_the_whole_escalation_in_one_saved_file_is_not_loaded(self, config_file):
        _write_config(config_file, FULL_ESCALATION)
        loaded = _reload(config_file)

        section = loaded.policy.server("naukri").get("agent", {})
        assert section.get("enabled") is False, section
        assert section.get("mode") == "dry_run", section
        assert section.get("min_fit_score") == 70, section
        assert section.get("blocklist", {}).get("enabled") is True, section
        assert not section.get("searches"), section

    def test_the_refusal_is_loud_and_names_the_keys(self, config_file):
        """Silently ignoring it would be its own bug: he would edit, save, see
        no change, and have nothing to read."""
        _write_config(config_file, FULL_ESCALATION)
        loaded = _reload(config_file)

        refused = " ".join(loaded.tier_c_refusals)
        for name in ("enabled", "mode", "min_fit_score", "blocklist", "searches"):
            assert name in refused, (name, loaded.tier_c_refusals)

    def test_the_hand_edit_check_CAN_fail(self, config_file):
        """CONTROL. A tier-A key in the same file DOES take effect, so the
        assertions above are about the tier, not about the file being ignored."""
        body = json.loads(json.dumps(FULL_ESCALATION))
        body["servers"]["naukri"]["display_min_score"] = 33
        body["scoring"] = {"weights": {"skills": 0.8, "experience": 0.2}}
        _write_config(config_file, body)
        loaded = _reload(config_file)

        assert loaded.policy.server("naukri")["display_min_score"] == 33
        assert loaded.policy.scoring.weights.skills == 0.8

    def test_the_agent_reads_its_own_file_not_this_one(self, config_file):
        """naukri's agent config lives in agent_config.json, and `load_agent_config`
        must not consult the shared file at any point."""
        import inspect

        from naukri_server import agent

        src = inspect.getsource(agent.load_agent_config)
        assert "jobhunt" not in src.lower()
        assert "jobcore" not in src.lower()
        assert agent.CONFIG_PATH.name == "agent_config.json"


# ---------------------------------------------------------------------------
# 3. The sibling path — candidate.skills, written from another server
# ---------------------------------------------------------------------------
#
# `candidate.skills` and `scoring` CANNOT be tier C: they are the feature he
# asked for by name. They are bounded instead, and the boundedness is what these
# tests measure.

EVERY_CANONICAL = None  # filled at import from the live taxonomy


def _every_canonical_skill() -> list:
    global EVERY_CANONICAL
    if EVERY_CANONICAL is None:
        from naukri_server.domain.skill_taxonomy import SKILL_ALIASES

        EVERY_CANONICAL = sorted(SKILL_ALIASES)
    return EVERY_CANONICAL


class TestTheSiblingSkillsPath:
    def test_the_attack_is_worth_defending_against(self):
        """CONTROL, and the reason this whole class exists.

        SkillMatch.score is |matched| / |job_skills|. A profile holding every
        canonical skill therefore scores 100 on every job whose requirements
        the taxonomy recognises — which is what a real recruiter keyword list
        normalises to — and that clears any selector, from any server's write.
        """
        from naukri_server.scoring import parse_skills, score_job

        canonical = _every_canonical_skill()
        everything = parse_skills(canonical)
        assert len(everything) >= 80, len(everything)

        jobs = [["python"], ["java", "spring"], canonical[:5], canonical[-7:],
                canonical]
        for job in jobs:
            job_skills = parse_skills(job)
            fit = score_job(job_skills, everything, "2-5 years", 4)
            assert fit.skill_match.score == 100.0, (job, fit.skill_match.score)
            assert fit.overall_score >= 100 - 0, fit.overall_score

    def test_the_attack_is_bounded_by_what_the_taxonomy_KNOWS(self):
        """And the honest limit of that claim, so nobody over-reads it.

        A job asking for skills outside the 88-name table is not scored 100 by
        the dump — the unmatched names stay in the denominator. The attack is
        devastating for recognised stacks and merely strong elsewhere, which is
        still far past any selector this agent uses.
        """
        from naukri_server.scoring import parse_skills, score_job

        everything = parse_skills(_every_canonical_skill())
        fit = score_job(parse_skills(["cobol", "fortran", "rust"]), everything,
                        "2-5 years", 4)
        assert 0 < fit.skill_match.score < 100

    def test_writing_every_canonical_skill_is_refused(self, config_file):
        """From uplers' allowed section, which is the point: `candidate` is
        shared, so section scoping does not bound this."""
        from jobcore import config as jobcore_config

        result = jobcore_config.apply_patch(
            {"candidate": {"skills": _every_canonical_skill()}},
            path=config_file, actor="uplers",
            allowed_sections=("candidate", "scoring", "servers.uplers"),
        )
        assert result["status"] == "refused", result
        assert any("skills" in r for r in result["refusals"]), result

    def test_a_hand_edited_skill_dump_is_refused_too(self, config_file):
        _write_config(config_file, {
            "config_version": 1, "revision": 1,
            "candidate": {"skills": _every_canonical_skill()},
        })
        loaded = _reload(config_file)
        assert loaded.config_error or len(loaded.policy.candidate.skills) == 0, (
            f"{len(loaded.policy.candidate.skills)} skills loaded"
        )

    def test_a_reasonable_skill_list_still_works(self, config_file):
        """CONTROL. The cap must bound the attack without killing the feature.

        Adding skills raises every score, so it is the loosening direction and
        needs confirm_widen — that is friction, not a ban. He gets to describe
        himself; he does not get to describe himself as everyone.
        """
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(
            {"candidate": {"skills": ["node.js", "typescript", "postgresql"]}},
            actor="test", confirm_widen=True)
        assert result["status"] == "ok", result
        assert set(_reload(config_file).policy.candidate.skills) == {
            "node.js", "typescript", "postgresql"}

    def test_confirm_widen_does_NOT_get_the_dump_through(self, config_file):
        """The sharpest form of the test: the same flag that legitimises three
        skills must not legitimise eighty-eight."""
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(
            {"candidate": {"skills": _every_canonical_skill()}},
            actor="test", confirm_widen=True)
        assert result["status"] == "refused", result
        assert any("skills" in r for r in result["refusals"]), result


# ---------------------------------------------------------------------------
# 4. The Python floor — the second, independent layer
# ---------------------------------------------------------------------------

class TestTheFloorTheFileCannotReach:
    def test_the_floor_exists_and_is_sane(self):
        from naukri_server.agent import _min_agent_fit_floor

        assert _min_agent_fit_floor() == MIN_AGENT_FIT_FLOOR == 60

    @pytest.mark.parametrize("configured", [0, 1, 59])
    @pytest.mark.asyncio
    async def test_a_sub_floor_threshold_never_enqueues(self, configured):
        """Even reaching agent_config.json directly — the surface the config
        file cannot touch but a tool can — the selector holds."""
        from unittest.mock import AsyncMock, patch

        from naukri_server.agent import _decide

        hunted = {"status": "success", "jobs_found": 1, "jobs_matched": 1,
                  "ranked_jobs": [{"job_id": "J1", "company": "Acme",
                                   "title": "Dev", "fit_score": 10}]}
        config = {
            "min_fit_score": configured,
            "mode": "auto",
            "searches": [{"name": "s", "keywords": "k", "min_fit_score": configured,
                          "enabled": True}],
            "blocklist": {"companies": [], "title_keywords": [], "enabled": True},
            "max_daily_applications": 15,
        }
        observe = {"cycle_id": "c1", "config": config, "applied_ids": set(),
                   "daily_applied": 0, "daily_remaining": 15}

        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=hunted) as hunt, \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock):
            result = await _decide(observe)

        assert result["candidates"] == [], result["candidates"]
        # …and the search itself was asked for the floor, not for 0.
        assert hunt.await_args.kwargs["min_fit_score"] >= MIN_AGENT_FIT_FLOOR

    @pytest.mark.asyncio
    async def test_the_floor_check_CAN_fail(self):
        """CONTROL. The same call with a job ABOVE the floor must produce a
        candidate — otherwise the test above passes because _decide never
        enqueues anything."""
        from unittest.mock import AsyncMock, patch

        from naukri_server.agent import _decide

        hunted = {"status": "success", "jobs_found": 1, "jobs_matched": 1,
                  "ranked_jobs": [{"job_id": "J1", "company": "Acme",
                                   "title": "Dev", "fit_score": 90}]}
        config = {
            "min_fit_score": 0, "mode": "auto",
            "searches": [{"name": "s", "keywords": "k", "enabled": True}],
            "blocklist": {"companies": [], "title_keywords": [], "enabled": True},
            "max_daily_applications": 15,
        }
        observe = {"cycle_id": "c1", "config": config, "applied_ids": set(),
                   "daily_applied": 0, "daily_remaining": 15}

        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=hunted), \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock):
            result = await _decide(observe)

        assert len(result["candidates"]) == 1

    @pytest.mark.asyncio
    async def test_tightening_below_the_floor_is_still_honoured(self):
        """The floor is a floor, not an override: a STRICTER threshold wins."""
        from unittest.mock import AsyncMock, patch

        from naukri_server.agent import _decide

        hunted = {"status": "success", "jobs_found": 1, "jobs_matched": 1,
                  "ranked_jobs": [{"job_id": "J1", "company": "Acme",
                                   "title": "Dev", "fit_score": 90}]}
        config = {
            "min_fit_score": 95, "mode": "auto",
            "searches": [{"name": "s", "keywords": "k", "enabled": True}],
            "blocklist": {"companies": [], "title_keywords": [], "enabled": True},
            "max_daily_applications": 15,
        }
        observe = {"cycle_id": "c1", "config": config, "applied_ids": set(),
                   "daily_applied": 0, "daily_remaining": 15}

        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=hunted) as hunt, \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock):
            await _decide(observe)

        assert hunt.await_args.kwargs["min_fit_score"] == 95


# ---------------------------------------------------------------------------
# 5. The forced approval cycle — for the levers that cannot be tier C
# ---------------------------------------------------------------------------

class TestScoringChangeForcesAnApprovalCycle:
    def test_auto_is_downgraded_when_the_fingerprint_moves(self, tmp_path, monkeypatch):
        from naukri_server import agent

        monkeypatch.setattr(agent, "POLICY_STATE_PATH", tmp_path / "state.json")
        mode, reason = agent._effective_mode({"mode": "auto"}, "c1")
        assert mode == "approval"
        assert "policy changed" in reason

    def test_an_unchanged_fingerprint_runs_in_the_configured_mode(self, tmp_path, monkeypatch):
        """CONTROL. If it downgraded unconditionally, "auto" would be dead code
        and the test above would prove nothing about change detection."""
        from naukri_server import agent

        state = tmp_path / "state.json"
        monkeypatch.setattr(agent, "POLICY_STATE_PATH", state)
        state.write_text(json.dumps(
            {"last_policy_hash": agent.current_policy_hash()}), encoding="utf-8")

        mode, reason = agent._effective_mode({"mode": "auto"}, "c1")
        assert mode == "auto"
        assert reason is None

    @pytest.mark.asyncio
    async def test_the_FIRST_EVER_cycle_never_auto_applies(self, tmp_path, monkeypatch):
        """A cycle that has never observed a scoring fingerprint shows the list.

        `requires_approval_cycle(current, None)` is True by design, and this is
        the case that matters most: a fresh install, or a state file lost, is
        exactly when nobody knows what the policy currently says.

        Found the hard way. Two auto-mode tests in test_agent.py were passing
        only because an earlier run had left `agent_policy_state.json` in the
        repo root carrying the current hash — so the downgrade never fired and
        nothing pinned that it should.
        """
        from unittest.mock import AsyncMock, patch

        from naukri_server import agent

        monkeypatch.setattr(agent, "POLICY_STATE_PATH", tmp_path / "state.json")
        assert not (tmp_path / "state.json").exists()

        decide_result = {
            "cycle_id": "c-first",
            "config": {"mode": "auto", "max_daily_applications": 15},
            "candidates": [agent.AgentCandidate(
                job_id="J1", title="Dev", company="Acme", fit_score=95,
                search_name="s")],
            "applied_ids": set(), "daily_applied": 0, "daily_remaining": 10,
        }

        with patch("naukri_server.tools.apply._apply_single",
                   new_callable=AsyncMock,
                   return_value={"status": "applied"}) as apply_single, \
             patch("naukri_server.database.update_agent_decision",
                   new_callable=AsyncMock), \
             patch("naukri_server.database.store_notification",
                   new_callable=AsyncMock), \
             patch("naukri_server.events.event_bus") as bus:
            bus.emit = AsyncMock()
            result = await agent._act(decide_result)

        assert apply_single.await_count == 0, "it APPLIED on a first-ever cycle"
        assert result["mode"] == "approval"
        assert result["applied"] == 0
        assert result["pending_approval"] == 1
        assert result["mode_downgraded_from"] == "auto"
        assert "policy changed" in result["mode_downgrade_reason"]

    def test_dry_run_is_never_upgraded(self, tmp_path, monkeypatch):
        from naukri_server import agent

        monkeypatch.setattr(agent, "POLICY_STATE_PATH", tmp_path / "state.json")
        assert agent._effective_mode({"mode": "dry_run"}, "c1")[0] == "dry_run"
        assert agent._effective_mode({"mode": "approval"}, "c1")[0] == "approval"

    def test_a_scoring_edit_moves_the_fingerprint(self, config_file, tmp_path, monkeypatch):
        """The whole mechanism end to end: edit the file, the hash moves, the
        next auto cycle is downgraded."""
        from naukri_server import agent

        state = tmp_path / "state.json"
        monkeypatch.setattr(agent, "POLICY_STATE_PATH", state)

        before = agent.current_policy_hash()
        state.write_text(json.dumps({"last_policy_hash": before}), encoding="utf-8")
        assert agent._effective_mode({"mode": "auto"}, "c1")[0] == "auto"

        _write_config(config_file, {
            "config_version": 1, "revision": 2,
            "scoring": {"weights": {"skills": 0.8, "experience": 0.2}},
        })
        _reload(config_file)

        after = agent.current_policy_hash()
        assert after != before
        assert agent._effective_mode({"mode": "auto"}, "c1")[0] == "approval"


# ---------------------------------------------------------------------------
# 6. The partial-reset bug in the one write path that DOES reach the agent
# ---------------------------------------------------------------------------

class TestAgentConfigPatchesDoNotSilentlyReset:
    @pytest.mark.asyncio
    async def test_patching_one_quiet_hour_keeps_the_other(self, monkeypatch, tmp_path):
        """`config.update(patch)` replaced the whole nested block, so patching
        start_hour reset end_hour to 8 — a partial patch was a partial reset of
        the window that bounds autonomous applying."""
        from unittest.mock import AsyncMock, patch as mpatch

        from naukri_server.tools.agent_tool import _agent_update_config

        base = {
            "enabled": False, "mode": "dry_run", "max_daily_applications": 15,
            "min_fit_score": 70,
            "quiet_hours": {"enabled": True, "start_hour": 20, "end_hour": 8},
            "searches": [{"name": "s", "keywords": "k", "enabled": True}],
            "blocklist": {"companies": [], "title_keywords": [], "enabled": True},
        }
        saved = {}

        with mpatch("naukri_server.agent.load_agent_config", return_value=dict(base)), \
             mpatch("naukri_server.agent.save_agent_config",
                    side_effect=lambda cfg: saved.update(cfg)):
            result = await _agent_update_config(json.dumps(
                {"quiet_hours": {"start_hour": 22}}))

        assert result["status"] == "success", result
        assert saved["quiet_hours"]["start_hour"] == 22
        assert saved["quiet_hours"]["end_hour"] == 8
        assert saved["quiet_hours"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_the_merge_check_CAN_fail(self):
        """CONTROL: the shallow merge the tool used to do, on the same inputs."""
        base = {"quiet_hours": {"enabled": True, "start_hour": 20, "end_hour": 8}}
        shallow = dict(base)
        shallow.update({"quiet_hours": {"start_hour": 22}})
        assert "end_hour" not in shallow["quiet_hours"]

        from naukri_server.tools.agent_tool import _deep_merge

        deep = _deep_merge(base, {"quiet_hours": {"start_hour": 22}})
        assert deep["quiet_hours"]["end_hour"] == 8

    @pytest.mark.asyncio
    async def test_lowering_min_fit_score_warns_that_the_floor_still_holds(self):
        from unittest.mock import patch as mpatch

        from naukri_server.tools.agent_tool import _agent_update_config

        base = {"enabled": False, "mode": "dry_run", "max_daily_applications": 15,
                "min_fit_score": 70,
                "quiet_hours": {"enabled": True, "start_hour": 20, "end_hour": 8},
                "searches": [{"name": "s", "keywords": "k", "enabled": True}],
                "blocklist": {"companies": [], "title_keywords": [],
                              "enabled": True}}

        with mpatch("naukri_server.agent.load_agent_config", return_value=dict(base)), \
             mpatch("naukri_server.agent.save_agent_config", return_value=None):
            result = await _agent_update_config(json.dumps({"min_fit_score": 5}))

        assert result["status"] == "success", result
        assert any("floor" in w for w in result.get("warnings", [])), result
