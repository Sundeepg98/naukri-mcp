"""Relativising the path FIELDS is not enough: the leak survives in PROSE.

jobcore composes its failure messages as f-strings with the absolute path
already baked in -- ``f"{path} is not valid JSON: {exc}"``,
``f"cannot read {path}: {exc}"``, ``f"could not append to {ledger}: {exc}"`` --
stores the result in ``config_error``, and derives ``config_status`` from it.
``policy.report()`` post-processed ``source`` and ``searched`` through
``display_path`` and never touched either prose field.

WHAT WAS ACTUALLY MEASURED HERE, which is narrower than the report that prompted
this and is written down so nobody re-derives it:

* ``policy.report()`` -- the module-level function -- DOES return
  ``config_error`` with this machine's absolute path in it. That is the real
  leak and it is what the first test below pins.
* ``naukri_config()`` -- the TOOL -- does NOT. ``utils.scrub_result`` runs over
  every tool result and catches it at the boundary. So the payload a client
  sees was already free of drive letters before this fix.

The scrubber saving it is not a reason to leave it, for two reasons. First it
saves it by collapsing the path to its BARE BASENAME, ``jobhunt.json``, which is
the exact "worse than saying nothing" degradation ``display_path`` exists to
prevent -- two candidate files render identically and the reader cannot tell
which one failed. Second, the guarantee then lives only in a downstream
backstop: any surface that renders a path of its own, or any caller of
``policy.report()`` that is not a tool, gets the raw absolute path.

AND THE WORSE DEFECT, found while reproducing the above: for an UNPARSEABLE
file, ``config_status`` said ``"loaded from <file>"``. It had not loaded. naukri
overwrote jobcore's ``config_status`` -- which correctly says ``"error: ..."``
-- with a string composed from ``source`` alone, discarding ``config_error``
entirely. A reader was told the config loaded when it had not, which is a
plainer failure than the leak that started this.

The fix is one line: pass ``display=display_path`` INTO ``snap.report()`` so
jobcore renders every path, field and prose alike, in one place -- and delete
naukri's hand post-processing so there is exactly one place a path is rendered.

All tests are PURE -- temp files only, no network, no browser.
"""

import json

import pytest

from tests.test_path_leaks import (
    DRIVE_PATH,
    assert_no_absolute_path,
    assert_path_absent,
    contains_path,
)


@pytest.fixture
def broken_config(tmp_path, monkeypatch):
    """Point JOBHUNT_CONFIG at a file that is not valid JSON."""
    import naukri_server.policy as naukri_policy

    cfg = tmp_path / "jobhunt.json"
    cfg.write_text("{ this is not valid json ", encoding="utf-8")
    monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
    naukri_policy.invalidate()
    try:
        yield cfg
    finally:
        naukri_policy.invalidate()


@pytest.fixture
def good_config(tmp_path, monkeypatch):
    import naukri_server.policy as naukri_policy

    cfg = tmp_path / "jobhunt.json"
    cfg.write_text(json.dumps({"config_version": 1, "revision": 1}),
                   encoding="utf-8")
    monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
    naukri_policy.invalidate()
    try:
        yield cfg
    finally:
        naukri_policy.invalidate()


class TestTheProseIsRenderedToo:

    def test_the_raw_jobcore_snapshot_really_does_bake_the_path_into_its_prose(
            self, broken_config):
        """THE CONTROL, and it runs on every OS.

        Everything below asserts a path is ABSENT. If the scenario silently
        stopped producing a leak at all -- a fixture that no longer breaks, an
        upstream change -- those assertions would all pass while measuring
        nothing. This one asserts the leak IS there upstream, so the others are
        known to be falsifiable.

        This is exactly the control that saved jobcore on 2026-08-22: its
        absence-assertions went green on Linux while blind, and the control
        failing is what exposed them.
        """
        from naukri_server import policy

        snap = policy.snapshot()
        assert snap.config_error, "the broken file produced no error at all"
        assert str(broken_config) in snap.config_error, (
            "jobcore no longer bakes the absolute path into config_error; the "
            "absence assertions below are no longer proving anything"
        )

    def test_policy_report_does_not_leak_an_absolute_path_in_the_prose(
            self, broken_config):
        """THE LEAK, at the layer that actually has it.

        Asserted on `policy.report()` rather than on the tool, because that is
        where it is real: the tool's copy is saved by `utils.scrub_result`, and
        a test that only looked there would certify a guarantee this module does
        not provide.

        `assert_path_absent` is the PRIMARY assertion and the drive-letter
        walker is corroboration: on naukri's Linux CI runner this fixture's path
        is `/tmp/pytest-of-runner/...` and the walker cannot see it.
        """
        from naukri_server import policy

        out = policy.report()
        needle = str(broken_config)

        assert out["config_error"], "the broken file did not produce an error"
        assert_path_absent(out["config_error"], needle, "config_error")
        assert_path_absent(out["config_status"], needle, "config_status")
        assert_path_absent(out, needle, "policy.report()")
        # Second opinion, Windows-only by construction.
        assert_no_absolute_path(out, "policy.report()")

    def test_every_prose_field_is_rendered_not_just_the_two_we_named(
            self, broken_config):
        """Coverage by walk, not by the list of fields somebody remembered.

        `ledger_error` is a third prose field composed the same way
        (`f"could not append to {ledger}"`). It is None in this scenario, but
        the walk is what stops a fourth one being added unrendered.

        The PARENT directory is checked too: jobcore names files it derives
        from the config's own directory (the history ledger, the write lock),
        and neither equals `source`, so a renderer that substituted only the
        exact source path would still publish the layout above it.
        """
        from naukri_server import policy

        out = policy.report()
        needles = (str(broken_config), str(broken_config.parent))
        for key, value in out.items():
            if not isinstance(value, str):
                continue
            for needle in needles:
                assert not contains_path(value, needle), (
                    "report()[%r] leaks %r: %r" % (key, needle, value)
                )
            assert not DRIVE_PATH.search(value), (
                "report()[%r] leaks a drive-letter path: %r" % (key, value)
            )

    async def test_the_error_message_is_still_an_answer_not_a_redaction(
            self, broken_config):
        """Leak-free is half the requirement. It must still be USABLE.

        The scrubber's basename collapse satisfies "no drive letter" and fails
        this: `jobhunt.json is not valid JSON` cannot tell the reader WHICH
        jobhunt.json, and the whole point of the anchored form is that two
        candidate files stay distinguishable. So this asserts the rendered path
        still carries a separator -- i.e. it is the anchored/home/tail form and
        not the bare basename -- and that the reason survives.
        """
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()

        err = result["config_error"]
        assert err, "no error reported for an unparseable file"
        assert "jobhunt.json" in err, "the file was scrubbed out of its own error"
        assert "not valid JSON" in err, "the REASON was lost"
        assert "/" in err, (
            "collapsed to a bare basename (%r) -- the reader cannot tell which "
            "jobhunt.json failed, which is the degradation display_path exists "
            "to prevent" % err
        )


