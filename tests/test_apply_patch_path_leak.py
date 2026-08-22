"""jobcore's `apply_patch` dict is handed back, and it carries absolute paths.

Error paths are where everyone looks for a leaked path, which is exactly why
this one survived a sweep that had already cleaned every path FIELD beside it:
**it is on the SUCCESS payload.** `jobcore.config.apply_patch` returns
`"path": str(target)` when the write lands, plus `ledger_error` and `detail`
on the failure branches, and `naukri_server.policy.apply_patch` returned that
dict verbatim.

MEASURED at layer 2 on 2026-08-22, against a temp config, all three branches::

    ok      result['path']          C:\\Users\\Dell\\...\\config\\jobhunt.json
    ok      result['ledger_error']  could not append to C:\\Users\\...\\policy_history.jsonl
    error   result['detail']        config file locked by live PID 4242 (lock: C:\\Users\\...)

THE ASSERTIONS LIVE AT LAYER 2, not at the tool, and that is a ruling rather
than a convenience. `naukri_set_config` looks clean today only because
`utils.scrub_result` collapses the path to the bare basename `jobhunt.json` --
under which two candidate config files read identically, out of a WRITE tool
whose response has exactly one question to answer: which file did you change.
A test at the tool would certify a guarantee this module does not provide and
would pass today for the wrong reason. Same ruling `test_config_prose_leak.py`
made for `report()`.

SO EVERY LEAK TEST HERE HAS A TWIN that asserts the field is STILL AN ANSWER --
non-empty, still naming the file, and still telling two candidate config files
apart. Deleting the field would satisfy a leak scanner and fail those.

THE LEDGER AND THE LOCK ARE NOT `source`. They are named from the config's
DIRECTORY, which is why `Loaded.known_paths` includes parent directories and
why the fix keys on it rather than on a hand-rolled `[source]`.

SAFETY. `naukri_set_config` writes a file shared by every server in this
family. No test here calls the MCP tool, and none can reach the real
`config/jobhunt.json`: `conftest._no_ambient_config` pins `JOBHUNT_CONFIG` to
`:none:` for every test, each fixture below re-points it at its own `tmp_path`,
and `_bind_config` PROVES the resolved write target is that temp file before
any test is allowed to write.

All tests are PURE -- temp files only, no network, no browser, no DB.
"""

import json
from pathlib import Path

import pytest

from jobcore import config as jobcore_config

from tests.test_path_leaks import (
    DRIVE_PATH,
    assert_no_absolute_path,
    assert_path_absent,
    contains_path,
)

# IMPORT-TIME GUARD ON THE BORROWED PATTERN, not decoration. A heredoc through
# an agent harness collapses `\\` to `\` in the file it writes; that turned a
# sibling repo's `[\\/]` into `[\/]` -- forward-slash only -- and the resulting
# detector reported CLEAN on a genuine Windows-path leak. In a raw string
# `[\\/]` is exactly TWO backslash characters. One means the collapse happened
# and every absence assertion in this file is worthless, so it fails at import
# where nobody can miss it rather than passing quietly.
assert DRIVE_PATH.pattern.count(chr(92)) == 2, (
    "tests.test_path_leaks.DRIVE_PATH lost a backslash: %r" % DRIVE_PATH.pattern
)
assert DRIVE_PATH.search("D:" + chr(92) + "Sundeep"), DRIVE_PATH.pattern
assert not DRIVE_PATH.search("https://www.naukri.com/job-listings-abc-123")


#: Tier A, unset in a fresh document, so the same patch is accepted by every
#: test here without a ratchet confirmation muddying the branch under test.
PATCH = {"candidate": {"years_experience": 5}}


