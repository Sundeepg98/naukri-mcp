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
        assert ":\\" not in result["source"] and ":/" not in result["source"]
        assert result["source"] != "jobhunt.json", (
            "collapsed to a bare basename — the caller cannot tell WHICH file"
        )
        # And the anchor form is the useful one for the real location.
        real = display_path(
            str(config_file.parent.parent.parent / "config" / "jobhunt.json"))
        assert real is not None


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
    async def test_the_agent_block_is_refused_from_here_too(self, config_file):
        from naukri_server.tools.config_tool import naukri_set_config

        result = await naukri_set_config(json.dumps(
            {"servers": {"naukri": {"agent": {"mode": "auto"}}}}))
        assert result["status"] == "refused", result


class TestTheSurfaceExplainsItself:
    @pytest.mark.asyncio
    async def test_it_names_what_the_file_cannot_decide(self, config_file):
        """"Why did my edit do nothing?" must be answerable from the tool."""
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()
        blob = " ".join(result["not_loadable_here"])
        for key in ("agent.enabled", "agent.mode", "agent.min_fit_score",
                    "agent.searches", "agent.blocklist.enabled"):
            assert key in blob, (key, result["not_loadable_here"])

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
