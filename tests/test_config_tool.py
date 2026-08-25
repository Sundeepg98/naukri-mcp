"""naukri_config / naukri_set_config — the operator-facing surface.

The server provides the capability; the client decides whether to use it. What
these tests pin is that the surface is HONEST: it reports where each value came
from, it names what the file is not allowed to decide, and it says so loudly
when there is no file at all rather than silently running on defaults.

All PURE: temp config files. No network, no browser, no live account.
"""

import json

import pytest


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    import naukri_server.policy as naukri_policy

    cfg = tmp_path / "jobhunt.json"
    cfg.write_text(json.dumps({"config_version": 1, "revision": 1}), encoding="utf-8")
    monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
    naukri_policy.invalidate()
    try:
        yield cfg
    finally:
        naukri_policy.invalidate()


class TestReadingWithNoFile:
    @pytest.mark.asyncio
    async def test_no_file_still_returns_the_shipped_policy(self):
        """The independence guarantee: a bare clone works and scores."""
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()
        assert result["status"] == "success"
        assert result["source"] is None
        assert result["scoring"]["weights"] == {"skills": 0.6, "experience": 0.4}

    @pytest.mark.asyncio
    async def test_no_file_is_reported_LOUDLY_with_every_path_tried(self):
        """The worst failure mode for a config system is editing a file nothing
        reads and getting no error. `config_status` has to say so."""
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()
        assert "no file found" in result["config_status"]

    @pytest.mark.asyncio
    async def test_the_status_line_CAN_say_something_else(self, config_file):
        """CONTROL for the assertion above."""
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()
        assert result["source"] is not None
        assert result["source"].endswith("jobhunt.json")
        assert "loaded from" in result["config_status"]

    @pytest.mark.asyncio
    async def test_the_reported_path_is_not_an_absolute_local_path(self, config_file):
        """`utils.scrub_result` reduces any surviving absolute path to its
        BASENAME, which would collapse every `searched` entry to the identical
        string. Paths are rendered relative to the checkout or to home instead:
        actionable, distinguishable, and no machine layout published."""
        from naukri_server.policy import display_path
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()
        # The exact needle first. The two drive-letter checks beside it cannot
        # fire on the Linux runner -- there this fixture's file is
        # /tmp/pytest-of-runner/..., which has no drive letter -- so on the box
        # that gates a merge they pass without being able to see anything.
        # This one fails on a leak on either OS.
        assert str(config_file) not in result["source"], (
            "the reported source is this machine's absolute path: %r"
            % result["source"]
        )
        assert ":\\" not in result["source"] and ":/" not in result["source"]
        assert result["source"] != "jobhunt.json", (
            "collapsed to a bare basename — the caller cannot tell WHICH file"
        )
        # And the anchor form is the useful one for the real location.
        real = display_path(
            str(config_file.parent.parent.parent / "config" / "jobhunt.json"))
        assert real is not None


