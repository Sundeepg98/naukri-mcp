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
import os
from pathlib import Path

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


# =====================================================================
# The needle has two spellings
# =====================================================================


def _doubled(path: str) -> str:
    """The repr() spelling of a path, which is what lands inside an error.

    `OSError.__str__` renders its filename through `repr()`, so on Windows the
    path arrives with DOUBLED separators:

        str(OSError(13, "Permission denied", r"D:\a\b.json"))
        -> "[Errno 13] Permission denied: 'D:\\a\\b.json'"

    On POSIX the two spellings are identical and this is a no-op.
    """
    return path.replace("\\", "\\\\")


class TestAPathSpelledByReprIsStillThatPath:
    """One sentence, two halves, two opposite verdicts.

    Found live by the uplers slice 2026-08-22 and fixed in jobcore 6acc7e6.
    `jobcore.config` composes `f"cannot read {path}: {exc}"` -- the `{path}`
    half with single separators, the `{exc}` half through `OSError.__str__`,
    which uses repr() and therefore doubles them. `relativise_known` searched
    only for the single spelling, so it relativised the first half and passed
    the second half through with the machine's full layout intact.

    WHY IT HID, and this is the part worth keeping: this file made exact
    substring matching the PRIMARY detector precisely because a drive-letter
    regex cannot fire on Linux -- and the exact detector was itself blind to the
    repr spelling on Windows. Between the two there was a window in which
    nothing was looking on either platform. Two detectors, each covering the
    other's blind spot, both blind to the same thing.

    naukri inherits the fix through `snap.report(SERVER_NAME, display=...)`;
    these tests assert it at naukri's own boundaries rather than trusting it.

    GREEN ON ARRIVAL, AND SAYING SO PLAINLY. These tests were green the moment
    they were written here and never went red on this box. That is not a weak
    test -- it is the local/CI asymmetry running the other way. naukri's venv
    has jobcore installed EDITABLE from ../jobcore, so it is already running
    6acc7e6 with the repr fix no matter what requirements-ci.txt says, while CI
    installs the PINNED commit and would not have it. The defect is real on the
    runner and unreachable here. Exactly inverted from the stale-pin break of
    the same day, and the same root cause.

    The red-first receipt for the fix itself is jobcore's, not naukri's:
    `jobcore/tests/test_report_display.py::TestAPathSpelledByReprIsTheSamePath
    ::test_both_spellings_are_relativised`, run red against jobcore at 6a5d68e.
    What THIS class buys is the property pinned at naukri's boundaries, so a
    future pin that loses the fix is caught here rather than in a shared
    transcript.
    """

    @pytest.fixture
    def unreadable_config(self, tmp_path, monkeypatch):
        """A config file that EXISTS but cannot be read.

        The OSError is CONSTRUCTED rather than provoked, because provoking a
        real permission failure is not portable -- but it uses the real
        3-argument form, so `str()` of it is byte-identical to what a genuinely
        failed open produces. Mirrors jobcore's own fixture deliberately: same
        scenario, asserted at naukri's layers.
        """
        import naukri_server.policy as naukri_policy

        cfg = tmp_path / "jobhunt.json"
        cfg.write_text("{}", encoding="utf-8")

        real = Path.read_bytes

        def boom(self):
            if self == cfg:
                raise OSError(13, "Permission denied", str(cfg))
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", boom)
        monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
        naukri_policy.invalidate()
        try:
            yield cfg
        finally:
            naukri_policy.invalidate()

    def test_the_repr_spelling_is_the_one_that_actually_appears(
            self, unreadable_config):
        """THE CONTROL. Pins that the scenario really does produce both
        spellings, so the absence assertions below are known falsifiable."""
        from naukri_server import policy

        err = policy.snapshot().config_error or ""
        assert "cannot read" in err, err
        assert "Permission denied" in err

        if os.sep == "\\":
            assert str(unreadable_config) in err, (
                "the {path} half should use single separators")
            assert _doubled(str(unreadable_config)) in err, (
                "the {exc} half should use repr() separators -- THIS is the "
                "half a single-spelling detector cannot see")

    def test_policy_report_carries_neither_spelling(self, unreadable_config):
        """THE LAYER THAT WAS ACTUALLY WRONG.

        `policy.report()` is unscrubbed -- no tool wrapper below it -- so this
        is where the doubled spelling would survive intact.
        """
        from naukri_server import policy

        out = policy.report()
        cfg = str(unreadable_config)

        assert_path_absent(out, cfg, "policy.report() [single]")
        assert_path_absent(out, _doubled(cfg), "policy.report() [repr]")
        assert_no_absolute_path(out, "policy.report()")

    async def test_the_tool_payload_carries_neither_spelling(
            self, unreadable_config):
        r"""The tool boundary. Expected to survive already, because
        `utils._DRIVE_PATH_RE` matches `D:\` regardless of what follows and
        reduces it to a basename -- but that is DEGRADED-not-leaking, not a
        pass, so it is asserted rather than assumed."""
        from naukri_server.tools.config_tool import naukri_config

        result = await naukri_config()
        cfg = str(unreadable_config)

        assert_path_absent(result, cfg, "naukri_config() [single]")
        assert_path_absent(result, _doubled(cfg), "naukri_config() [repr]")
        assert_no_absolute_path(result, "naukri_config()")

    def test_the_error_is_still_an_answer(self, unreadable_config):
        """Leak-free is half the requirement; it must still be readable."""
        from naukri_server import policy

        err = policy.report()["config_error"] or ""
        assert "jobhunt.json" in err, "which file failed was scrubbed away"
        assert "Permission denied" in err, "why it failed was scrubbed away"
        assert "/" in err, (
            "collapsed to a bare basename (%r) -- the reader cannot tell which "
            "jobhunt.json" % err)