class TestConfigStatusDoesNotClaimItLoaded:

    def test_config_status_names_the_error_instead_of_claiming_it_loaded(
            self, broken_config):
        """The plainer defect. The file did not load; saying it did is a lie.

        naukri overwrote jobcore's `config_status` with a string built from
        `source` alone, so an unparseable file reported
        `"loaded from <file>"`. jobcore's own property says `"error: ..."`.
        """
        from naukri_server import policy

        status = policy.report()["config_status"]

        assert not status.startswith("loaded from"), (
            "config_status claims the file loaded when it failed to parse: %r"
            % status
        )
        assert "error" in status.lower()
        assert "not valid JSON" in status

    def test_a_good_file_still_says_loaded_from(self, good_config):
        """The control -- the honest branch must keep working, and keep the
        relativised path rather than an absolute one."""
        from naukri_server import policy

        out = policy.report()
        assert out["config_error"] is None
        assert out["config_status"].startswith("loaded from")
        assert not DRIVE_PATH.search(out["config_status"])
        assert "jobhunt.json" in out["config_status"]

    def test_no_file_still_says_no_file_found_and_lists_what_was_tried(
            self, monkeypatch):
        """The third branch. Silence about a missing config is the worst
        failure mode a config system has."""
        import naukri_server.policy as naukri_policy

        monkeypatch.setenv("JOBHUNT_CONFIG", ":none:")
        naukri_policy.invalidate()

        out = naukri_policy.report()
        assert out["source"] is None
        assert "no file found" in out["config_status"]


class TestTheToolBoundaryStaysClean:
    """REGRESSION GUARDS, not reproductions.

    These were GREEN before the fix -- `utils.scrub_result` already held the
    tool boundary. They are here so that the boundary cannot quietly regress
    once the prose is being rendered upstream instead.
    """

    async def test_the_whole_naukri_config_payload_carries_no_path(
            self, broken_config):
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()
        assert_path_absent(result, str(broken_config), "naukri_config()")
        assert_path_absent(result, str(broken_config.parent), "naukri_config()")
        assert_no_absolute_path(result, "naukri_config()")

    async def test_a_narrowed_section_read_is_clean_too(self, broken_config):
        """The `section=` branch composes a different dict and could miss it."""
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config(section="scoring")
        assert_path_absent(result, str(broken_config), "naukri_config(section=)")
        assert_no_absolute_path(result, "naukri_config(section=)")

    async def test_server_info_config_block_is_clean_with_a_broken_file(
            self, broken_config):
        """`naukri_server_info` renders `source` itself. It must not have its
        own copy of this bug."""
        from naukri_server.tools.server_info import naukri_server_info

        result = await naukri_server_info()
        assert_path_absent(result, str(broken_config), "naukri_server_info()")
        assert_no_absolute_path(result, "naukri_server_info()")

    async def test_health_check_surfaces_no_config_prose(self):
        """Confirmed by inspection AND asserted: `naukri_health_check` does not
        publish config_status or config_error at all, so there is no second
        copy of the prose to render. Pinned so adding one is a deliberate act.
        """
        import inspect

        from naukri_server.tools import health

        src = inspect.getsource(health)
        assert "config_status" not in src
        assert "config_error" not in src

    async def test_daily_brief_surfaces_no_config_prose(self):
        import inspect

        from naukri_server.tools import daily_brief

        src = inspect.getsource(daily_brief)
        assert "config_status" not in src
        assert "config_error" not in src


class TestThereIsOnlyOnePlaceAPathIsRendered:

    def test_report_delegates_rendering_rather_than_post_processing(self):
        """The structural half of the fix.

        Passing `display` down and ALSO keeping the hand post-processing would
        pass every test above while leaving two renderers to drift apart. This
        asserts the hand-rolled ones are gone.
        """
        import inspect

        from naukri_server import policy

        src = inspect.getsource(policy.report)
        assert "display=display_path" in src, (
            "report() does not pass the renderer down to jobcore"
        )
        assert 'out["source"] = display_path(' not in src, (
            "source is still being post-processed by hand"
        )
        assert 'out["config_status"] = ' not in src, (
            "config_status is still being overwritten by hand, which is what "
            "discarded config_error"
        )