class TestDisplayPathUnderNeitherAnchor:
    """The case that only CI could see, made visible on every box.

    The test above passes on Windows for an accidental reason: a Windows temp
    dir lives under ``C:\\Users\\Dell``, so the HOME anchor always caught it.
    On the Linux runner ``tmp_path`` is ``/tmp/pytest-of-runner/...``, under
    neither the checkout nor ``/home/runner`` -- both anchors missed, the old
    code fell through to ``p.name``, and every path collapsed to the identical
    string "jobhunt.json". Two pushes went red there and green here.

    These tests force both anchors to miss regardless of platform, so the
    collapse is reproducible on the box where the code is written.
    """

    @pytest.fixture
    def no_anchor(self, monkeypatch, tmp_path):
        """Force BOTH anchors to miss, the way /tmp misses them on the runner.

        The repo root is deliberately DEEP. A shallow fake root sharing a
        parent with the subject path yields a relpath of "../x/y/z" -- one
        ".." -- which passes the <=4 gate, returns from the FIRST branch, and
        never reaches the code under test. That mistake was caught by mutating
        the fallback back to `p.name` and watching these tests stay green; the
        depth below is what makes the mutation red.
        """
        from pathlib import Path

        import naukri_server.policy as naukri_policy

        elsewhere = tmp_path / "a" / "b" / "c" / "d" / "e" / "f"
        home = tmp_path / "not-home"
        elsewhere.mkdir(parents=True)
        home.mkdir()
        monkeypatch.setattr(naukri_policy, "_REPO_ROOT", elsewhere)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        return naukri_policy.display_path

    def test_it_does_not_collapse_to_a_bare_basename(self, no_anchor, tmp_path):
        deep = tmp_path / "sub" / "pytest-of-runner" / "pytest-0" / "test_x0" / "jobhunt.json"
        out = no_anchor(str(deep))
        assert out != "jobhunt.json", (
            "collapsed to a bare basename with no anchor available -- this is "
            "the Linux-only regression; two entries of `searched` would now "
            "print the identical string"
        )
        assert out.endswith("jobhunt.json")

    def test_two_different_files_stay_distinguishable(self, no_anchor, tmp_path):
        """The actual point. One string for two files is the failure."""
        a = no_anchor(str(tmp_path / "sub" / "run-a" / "cfg" / "jobhunt.json"))
        b = no_anchor(str(tmp_path / "sub" / "run-b" / "cfg" / "jobhunt.json"))
        assert a != b, f"both rendered as {a!r}"

    def test_it_publishes_no_drive_letter_and_no_home(self, no_anchor, tmp_path):
        """The guarantee the anchoring exists to keep, held by the tail too."""
        out = no_anchor(str(tmp_path / "sub" / "p" / "q" / "jobhunt.json"))
        assert ":\\" not in out and ":/" not in out
        assert not out.startswith("/")
        assert str(tmp_path) not in out

    def test_it_survives_the_scrubber_untouched(self, no_anchor, tmp_path):
        """A form the scrubber eats would be no better than the basename."""
        from naukri_server.utils import scrub_paths

        out = no_anchor(str(tmp_path / "sub" / "p" / "q" / "jobhunt.json"))
        assert scrub_paths(out) == out

    def test_a_single_component_path_gets_no_elision_marker(self, no_anchor):
        """Nothing was elided, so `.../` would be a lie."""
        assert no_anchor("jobhunt.json") == "jobhunt.json"


class TestProvenance:
    @pytest.mark.asyncio
    async def test_an_edited_key_is_marked_as_coming_from_the_file(self, config_file):
        from naukri_server.tools.config_tool import naukri_config, naukri_set_config

        await naukri_set_config(json.dumps(
            {"scoring": {"weights": {"skills": 0.75, "experience": 0.25}}}))
        result = await naukri_config()

        assert result["scoring"]["weights"]["skills"] == 0.75
        assert result["provenance"].get("scoring.weights.skills") == "file"

    @pytest.mark.asyncio
    async def test_an_untouched_key_is_marked_as_default(self, config_file):
        """CONTROL. If provenance said "file" for everything it would answer
        nothing, which is the question it exists to answer."""
        from naukri_server.tools.config_tool import naukri_config, naukri_set_config

        await naukri_set_config(json.dumps(
            {"scoring": {"weights": {"skills": 0.75, "experience": 0.25}}}))
        result = await naukri_config()

        assert result["provenance"].get("scoring.bonuses.location_match") in (
            "default", None)
        assert result["scoring"]["bonuses"]["location_match"] == 5

    @pytest.mark.asyncio
    async def test_the_envelope_is_not_reported_as_an_unknown_key(self, config_file):
        """jobcore's own default_document() writes config_version / revision /
        updated_at / updated_by and its census then calls them decoys. A warning
        that fires on correct input trains people to ignore warnings."""
        from naukri_server.tools.config_tool import naukri_config

        config_file.write_text(json.dumps({
            "config_version": 1, "revision": 3,
            "updated_at": "2026-08-21T00:00:00Z", "updated_by": "claude",
        }), encoding="utf-8")
        result = await naukri_config()
        assert result["unknown_keys"] == [], result["unknown_keys"]

    @pytest.mark.asyncio
    async def test_a_genuinely_unknown_key_IS_reported(self, config_file):
        """CONTROL for the filter above — it must not swallow real ones."""
        from naukri_server.tools.config_tool import naukri_config

        config_file.write_text(json.dumps({
            "config_version": 1, "revision": 3,
            "scoring": {"weights": {"skills": 0.6, "experience": 0.4}},
            "nonsense_block": {"x": 1},
        }), encoding="utf-8")
        result = await naukri_config()
        assert any("nonsense" in k for k in result["unknown_keys"]), result["unknown_keys"]