# =====================================================================
# Can that guard actually fail?
# =====================================================================

# TRAP GUARD, EVALUATED AT IMPORT AND DELIBERATELY NOT A TEST.
#
# Measured on 2026-08-22: a bash heredoc through an agent harness COLLAPSES
# `\\` to `\` in the file it writes. It turned the character class `[\\/]`
# into `[\/]` -- forward slash only -- and a drive-letter detector then
# reported CLEAN on a genuine Windows leak. Every backslash literal below is
# load-bearing for exactly that reason, so a future mangling must raise on
# import rather than quietly certify nothing. Collapsing `"\\"` yields an
# unterminated string and a SyntaxError; collapsing `"\\\\"` is caught here.
assert len("\\") == 1, "backslash literals in this file have been mangled"
assert len("\\\\") == 2, "backslash literals in this file have been mangled"
assert _doubled("a\\b") == "a\\\\b", (
    "_doubled no longer produces the repr() spelling -- every absence "
    "assertion in this file that uses it is now measuring nothing"
)


def test_an_oserror_spells_its_filename_with_doubled_backslashes(tmp_path):
    """The doubling is a property of CPYTHON, not of this server.

    `OSError.__str__` renders its `filename` through `repr()`, and `repr()` of
    a str escapes backslashes. That single fact is the entire reason
    `jobcore.paths._spellings` exists and the reason this file carries a second
    needle at all -- so it is pinned here rather than assumed forever.

    A future Python that stops repr-ing the filename would make every doubled
    spelling in this file dead weight. This test fails loudly on that day, so
    the extra spellings can be retired knowingly instead of lingering as cargo
    nobody dares remove.

    The doubling assertion is guarded on `os.sep` because it is only reachable
    where separators ARE backslashes. On POSIX the two spellings are the same
    string, which is asserted rather than skipped past.
    """
    cfg = tmp_path / "jobhunt.json"
    rendered = str(OSError(13, "Permission denied", str(cfg)))

    assert "Permission denied" in rendered
    assert "jobhunt.json" in rendered

    if os.sep == "\\":
        assert _doubled(str(cfg)) in rendered, (
            "OSError no longer spells its filename through repr(): %r"
            % rendered
        )
        assert str(cfg) not in rendered, (
            "the SINGLE spelling now appears too -- a single-spelling "
            "scrubber would start working again and _spellings could go"
        )
    else:
        assert str(cfg) in rendered
        assert _doubled(str(cfg)) == str(cfg), (
            "a POSIX path grew a backslash; the two spellings no longer "
            "collapse and this platform now needs the second needle too"
        )


