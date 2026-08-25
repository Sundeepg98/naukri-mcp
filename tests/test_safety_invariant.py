"""THE BOUNDARY, run as an attack rather than asserted as a property.

    RETIRED 2026-08-25: *no sequence of config writes, from any server, may
    grant autonomous apply authority.* The config file CAN now arm this agent.

naukri is the only server in the family that can submit an application with no
human in the loop, so this is where the boundary is load-bearing -- and this
file is where BOTH halves of it are proved, because both halves live in this
package. The adversarial review traced a live five-call path through the config
file:

    1. servers.naukri.agent.enabled     : true      -> agent armed
    2. servers.naukri.agent.mode        : "auto"    -> applies with no approval
    3. servers.naukri.agent.min_fit_score: 0        -> every job is a candidate
    4. servers.naukri.agent.blocklist.enabled: false-> blocked companies eligible
    5. servers.naukri.agent.searches    : [...]     -> arbitrary queries

ending at fifteen real applications a day on his live account. THAT TRACE WAS
CORRECT. The operator overruled the conclusion and kept the protections, so
the escalation now LANDS at the config layer (section 2) and is NEUTRALISED in
Python (section 2b). Four guards, none of them reachable from any file:

    1. MIN_AGENT_FIT_FLOOR, applied in `_decide` as `max(configured, floor)`
       and AGAIN per search, because a per-search override is the selector one
       level down and lists never reach jobcore's schema at all.
    2. `requires_approval_cycle`, for the two levers that cannot be tier C.
       Its honest limit is measured in section 2b: it keys on {scoring,
       candidate} and NOT on `servers.*`, so the five agent writes above do
       not move it. It fires on a first-ever cycle and on any scoring or
       candidate change; it is not a guard on the agent block.
    3. The kill switch, re-checked inside the auto-apply loop.
    4. The daily quota, capping candidates upstream of `_act`, plus
       `validate_agent_config`, which re-checks every file-sourced value and
       drops an overlay whole rather than half-applying it.

What did NOT change, and is asserted throughout: anything under `agent` that
jobcore's schema does not explicitly name is still tier C and still refused;
`agent.max_daily_applications` is still not taken from the shared file; and a
bare `min_fit_score` leaf anywhere else is still tier C.

A SIXTH path ran through a sibling server and never touched the agent block at
all: writing every canonical skill into `candidate.skills` drives
|matched|/|job_skills| to 100 for every job in existence, which drives this
agent's selector. That path is UNCHANGED by the ruling and section 3 is
untouched.

These tests RUN all six, through both surfaces that can reach the file:
`apply_patch` (the tool path) and a hand-edited document (the text-editor path,
which is the workflow the whole design exists to serve and which takes no lock
and honours no compare-and-swap).

Each guard is paired with a CONTROL that shows it CAN fail. Six bugs in this
codebase in one week were checks that could not fail; an assertion that nothing
bad happened is worthless unless something bad is reachable. After the ruling
the controls matter MORE, not less: most of the tier assertions here are now
refuse-then-accept pairs, and a pair where only one half runs proves nothing
about the flag between them.

MEASURED against a build where the guards are absent:

    naukri's own guards reverted (no MIN_AGENT_FIT_FLOOR enforcement, no
    approval-cycle downgrade, shallow config.update):

        11 failed, 20 passed

All PURE: temp config files, an isolated agent_config.json, no network, no
browser, no live account.
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

class TestTheWritePathRatchetsEveryStep:
    """Was `TestTheWritePathRefusesEveryStep`.

    Every step of the escalation is a LOOSENING move on a tier-B key now. It
    refuses once, and lands when asked with confirm_widen. Both halves are
    asserted: a refusal that never becomes an acceptance leaves the flag
    untested, and an acceptance with no refusal leaves the ratchet untested.
    """

    @pytest.mark.parametrize("label,patch", ESCALATION_STEPS,
                             ids=[s[0] for s in ESCALATION_STEPS])
    def test_step_is_refused_without_confirmation(self, config_file, label, patch):
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(patch, actor="test")
        assert result["status"] == "refused", (label, result)
        assert result.get("refusals"), result
        assert "confirm_widen" in " ".join(result["refusals"]), result

    @pytest.mark.parametrize("label,patch", ESCALATION_STEPS,
                             ids=[s[0] for s in ESCALATION_STEPS])
    def test_confirm_widen_DOES_buy_it(self, config_file, label, patch):
        """CONTROL for the pair, and the 2026-08-25 ruling in one line."""
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(patch, actor="test",
                                           confirm_widen=True)
        assert result["status"] == "ok", (label, result)

    def test_an_UNNAMED_agent_key_is_still_refused_at_any_price(self, config_file):
        """The half that did not move. Tier C is not a ratchet with extra
        friction; there is no flag, and there still isn't one."""
        import naukri_server.policy as naukri_policy

        for patch in (
            {"servers": {"naukri": {"agent": {"invented_switch": True}}}},
            {"servers": {"naukri": {"agent": {"deeply": {"nested": 1}}}}},
        ):
            result = naukri_policy.apply_patch(patch, actor="test",
                                               confirm_widen=True)
            assert result["status"] == "refused", (patch, result)
            assert "tier c" in " ".join(result["refusals"]).lower(), result

    def test_the_daily_quota_still_cannot_be_raised(self, config_file):
        """It is one of the four Python guards that replaced tier C here."""
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(
            {"servers": {"naukri": {"agent": {"max_daily_applications": 99}}}},
            actor="test", confirm_widen=True)
        assert result["status"] == "refused", result
        assert "ceiling" in " ".join(result["refusals"]), result

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