class TestWriting:
    @pytest.mark.asyncio
    async def test_a_tier_a_write_lands_and_bumps_the_revision(self, config_file):
        from naukri_server.tools.config_tool import naukri_config, naukri_set_config

        before = (await naukri_config())["revision"]
        result = await naukri_set_config(json.dumps(
            {"servers": {"naukri": {"display_min_score": 72}}}))
        assert result["status"] == "ok", result
        after = await naukri_config()
        assert after["revision"] == before + 1
        assert after["server"]["display_min_score"] == 72

    @pytest.mark.asyncio
    async def test_a_stale_base_revision_conflicts_instead_of_clobbering(self, config_file):
        from naukri_server.tools.config_tool import naukri_config, naukri_set_config

        await naukri_set_config(json.dumps(
            {"servers": {"naukri": {"display_min_score": 72}}}))
        result = await naukri_set_config(
            json.dumps({"servers": {"naukri": {"display_min_score": 40}}}),
            base_revision=1)
        assert result["status"] == "conflict", result
        assert result["error_code"] == "CONFLICT"
        assert (await naukri_config())["server"]["display_min_score"] == 72

    @pytest.mark.asyncio
    async def test_a_fresh_base_revision_succeeds(self, config_file):
        """CONTROL: the CAS check must not refuse everything."""
        from naukri_server.tools.config_tool import naukri_config, naukri_set_config

        rev = (await naukri_config())["revision"]
        result = await naukri_set_config(
            json.dumps({"servers": {"naukri": {"display_min_score": 41}}}),
            base_revision=rev)
        assert result["status"] == "ok", result

    @pytest.mark.asyncio
    async def test_bad_json_is_a_validation_error_not_a_traceback(self, config_file):
        from naukri_server.tools.config_tool import naukri_set_config

        result = await naukri_set_config("{not json")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_a_foreign_section_is_refused_by_name(self, config_file):
        from naukri_server.tools.config_tool import naukri_set_config

        result = await naukri_set_config(json.dumps(
            {"servers": {"uplers": {"include_aggregated": True}}}))
        assert result["status"] == "refused", result
        assert any("uplers" in r for r in result["refusals"]), result

    @pytest.mark.asyncio
    async def test_the_agent_block_is_RATCHETED_from_here_not_refused(self,
                                                                      config_file):
        """Was `test_the_agent_block_is_refused_from_here_too`.

        Since 2026-08-25 this tool CAN arm the agent. `mode: "auto"` is a
        loosening move on a tier-B key, so it refuses once and lands when
        asked properly -- both halves asserted, because a refusal that never
        turns into an acceptance would leave the confirmation flag untested.
        """
        from naukri_server.tools.config_tool import naukri_config, naukri_set_config

        result = await naukri_set_config(json.dumps(
            {"servers": {"naukri": {"agent": {"mode": "auto"}}}}))
        assert result["status"] == "refused", result
        assert "confirm_widen" in " ".join(result["refusals"]), result

        result = await naukri_set_config(
            json.dumps({"servers": {"naukri": {"agent": {"mode": "auto"}}}}),
            confirm_widen=True)
        assert result["status"] == "ok", result
        assert (await naukri_config())["server"]["agent"]["mode"] == "auto"

    @pytest.mark.asyncio
    async def test_an_UNNAMED_agent_key_is_still_refused_from_here(self,
                                                                   config_file):
        """CONTROL for the pair above, and the half the ruling did not touch.

        Six keys are named and loadable; the subtree still denies by default,
        and confirm_widen does not buy a tier-C key at any price.
        """
        from naukri_server.tools.config_tool import naukri_set_config

        result = await naukri_set_config(
            json.dumps({"servers": {"naukri": {"agent": {"invented": True}}}}),
            confirm_widen=True)
        assert result["status"] == "refused", result
        assert "tier C" in " ".join(result["refusals"]), result