def _relativise_known_without_the_repr_fix(text, *, known, render):
    """`jobcore.paths.relativise_known` EXACTLY as it stood before 6acc7e6.

    Taken from `git show 6acc7e6 -- src/jobcore/paths.py`: the fix added the
    `_spellings` helper and rebuilt this loop around it, and changed nothing
    else in the function. So this body is the real pre-fix behaviour -- one
    pass over the SINGLE spelling of each known path -- rather than an invented
    failure mode that never shipped.
    """
    if not isinstance(text, str) or not text:
        return text
    # Longest first: one searched path is often a prefix of another, and
    # replacing the short one first would leave a mangled tail behind.
    for raw in sorted({str(k) for k in known if k}, key=len, reverse=True):
        if raw in text:
            text = text.replace(raw, str(render(raw)))
    return text


class TestTheReprGuardCanActuallyFail:
    """THE CONTROL for `TestAPathSpelledByReprIsStillThatPath` above.

    That class is GREEN ON ARRIVAL and says so plainly: naukri's venv installs
    jobcore EDITABLE from `../jobcore`, so the fix is present on this box no
    matter what `requirements-ci.txt` pins, and those assertions have never
    been seen failing here. A guard nobody has watched fail certifies nothing,
    and a suite of such guards manufactures confidence at scale -- so this
    class makes the guard fail on purpose and asserts that it does.

    WHAT IS NEUTRALISED, AND WHERE. `jobcore.paths.relativise_known` is
    replaced, for the duration of one test, by its own pre-6acc7e6 body above.
    Two properties make that the stable hook rather than a lucky one:

    * it is jobcore's PUBLIC surface -- `relativise_known` is named in
      `jobcore.paths.__all__`, so it carries a compatibility promise that the
      private `_spellings` does not. A permanent control coupled to a private
      helper breaks on any upstream rename, and that break would arrive
      looking exactly like a regression;
    * `jobcore.config.Loaded.report` does `from .paths import relativise_known`
      INSIDE the function, so patching the module attribute is picked up at
      call time. If that import is ever hoisted to module scope the patch stops
      biting -- and this class fails, because the guard it expects to fire
      would not. That is the intended behaviour, not a flake.

    WHY IT IS WORTH A PERMANENT SLOT. The closure lives in a dependency PIN
    (`requirements-ci.txt` -> jobcore 6acc7e6), not in this repo. Roll the pin
    back or revert the upstream fix and naukri regresses in silence; nothing
    else in 2600+ tests looks at this. The guard above catches that. This class
    is what keeps the guard honest about being able to catch it.

    HOW TO RETIRE IT KNOWINGLY. If jobcore's repr handling ever moves into
    naukri, or the pin stops being the way the fix arrives, this control has
    nothing left to neutralise and should be deleted along with the shim. If
    instead `relativise_known` is renamed or its signature changes, the correct
    edit is to RE-POINT the shim, never to delete the class.
    """

    @pytest.fixture
    def unreadable_config(self, tmp_path, monkeypatch):
        """A config file that EXISTS but cannot be read.

        Deliberately a copy of the sibling class's fixture rather than a shared
        one: this control has to reproduce that guard's exact scenario, and a
        fixture edited for one of them while the other silently follows along
        is how a control quietly stops controlling anything.

        `Path.read_bytes` is patched NARROWLY -- one file, everything else
        real. A genuine OS-level lock is not an option: it would not reproduce
        on the Linux runner, where this suite actually gates a merge.
        """
        import naukri_server.policy as naukri_policy

        cfg = tmp_path / "jobhunt.json"
        cfg.write_text("{}", encoding="utf-8")

        real = Path.read_bytes

        def boom(self):
            if self == cfg:
                raise OSError(13, "Permission denied", str(cfg))
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", boom)
        monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
        naukri_policy.invalidate()
        try:
            yield cfg
        finally:
            naukri_policy.invalidate()

    @pytest.mark.skipif(
        os.sep != "\\",
        reason="the repr spelling only differs from the plain one where "
               "separators are backslashes; the collapse is asserted by "
               "test_on_posix_the_two_spellings_are_the_same_string",
    )
    def test_the_guard_fires_when_the_repr_fix_is_neutralised(
            self, unreadable_config, monkeypatch):
        """Green -> RED -> green, with the fix as the only variable.

        The sequence is the whole point. Asserting the guard passes BEFORE the
        shim rules out a broken `assert_path_absent` that raises on everything
        (which would make `pytest.raises` below pass for no reason), and
        asserting it passes again AFTER rules out the fixture having leaked on
        its own.
        """
        import jobcore.paths as jobcore_paths

        from naukri_server import policy

        cfg = str(unreadable_config)
        repr_cfg = _doubled(cfg)
        assert repr_cfg != cfg, "the two spellings must differ here"

        # 1. With the fix present -- the guard passes, and the field is still
        #    an answer. A "fix" that empties config_error would satisfy every
        #    absence assertion in this file and help nobody.
        before = policy.report()
        assert_path_absent(before, repr_cfg, "policy.report() [repr, fix in]")
        err_before = before["config_error"] or ""
        assert "jobhunt.json" in err_before, "which file failed is gone"
        assert "Permission denied" in err_before, "why it failed is gone"

        # 2. With the fix neutralised -- the guard's own assertion must raise.
        with monkeypatch.context() as m:
            m.setattr(jobcore_paths, "relativise_known",
                      _relativise_known_without_the_repr_fix)
            during = policy.report()

            with pytest.raises(AssertionError) as caught:
                assert_path_absent(during, repr_cfg, "policy.report() [repr]")
            # `assert_path_absent` formats the needle with `%r`, and repr()
            # doubles backslashes -- the same CPython behaviour that caused
            # this defect now shapes the message reporting it, so the needle
            # appears in the failure text DOUBLY doubled. Matching on
            # `repr(repr_cfg)` is the exact form and says why it is exact.
            assert repr(repr_cfg) in str(caught.value), (
                "the guard fired but did not name the path it caught, which "
                "is the half of a failure message a reader acts on"
            )

            # ONE SENTENCE, TWO HALVES, TWO OPPOSITE VERDICTS -- the shape that
            # made this defect survive being looked at. The {path} half is
            # still correctly relativised by the very same call.
            assert_path_absent(during, cfg,
                               "policy.report() [single, fix neutralised]")

        # 3. Fix restored -- green again, so step 2 was caused by the shim.
        after = policy.report()
        assert_path_absent(after, repr_cfg, "policy.report() [repr, fix back]")

    @pytest.mark.skipif(
        os.sep != "\\",
        reason="the drive-letter walker cannot fire without a drive letter",
    )
    def test_the_second_opinion_also_fires_when_the_fix_is_neutralised(
            self, unreadable_config, monkeypatch):
        """The corroborating detector is not dead either.

        `assert_path_absent` is the PRIMARY assertion because it works on
        Linux; `assert_no_absolute_path` is kept as a second opinion. A second
        opinion that cannot dissent is decoration, so it gets the same
        treatment as the primary.
        """
        import jobcore.paths as jobcore_paths

        from naukri_server import policy

        assert_no_absolute_path(policy.report(), "policy.report() [fix in]")

        with monkeypatch.context() as m:
            m.setattr(jobcore_paths, "relativise_known",
                      _relativise_known_without_the_repr_fix)
            during = policy.report()

            with pytest.raises(AssertionError) as caught:
                assert_no_absolute_path(during, "policy.report()")
            # Which prose field it names first is dict-insertion order, not a
            # property worth pinning -- `config_status` is derived from
            # `config_error` and both carry the leak. What matters is that it
            # fired on a PROSE field and not on some unrelated corner.
            named = str(caught.value)
            assert ("config_status" in named or "config_error" in named), (
                "the walker fired somewhere other than the prose fields this "
                "control is about: %s" % named
            )
            assert DRIVE_PATH.search(during["config_error"] or ""), (
                "config_error itself is clean, so the walker's dissent came "
                "from somewhere this control did not neutralise"
            )

    def test_on_posix_the_two_spellings_are_the_same_string(self):
        """Why the two tests above skip off Windows -- asserted, not asserted
        in a comment.

        `_spellings` collapses to a single entry when a path holds no
        backslash, so the pre-fix body and the fixed one are indistinguishable
        there and the guard genuinely cannot be made to fail. This runs on both
        platforms so the reason stays true rather than merely stated.
        """
        posix_shaped = "/home/runner/work/naukri/naukri/config/jobhunt.json"
        assert _doubled(posix_shaped) == posix_shaped

        windows_shaped = "D:\\Sundeep\\config\\jobhunt.json"
        assert _doubled(windows_shaped) != windows_shaped
        assert "\\\\" in _doubled(windows_shaped)