@pytest.fixture
def isolated_agent_config(tmp_path, monkeypatch):
    """agent_config.json at a tmp path, so no test reads the live one.

    `load_agent_config` reads CONFIG_PATH from DATA_DIR, which on a developer
    box is the REAL agent_config.json in the repo root. Without this the tests
    below would measure whatever that file happens to say today.
    """
    from naukri_server import agent

    path = tmp_path / "agent_config.json"
    path.write_text(json.dumps({
        "enabled": False, "mode": "dry_run", "max_daily_applications": 15,
        "min_fit_score": 70, "per_search_limit": 20,
        "quiet_hours": {"enabled": True, "start_hour": 20, "end_hour": 8},
        "searches": [{"name": "baseline", "keywords": "node.js",
                      "enabled": True}],
        "blocklist": {"companies": [], "title_keywords": [], "enabled": True},
    }), encoding="utf-8")
    monkeypatch.setattr(agent, "CONFIG_PATH", path)
    monkeypatch.setattr(agent, "POLICY_STATE_PATH", tmp_path / "state.json")
    return path


class TestAHandEditedFileCanArmTheAgent:
    """Was `TestAHandEditedFileCannotArmTheAgent`. The ruling, at full strength.

    Notepad calls no function, so the file has always been the surface that
    matters. It now arms the agent from that surface with no ceremony at all.
    Everything that keeps a file-armed agent from applying to the wrong thing
    is asserted in `TestAndThenPythonNeutralisesIt` below -- these tests are
    only about arrival.
    """

    def test_the_whole_escalation_in_one_saved_file_IS_loaded(self, config_file):
        _write_config(config_file, FULL_ESCALATION)
        loaded = _reload(config_file)

        section = loaded.policy.server("naukri").get("agent", {})
        assert section.get("enabled") is True, section
        assert section.get("mode") == "auto", section
        assert section.get("min_fit_score") == 0, section
        assert section.get("blocklist", {}).get("enabled") is False, section
        assert len(section.get("searches") or []) == 1, section

    def test_it_reaches_the_AGENT_not_just_the_policy(self, config_file,
                                                      isolated_agent_config):
        """The load-bearing half. A value sitting in the loaded policy that
        `load_agent_config` never consults would be a display echo, which is
        exactly what these keys used to be."""
        from naukri_server.agent import load_agent_config

        _write_config(config_file, FULL_ESCALATION)
        _reload(config_file)
        config = load_agent_config()

        assert config["enabled"] is True
        assert config["mode"] == "auto"
        assert config["min_fit_score"] == 0
        assert config["blocklist"]["enabled"] is False
        assert [s["name"] for s in config["searches"]] == ["anything"]
        assert config["per_search_limit"] == 200

    def test_nothing_in_the_escalation_is_tier_c_any_more(self, config_file):
        """Was `test_the_refusal_is_loud_and_names_the_keys`.

        There is no refusal left to be loud about for these five. The loudness
        moved: `load_agent_config` logs what the file supplied, which is the
        same need (he edits, saves, and can read what happened) answered from
        the layer that now decides.
        """
        _write_config(config_file, FULL_ESCALATION)
        loaded = _reload(config_file)
        assert loaded.tier_c_refusals == (), loaded.tier_c_refusals

    def test_an_UNNAMED_key_in_the_same_file_is_STILL_refused_loudly(
            self, config_file):
        """CONTROL for the assertion above, and the surviving half of the
        original test. If the refusal list were empty because the census
        stopped running, this would be empty too."""
        body = json.loads(json.dumps(FULL_ESCALATION))
        body["servers"]["naukri"]["agent"]["invented_switch"] = True
        _write_config(config_file, body)
        loaded = _reload(config_file)

        assert len(loaded.tier_c_refusals) == 1, loaded.tier_c_refusals
        assert "invented_switch" in " ".join(loaded.tier_c_refusals)

    def test_the_load_names_what_the_file_supplied(self, config_file,
                                                   isolated_agent_config, caplog):
        """He edits, saves, and must be able to read what happened.

        That need did not go away when the refusal did; it moved to the layer
        that now decides. A silent overlay is the same defect as a silent
        refusal, pointing the other way.
        """
        from naukri_server.agent import load_agent_config

        _write_config(config_file, FULL_ESCALATION)
        _reload(config_file)
        with caplog.at_level("INFO", logger="naukri_server.agent"):
            load_agent_config()

        assert "shared policy file" in caplog.text
        for named in ("mode", "min_fit_score", "enabled"):
            assert named in caplog.text, named

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

    def test_the_agent_reads_its_own_file_AND_the_shared_one(self, config_file,
                                                             isolated_agent_config):
        """Was `test_the_agent_reads_its_own_file_not_this_one`.

        It reads both, and the PRECEDENCE is the thing worth pinning: the
        shared file wins for the six, `agent_config.json` decides everything
        else. `max_daily_applications` is the key that proves the boundary is
        real rather than "the file wins" with no edge.
        """
        from naukri_server.agent import FILE_DECIDABLE_KEYS, load_agent_config

        body = json.loads(json.dumps(FULL_ESCALATION))
        body["servers"]["naukri"]["agent"]["max_daily_applications"] = 25
        _write_config(config_file, body)
        _reload(config_file)
        config = load_agent_config()

        assert config["mode"] == "auto", "the shared file decides the six"
        assert config["max_daily_applications"] == 15, (
            "and decides nothing else -- 25 in the shared file, 15 in "
            "agent_config.json, and the agent used 15"
        )
        assert config["quiet_hours"]["start_hour"] == 20
        assert "max_daily_applications" not in FILE_DECIDABLE_KEYS

    def test_an_EMPTY_searches_list_means_not_specified(self, config_file,
                                                        isolated_agent_config):
        """THE TRAP, pinned. The shipped config/jobhunt.json really does carry
        `searches: []`, and `validate_agent_config` requires a NON-EMPTY list.
        A naive merge fails validation on the very first load and takes the
        agent down with it, so empty means "no opinion", never "search for
        nothing"."""
        from naukri_server.agent import agent_config_overlay, load_agent_config

        _write_config(config_file, {
            "config_version": 1, "revision": 1,
            "servers": {"naukri": {"agent": {"searches": [], "mode": "auto"}}},
        })
        _reload(config_file)

        assert "searches" not in agent_config_overlay()
        config = load_agent_config()
        assert [s["name"] for s in config["searches"]] == ["baseline"]
        assert config["mode"] == "auto", "the rest of the overlay still landed"

    def test_the_SHIPPED_config_file_is_a_no_op_overlay(self, config_file,
                                                        isolated_agent_config):
        """A file carrying only shipped defaults must decide nothing.

        jobcore returns a MERGED block, so "present" cannot mean "declared".
        If it did, the shipped file would reset a deliberately-armed agent to
        disabled/dry_run on every single load.
        """
        from jobcore.policy import schema_defaults

        from naukri_server.agent import agent_config_overlay, load_agent_config

        shipped = schema_defaults("servers")["naukri"]["agent"]
        _write_config(config_file, {
            "config_version": 1, "revision": 1,
            "servers": {"naukri": {"agent": json.loads(json.dumps(shipped))}},
        })
        _reload(config_file)

        assert agent_config_overlay() == {}
        assert load_agent_config()["searches"][0]["name"] == "baseline"

    def test_an_INVALID_overlay_is_dropped_WHOLE_not_half_applied(
            self, config_file, isolated_agent_config, caplog):
        """Every file-sourced value goes through `validate_agent_config`.

        Half a policy is worse than none -- the same call jobcore's loader
        makes on a malformed document. Here the mode is off-menu, so the
        arming half must not land either.
        """
        from naukri_server.agent import load_agent_config

        _write_config(config_file, {
            "config_version": 1, "revision": 1,
            "servers": {"naukri": {"agent": {
                "enabled": True, "mode": "yolo",
                "searches": [{"name": "x", "keywords": "x", "enabled": True}],
            }}},
        })
        _reload(config_file)
        with caplog.at_level("ERROR", logger="naukri_server.agent"):
            config = load_agent_config()

        assert config["mode"] == "dry_run", "the bad value did not land"
        assert config["enabled"] is False, (
            "and neither did the GOOD value beside it -- whole, not half"
        )
        assert [s["name"] for s in config["searches"]] == ["baseline"]
        assert "DROPPED" in caplog.text

    def test_the_validation_check_CAN_fail(self, config_file,
                                           isolated_agent_config):
        """CONTROL. The same overlay with a legal mode DOES land, so the test
        above is about validation and not about overlays never working."""
        from naukri_server.agent import load_agent_config

        _write_config(config_file, {
            "config_version": 1, "revision": 1,
            "servers": {"naukri": {"agent": {
                "enabled": True, "mode": "auto",
                "searches": [{"name": "x", "keywords": "x", "enabled": True}],
            }}},
        })
        _reload(config_file)
        config = load_agent_config()

        assert config["mode"] == "auto"
        assert config["enabled"] is True