class TestTheSurfaceExplainsItself:
    @pytest.mark.asyncio
    async def test_it_names_what_the_file_cannot_decide(self, config_file):
        """"Why did my edit do nothing?" must be answerable from the tool.

        The list SHRANK on 2026-08-25 -- five of the six names it used to
        carry are loadable now -- so the readout has to say the new truth in
        both directions: what is still refused, and what the file now decides.
        A readout still naming `agent.mode` as unreachable would be the
        documentation-true-when-written defect in its purest form.
        """
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()
        blob = " ".join(result["not_loadable_here"])
        assert "agent.max_daily_applications" in blob, result["not_loadable_here"]
        for gone in ("agent.enabled", "agent.mode", "agent.min_fit_score",
                     "agent.searches", "agent.blocklist.enabled",
                     "agent.per_search_limit"):
            assert gone not in blob, (gone, result["not_loadable_here"])

        assert "subtree_deny" in result and result["subtree_deny"]

    @pytest.mark.asyncio
    async def test_it_says_the_file_CAN_arm_the_agent_and_what_bounds_it(self,
                                                                         config_file):
        """The other half. A surface that only lists prohibitions, after the
        prohibition was lifted, is worse than one that says nothing."""
        from naukri_server.tools.config_tool import naukri_config

        blob = (await naukri_config())["agent_authority"].lower()
        assert "can arm" in blob
        for guard in ("floor", "kill switch", "quota", "validate_agent_config"):
            assert guard in blob, guard

    @pytest.mark.asyncio
    async def test_it_reports_the_python_floor(self, config_file):
        from jobcore.config import MIN_AGENT_FIT_FLOOR
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()
        assert result["min_agent_fit_floor"] == MIN_AGENT_FIT_FLOOR

    @pytest.mark.asyncio
    async def test_a_hand_edit_is_surfaced_not_swallowed(self, config_file):
        """H2/H3: Notepad takes no lock and honours no CAS, so the loader has to
        DETECT the edit and say so. A changed score under an unchanged stamp is
        the failure this exists to prevent."""
        from naukri_server.tools.config_tool import naukri_config, naukri_set_config
        import naukri_server.policy as naukri_policy

        await naukri_set_config(json.dumps(
            {"scoring": {"weights": {"skills": 0.7, "experience": 0.3}}}))
        first = await naukri_config()

        body = json.loads(config_file.read_text(encoding="utf-8"))
        body["scoring"]["weights"] = {"skills": 0.8, "experience": 0.2}
        config_file.write_text(json.dumps(body), encoding="utf-8")
        naukri_policy.invalidate()

        second = await naukri_config()
        assert second["policy_hash"] != first["policy_hash"]
        assert second["policy_rev"] != first["policy_rev"]

    @pytest.mark.asyncio
    async def test_a_cosmetic_edit_does_NOT_churn_the_stamp(self, config_file):
        """CONTROL for the test above. If every save moved the hash, the hash
        would carry no information — and it would force a needless approval
        cycle on the agent every time he renamed himself."""
        from naukri_server.tools.config_tool import naukri_config, naukri_set_config
        import naukri_server.policy as naukri_policy

        await naukri_set_config(json.dumps({"candidate": {"name": "A. Candidate"}}))
        first = await naukri_config()

        await naukri_set_config(json.dumps(
            {"candidate": {"headline": "Backend Software Engineer"}}))
        naukri_policy.invalidate()
        second = await naukri_config()

        assert second["policy_hash"] == first["policy_hash"]