def _bind_config(monkeypatch, where: Path) -> Path:
    """Create a config under *where*, bind it, and PROVE it is the target.

    The proof is the safety gate, not ceremony: `apply_patch` writes whatever
    `locate()` resolves to, and the file it would otherwise resolve to is the
    one every server in this family shares.
    """
    import naukri_server.policy as naukri_policy

    where.mkdir(parents=True, exist_ok=True)
    cfg = where / "jobhunt.json"
    cfg.write_text(json.dumps({"config_version": 1, "revision": 1}),
                   encoding="utf-8")
    monkeypatch.setenv("JOBHUNT_CONFIG", str(cfg))
    naukri_policy.invalidate()

    located = jobcore_config.locate(naukri_policy._START)
    assert located.found and Path(located.path).resolve() == cfg.resolve(), (
        "REFUSING TO WRITE: apply_patch would target %r, not the temp file %r"
        % (getattr(located, "path", None), cfg)
    )
    return cfg


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A throwaway config in its own directory, bound as THE config."""
    import naukri_server.policy as naukri_policy

    cfg = _bind_config(monkeypatch, tmp_path / "config")
    try:
        yield cfg
    finally:
        naukri_policy.invalidate()


@pytest.fixture
def ledger_blocked(config_file):
    """A DIRECTORY where jobcore wants to append its history file.

    Not a monkeypatch of the write: `ledger.open("a")` against a directory
    raises a real OSError on both platforms (PermissionError on Windows,
    IsADirectoryError on POSIX), so `ledger_error` is produced here by the
    mechanism that produces it in the field, message and all.
    """
    (config_file.parent / jobcore_config.LEDGER_FILENAME).mkdir()
    return config_file


def _doubled(raw: str) -> str:
    """How `repr()` spells *raw* inside an OSError message.

    `OSError.__str__` renders its filename through `repr()`, so a Windows path
    arrives in the `{exc}` half of a composed message with DOUBLED separators
    while the `{path}` half carries the single form. A scrubber that knows one
    spelling relativises half a sentence and publishes the other half. On POSIX
    there are no separators to double and this collapses onto the input, which
    is why the assertions using it stay correct on the Linux runner instead of
    silently checking the same thing twice.
    """
    return raw.replace(chr(92), chr(92) * 2)


# =====================================================================
# 0. The controls -- every assertion below says a path is ABSENT
# =====================================================================
# That shape is only worth anything if the leak demonstrably EXISTS upstream
# and the detector demonstrably FIRES on it. jobcore's own absence assertions
# went green on Linux while measuring nothing on 2026-08-22, and a control
# failing is the only reason anyone noticed.


class TestTheseAssertionsCanFail:

    def test_raw_jobcore_really_does_return_the_absolute_path_on_success(
            self, config_file, tmp_path):
        """CONTROL for the ok branch, and it runs on every OS.

        If jobcore ever started rendering `path` itself, the leak test below
        would pass while proving nothing about this server.
        """
        result = jobcore_config.apply_patch(
            PATCH, path=config_file, actor="test",
            allowed_sections=("candidate", "scoring", "servers.naukri"))
        assert result["status"] == "ok", result
        assert str(tmp_path) in result["path"], (
            "jobcore no longer bakes the absolute path into apply_patch's "
            "success payload; the absence assertions below prove nothing"
        )

    def test_raw_jobcore_really_does_bake_the_ledger_path_into_ledger_error(
            self, ledger_blocked, tmp_path):
        """CONTROL for the ledger branch, in BOTH spellings.

        The `{ledger}` half of `could not append to {ledger}: {exc}` carries the
        single-separator form and the `{exc}` half carries the repr form, out of
        one sentence. Both are asserted so a fix that catches only one is
        visible.
        """
        result = jobcore_config.apply_patch(
            PATCH, path=ledger_blocked, actor="test",
            allowed_sections=("candidate", "scoring", "servers.naukri"))
        assert result["status"] == "ok", result
        assert result["ledger_error"], (
            "the blocked ledger produced no error at all -- the fixture stopped "
            "reproducing the branch it exists for"
        )
        assert str(tmp_path) in result["ledger_error"], result["ledger_error"]
        assert _doubled(str(tmp_path)) in result["ledger_error"], (
            "the repr spelling is gone from the {exc} half: %r"
            % result["ledger_error"]
        )

    def test_jobcores_lock_message_really_names_the_lock_file(self, tmp_path):
        """CONTROL for the lock branch, built from jobcore's REAL exception.

        The message is not invented here -- it is `str(ConfigLockedError(...))`
        -- so the test cannot drift from the text it guards, and if jobcore
        stops naming the lock file this fails rather than going quietly green.
        """
        lock_file = tmp_path / "config" / "jobhunt.json.lock"
        message = str(jobcore_config.ConfigLockedError(4242, lock_file))
        assert str(tmp_path) in message, message
        assert "4242" in message, message

    def test_the_primary_detector_fires_when_the_path_is_present(
            self, tmp_path):
        """CONTROL for the instrument, not for the server.

        `assert_path_absent` is the primary assertion in every test below --
        exact, and therefore not blind on the Linux runner the way a
        drive-letter regex is. It has to be shown firing.
        """
        leaking = {"path": str(tmp_path / "config" / "jobhunt.json")}
        assert contains_path(leaking, str(tmp_path))
        with pytest.raises(AssertionError):
            assert_path_absent(leaking, str(tmp_path))

    def test_the_drive_letter_walker_is_only_a_second_opinion(self):
        """CONTROL recording WHY the exact needle is primary.

        This is what a leaked config path looks like on ubuntu-latest, which is
        where naukri's CI actually gates a merge. The walker cannot see it --
        there is no drive letter to see -- so on that runner it passes without
        detecting anything, and an assertion that cannot fail where it runs
        certifies nothing.
        """
        posix_leak = "could not append to /tmp/pytest-of-runner/pytest-0/x.jsonl"
        assert not DRIVE_PATH.search(posix_leak), (
            "if this starts failing the walker grew POSIX sight and this "
            "control needs rewriting, not deleting"
        )
        assert_no_absolute_path(posix_leak)


# =====================================================================
# 1. The leak, at the layer that actually has it
# =====================================================================


class TestApplyPatchRendersEveryPathItReturns:

    def test_the_success_payload_does_not_leak_the_config_path(
            self, config_file, tmp_path):
        """THE HAPPY PATH, which is the half the earlier sweep walked past."""
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(PATCH, actor="test")

        assert result["status"] == "ok", result
        assert_path_absent(result, str(tmp_path), "apply_patch ok")
        assert_path_absent(result, _doubled(str(tmp_path)), "apply_patch ok")
        assert_no_absolute_path(result, "apply_patch ok")

    def test_the_ledger_error_does_not_leak_the_ledger_path(
            self, ledger_blocked, tmp_path):
        """The PARENT-DIRECTORY case: the ledger is never equal to `source`.

        A substitution keyed on `source` alone cannot touch this string, which
        is the whole reason the fix uses `Loaded.known_paths`.
        """
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(PATCH, actor="test")

        assert result["status"] == "ok", result
        assert result["ledger_error"], result
        assert_path_absent(result, str(tmp_path), "apply_patch ledger")
        assert_path_absent(result, _doubled(str(tmp_path)), "apply_patch ledger")
        assert_no_absolute_path(result, "apply_patch ledger")
        # STILL AN ANSWER: it must still say which file could not be appended to.
        assert "policy_history.jsonl" in result["ledger_error"], result

    def test_a_lock_conflict_does_not_leak_the_lock_file(
            self, config_file, tmp_path, monkeypatch):
        """The other parent-directory case, with jobcore's OWN message text.

        The lock file lives beside the config and is not in `known_paths`
        itself; only its directory is, which is what makes this the test that
        would fail on a `source`-only substitution.
        """
        import naukri_server.policy as naukri_policy

        lock_file = config_file.parent / (config_file.name + ".lock")
        locked = jobcore_config.ConfigLockedError(4242, lock_file)
        monkeypatch.setattr(
            jobcore_config, "apply_patch",
            lambda *a, **kw: {"status": "error", "detail": str(locked),
                              "holder_pid": 4242})

        result = naukri_policy.apply_patch(PATCH, actor="test")

        assert_path_absent(result, str(tmp_path), "apply_patch lock")
        assert_path_absent(result, _doubled(str(tmp_path)), "apply_patch lock")
        assert_no_absolute_path(result, "apply_patch lock")
        # STILL AN ANSWER: which pid holds it, and which lock file.
        assert "4242" in result["detail"], result
        assert "jobhunt.json.lock" in result["detail"], result


# =====================================================================
# 2. Still an answer -- the half a leak scanner cannot see
# =====================================================================
# Deleting `path`, or rendering every candidate as the bare basename
# `jobhunt.json`, passes every assertion above. Both are the degradation
# `display_path` exists to prevent, on the one tool where "which file did you
# just change" is the entire question.


class TestTheAnswerSurvivesTheFix:

    def test_the_success_payload_still_names_the_file_it_wrote(
            self, config_file):
        import naukri_server.policy as naukri_policy

        result = naukri_policy.apply_patch(PATCH, actor="test")

        shown = result["path"]
        assert shown, "the path field was emptied, not relativised"
        assert shown.endswith("config/jobhunt.json"), (
            "collapsed to something that has lost its directory: %r" % shown
        )

    def test_two_different_config_files_do_not_render_identically(
            self, tmp_path, monkeypatch):
        """The bare-basename regression, asserted rather than described.

        Both files are called `jobhunt.json` ON PURPOSE -- that is the case a
        basename collapse cannot tell apart, and the case a reader of a write
        tool most needs told apart.
        """
        import naukri_server.policy as naukri_policy

        first = _bind_config(monkeypatch, tmp_path / "alpha")
        one = naukri_policy.apply_patch(PATCH, actor="test")
        assert one["status"] == "ok", one

        second = _bind_config(monkeypatch, tmp_path / "beta")
        two = naukri_policy.apply_patch(PATCH, actor="test")
        assert two["status"] == "ok", two

        # CONTROL: the two files really are indistinguishable by basename, so
        # this test can only pass by keeping directory information.
        assert first.name == second.name == "jobhunt.json"
        assert one["path"] != two["path"], (
            "two different config files render identically as %r" % one["path"]
        )
        assert one["path"].endswith("alpha/jobhunt.json"), one["path"]
        assert two["path"].endswith("beta/jobhunt.json"), two["path"]


# =====================================================================
# 3. The substitution is exact, and correct output must survive it
# =====================================================================


class TestTheSubstitutionStaysExact:

    def test_a_job_url_and_an_api_route_survive(self, config_file):
        """A loose "looks like a path" rule flagged two CORRECT urls live.

        naukri results are full of `https://www.naukri.com/...` and of API
        routes like `/jobapi/v3/search`. `relativise_known` only ever replaces
        strings the snapshot already knows are paths, and this pins that -- a
        scrubber that mangles correct fields does more damage than the leak it
        was written for.
        """
        import naukri_server.policy as naukri_policy

        snap = naukri_policy.snapshot()
        for text in (
            "https://www.naukri.com/job-listings-node-developer-abc-123",
            "GET /jobapi/v3/search returned 500",
            "http://localhost:8765/preview",
        ):
            assert naukri_policy.relativise_known_paths(text, snap) == text, text

    def test_but_a_known_path_in_the_same_shape_of_sentence_is_replaced(
            self, config_file, tmp_path):
        """CONTROL for the test above: the substitution is not simply inert."""
        import naukri_server.policy as naukri_policy

        snap = naukri_policy.snapshot()
        sentence = "cannot read %s: [Errno 13] Permission denied" % config_file
        rendered = naukri_policy.relativise_known_paths(sentence, snap)
        assert rendered != sentence, "nothing was substituted at all"
        assert_path_absent(rendered, str(tmp_path), "relativise_known_paths")
        assert "jobhunt.json" in rendered, rendered

    def test_non_strings_pass_through_untouched(self, config_file):
        """`relativise_mapping` maps over a MIXED payload -- ints, None, dicts.

        Pinned because the fix runs over jobcore's whole return, which carries
        `revision`, `changed` and `holder_pid` beside the three path fields.
        """
        import naukri_server.policy as naukri_policy

        snap = naukri_policy.snapshot()
        payload = {"revision": 2, "ledger_error": None,
                   "changed": {"candidate.years_experience": [None, 5]}}
        assert naukri_policy.relativise_mapping(payload, snap) == payload
        assert naukri_policy.relativise_mapping("not a dict", snap) == "not a dict"