# ---------------------------------------------------------------------------
# 2b. ...AND THEN PYTHON NEUTRALISES IT
# ---------------------------------------------------------------------------
#
# This is the half the 2026-08-25 ruling moved down a layer. The escalation
# lands in the config (section 2); these tests plant it in a real file, load it
# through the real `load_agent_config`, and then measure what `_decide` and
# `_effective_mode` actually do with it.

class TestAndThenPythonNeutralisesIt:
    """The full five-write escalation, loaded, then measured through _decide.

    Every assertion here is on a NUMBER that came out of the real code path
    with the real file in place -- not on a guard existing.
    """

    HUNTED = {"status": "success", "jobs_found": 5, "jobs_matched": 5,
              "ranked_jobs": [{"job_id": f"J{i}", "company": "Acme",
                               "title": "Dev", "fit_score": 95}
                              for i in range(5)]}

    def _armed_config(self, config_file):
        from naukri_server.agent import load_agent_config

        _write_config(config_file, FULL_ESCALATION)
        _reload(config_file)
        return load_agent_config()

    @pytest.mark.asyncio
    async def test_the_file_asked_for_zero_and_the_search_was_asked_for_the_floor(
            self, config_file, isolated_agent_config):
        """GUARD 1. `min_fit_score: 0` and a per-search override of 0, both
        from the file, and the selector still starts at 60."""
        from unittest.mock import AsyncMock, patch

        from naukri_server.agent import _decide

        config = self._armed_config(config_file)
        assert config["min_fit_score"] == 0, "the escalation really did land"
        assert config["searches"][0]["min_fit_score"] == 0

        observe = {"cycle_id": "c1", "config": config, "applied_ids": set(),
                   "daily_applied": 0, "daily_remaining": 15}
        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=self.HUNTED) as hunt, \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock):
            await _decide(observe)

        assert hunt.await_args.kwargs["min_fit_score"] == MIN_AGENT_FIT_FLOOR
        assert MIN_AGENT_FIT_FLOOR == 60

    @pytest.mark.asyncio
    async def test_the_file_asked_for_200_results_and_got_the_ceiling(
            self, config_file, isolated_agent_config):
        """`per_search_limit` is read now -- it was a decoy while `_decide`
        passed a hardcoded 20 -- and it is clamped in Python, because jobcore
        enforces its ceiling on the WRITE path only."""
        from unittest.mock import AsyncMock, patch

        from naukri_server.agent import _PER_SEARCH_LIMIT_CEILING, _decide

        config = self._armed_config(config_file)
        assert config["per_search_limit"] == 200

        observe = {"cycle_id": "c1", "config": config, "applied_ids": set(),
                   "daily_applied": 0, "daily_remaining": 15}
        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=self.HUNTED) as hunt, \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock):
            await _decide(observe)

        assert hunt.await_args.kwargs["limit"] == _PER_SEARCH_LIMIT_CEILING == 100

    @pytest.mark.asyncio
    async def test_the_quota_caps_candidates_BEFORE_act_ever_runs(
            self, config_file, isolated_agent_config):
        """GUARD 4. Five jobs clear the floor; three slots remain; three
        candidates. The cap is upstream of `_act`, so it bounds applications
        rather than merely reporting on them."""
        from unittest.mock import AsyncMock, patch

        from naukri_server.agent import _decide

        config = self._armed_config(config_file)
        observe = {"cycle_id": "c1", "config": config, "applied_ids": set(),
                   "daily_applied": 12, "daily_remaining": 3}
        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=self.HUNTED), \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock):
            result = await _decide(observe)

        assert len(result["candidates"]) == 3, len(result["candidates"])

    @pytest.mark.asyncio
    async def test_the_quota_cap_CAN_fail(self, config_file,
                                          isolated_agent_config):
        """CONTROL. With room for all five, all five arrive -- so the test
        above measures the cap and not an empty pipeline."""
        from unittest.mock import AsyncMock, patch

        from naukri_server.agent import _decide

        config = self._armed_config(config_file)
        observe = {"cycle_id": "c1", "config": config, "applied_ids": set(),
                   "daily_applied": 0, "daily_remaining": 15}
        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=self.HUNTED), \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock):
            result = await _decide(observe)

        assert len(result["candidates"]) == 5

    def test_the_file_cannot_raise_the_quota_that_does_the_capping(
            self, config_file, isolated_agent_config):
        """A guard whose value the same file can raise is worth less than one
        it cannot. The file says 25; the agent uses agent_config.json's 15."""
        body = json.loads(json.dumps(FULL_ESCALATION))
        body["servers"]["naukri"]["agent"]["max_daily_applications"] = 25
        _write_config(config_file, body)
        _reload(config_file)

        from naukri_server.agent import load_agent_config

        assert load_agent_config()["max_daily_applications"] == 15

    def test_the_FIRST_EVER_cycle_still_downgrades_auto_to_approval(
            self, config_file, isolated_agent_config):
        """GUARD 2, in the case where it does fire for a file-armed agent.

        `requires_approval_cycle(current, None)` is True by design, so an
        agent armed by a file edit on a box with no state file shows the list
        before it submits anything.
        """
        from naukri_server import agent

        config = self._armed_config(config_file)
        assert config["mode"] == "auto"
        assert not agent.POLICY_STATE_PATH.exists()

        mode, reason = agent._effective_mode(config, "c1")
        assert mode == "approval"
        assert "policy changed" in reason

    def test_and_the_HONEST_LIMIT_of_that_guard_measured_not_assumed(
            self, config_file, isolated_agent_config):
        """THE CAVEAT, pinned as a test so nobody re-derives it as a discovery.

        `requires_approval_cycle` keys on `policy_hash`, which covers
        {scoring, candidate} and NOT `servers.*`. So once the fingerprint is
        known, the five agent writes do NOT move it and do NOT buy an approval
        cycle. That guard was built for the two levers that cannot be tier C
        and it still catches those -- it is not, and never was, a guard on the
        agent block.

        This is why guards 1, 3 and 4 are the ones that bound a file-armed
        agent, and why documenting guard 2 as "config-independent, therefore
        it covers this" would have been wrong.
        """
        from naukri_server import agent

        _write_config(config_file, {"config_version": 1, "revision": 1})
        _reload(config_file)
        before = agent.current_policy_hash()
        agent.POLICY_STATE_PATH.write_text(
            json.dumps({"last_policy_hash": before}), encoding="utf-8")
        assert agent._effective_mode({"mode": "auto"}, "c1")[0] == "auto"

        # the five agent writes...
        _write_config(config_file, FULL_ESCALATION)
        _reload(config_file)
        assert agent.current_policy_hash() == before, (
            "the agent block is not in the fingerprint"
        )
        assert agent._effective_mode(agent.load_agent_config(), "c1")[0] == "auto"

        # ...and the CONTROL: a scoring write DOES move it.
        body = json.loads(json.dumps(FULL_ESCALATION))
        body["scoring"] = {"weights": {"skills": 0.8, "experience": 0.2}}
        _write_config(config_file, body)
        _reload(config_file)
        assert agent.current_policy_hash() != before
        assert agent._effective_mode({"mode": "auto"}, "c1")[0] == "approval"

    @pytest.mark.asyncio
    async def test_the_kill_switch_halts_a_file_armed_auto_batch(
            self, config_file, isolated_agent_config):
        """GUARD 3. Checked INSIDE the loop, so it stops the rest of a batch
        rather than only refusing to start one."""
        from unittest.mock import AsyncMock, patch

        from naukri_server import agent

        candidates = [agent.AgentCandidate(job_id=f"J{i}", title="Dev",
                                           company="Acme", fit_score=95,
                                           search_name="s")
                      for i in range(4)]
        decide_result = {
            "cycle_id": "c-kill",
            "config": {"mode": "auto", "max_daily_applications": 15},
            "candidates": candidates, "applied_ids": set(),
            "daily_applied": 0, "daily_remaining": 15,
        }
        # fingerprint already seen, so the mode is genuinely "auto" here
        agent.POLICY_STATE_PATH.write_text(
            json.dumps({"last_policy_hash": agent.current_policy_hash()}),
            encoding="utf-8")

        with patch("naukri_server.kill_switch.is_tripped", return_value=True), \
             patch("naukri_server.tools.apply._apply_single",
                   new_callable=AsyncMock,
                   return_value={"status": "applied"}) as apply_single, \
             patch("naukri_server.database.update_agent_decision",
                   new_callable=AsyncMock), \
             patch("naukri_server.events.event_bus") as bus:
            bus.emit = AsyncMock()
            result = await agent._act(decide_result)

        assert apply_single.await_count == 0, "it applied with the switch tripped"
        assert result["mode"] == "auto"

    @pytest.mark.asyncio
    async def test_the_kill_switch_check_CAN_fail(self, config_file,
                                                  isolated_agent_config):
        """CONTROL. With the switch untripped the same batch DOES apply, so
        the test above measures the switch and not a dead code path."""
        from unittest.mock import AsyncMock, patch

        from naukri_server import agent

        candidates = [agent.AgentCandidate(job_id="J1", title="Dev",
                                           company="Acme", fit_score=95,
                                           search_name="s")]
        decide_result = {
            "cycle_id": "c-live",
            "config": {"mode": "auto", "max_daily_applications": 15},
            "candidates": candidates, "applied_ids": set(),
            "daily_applied": 0, "daily_remaining": 15,
        }
        agent.POLICY_STATE_PATH.write_text(
            json.dumps({"last_policy_hash": agent.current_policy_hash()}),
            encoding="utf-8")

        with patch("naukri_server.kill_switch.is_tripped", return_value=False), \
             patch("naukri_server.tools.apply._apply_single",
                   new_callable=AsyncMock,
                   return_value={"status": "applied"}) as apply_single, \
             patch("naukri_server.database.update_agent_decision",
                   new_callable=AsyncMock), \
             patch("naukri_server.events.event_bus") as bus:
            bus.emit = AsyncMock()
            await agent._act(decide_result)

        assert apply_single.await_count == 1


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
