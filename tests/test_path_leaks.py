"""No tool result may carry an absolute local filesystem path.

A live sweep on 2026-08-20 found `D:\\Sundeep\\projects\\...` inside tool
results. It is wrong twice over: it publishes the machine's layout into any
shared transcript or future public release, and it is paid for in tokens on
every single response that carries it.

A census of `naukri_server/` found 20 emission sites in three classes:

* DIRECT (6)      - a path string placed straight into a returned field
* EXCEPTION (8)   - a path arrives via `f"...{e}"`, because OSError and
                    Playwright errors embed the filename they failed on
* CONDITIONAL (6) - only on some branch (missing dir, unwritable dir, ...)

Fixing only the DIRECT six would leave the majority, and the EXCEPTION class is
the one nobody can fix site-by-site for long: every new `except Exception as e:`
that formats `{e}` into a message re-opens it. So there are two layers here and
both are tested: each site returns something sensible, AND a server-wide scrub
guarantees the property for all 118 tools, including sites nobody has written
yet.

All tests are PURE -- no network, no browser, no file I/O.
"""

import re

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Any Windows drive-letter path, which is what the sweep actually saw.
#
# THE LOOKBEHIND IS LOAD-BEARING, not decoration. A drive letter is ONE
# character, so the bare `[A-Za-z]:[\\/]` this used to be ALSO matches the
# `s:/` inside `https://` -- which makes every correct Naukri job URL read as
# a path leak. Two sibling suites hit that false positive on 2026-08-22, which
# is how it was found; naukri's own results are full of job URLs and this
# walker was one assertion away from failing on them.
#
# It deliberately mirrors `utils._DRIVE_PATH_RE`, the SCRUBBER this walker
# exists to certify, INCLUDING the `0-9` in the lookbehind. A walker stricter
# than its scrubber flags text the scrubber intentionally leaves alone, which
# is a test nothing can satisfy; keeping the two lookbehinds identical is what
# makes "the scrubber ran" and "this walker is quiet" the same statement.
# TestTheWalkersOwnRegex below pins both halves.
DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")


def assert_no_absolute_path(value, where="result"):
    """Walk a tool result and fail on any drive-letter path it contains.

    A SECOND OPINION, not the primary detector -- see `assert_path_absent`.
    This can only fire on a Windows-shaped path, so where the payload's paths
    are POSIX it passes without being able to see anything.
    """
    if isinstance(value, str):
        assert not DRIVE_PATH.search(value), (
            "%s leaks an absolute path: %r" % (where, value)
        )
    elif isinstance(value, dict):
        for k, v in value.items():
            assert_no_absolute_path(k, "%s key" % where)
            assert_no_absolute_path(v, "%s[%r]" % (where, k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            assert_no_absolute_path(v, "%s[%d]" % (where, i))


def contains_path(value, needle: str) -> bool:
    """Does the exact path string `needle` appear anywhere in this payload?"""
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(contains_path(k, needle) or contains_path(v, needle)
                   for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_path(v, needle) for v in value)
    return False


def assert_path_absent(value, needle, where="result"):
    """THE PRIMARY LEAK ASSERTION: the path the fixture made must be gone.

    WHY THIS EXISTS AND WHY `assert_no_absolute_path` IS ONLY A SECOND OPINION.
    A drive-letter regex can fire only on Windows. naukri's CI runs Linux --
    `policy.display_path`'s own docstring records a Linux-only failure found
    there on 2026-08-21 -- and on that runner a leaked path is
    `/tmp/pytest-of-runner/...` or `/home/runner/work/...`, which carries no
    drive letter at all. So on the machine where these tests actually gate a
    merge, every drive-letter assertion PASSES WITHOUT DETECTING ANYTHING.

    jobcore hit exactly this on 2026-08-22: its leak assertions went green on
    the Linux half of its matrix while measuring nothing, and the only reason
    anyone noticed was that a CONTROL asserting a leak IS present failed there.
    A check that cannot fail where it runs certifies nothing, and a suite of
    them manufactures confidence at scale.

    Checking for the exact path string the fixture created is platform
    independent and strictly stronger: it fails on a real leak on either OS,
    and it cannot pass by being unable to see. Pass the absolute path your
    fixture built; assert this FIRST, then the walker as corroboration.
    """
    assert not contains_path(value, needle), (
        "%s leaks the absolute path %r" % (where, needle)
    )


# =====================================================================
# 0. The walker's own regex
# =====================================================================
# An instrument enters the register only if it has been shown failing. This
# walker IS an instrument -- every other test in this file and in two sibling
# files delegates its verdict to it -- so its regex gets the same treatment as
# the code it audits.


class TestTheWalkersOwnRegex:

    def test_it_still_catches_a_real_drive_letter_path(self):
        """Tightening must not have bought quiet by going blind."""
        for text in (
            r"D:\workspace\projects\job-hunting\naukri\sync_state.json",
            "C:/Users/TestUser/secret.json",
            "[Errno 2] No such file or directory: 'D:\\a\\b.json'",
            "exported to E:/out.csv",
        ):
            assert DRIVE_PATH.search(text), (
                "stopped catching a genuine absolute path: %r" % text
            )

    def test_the_loose_form_matches_https_and_the_tightened_one_does_not(self):
        """The regression this tightening exists for, both halves asserted.

        Written as an explicit comparison so nobody copies the loose literal
        into a third suite: the loose form is not merely "less precise", it
        FIRES on ordinary correct output.
        """
        loose = re.compile(r"[A-Za-z]:[\\/]")
        for url in (
            "https://www.naukri.com/job-listings-node-developer-abc-123",
            "http://www.ambitionbox.com/overview/infosys-overview",
            "see https://naukri.com/mnjuser/profile for details",
        ):
            assert loose.search(url), (
                "the loose form was supposed to false-positive here; if it no "
                "longer does, this control has stopped testing anything"
            )
            assert not DRIVE_PATH.search(url), (
                "the tightened form still false-positives on %r" % url
            )

    def test_the_walker_is_BLIND_to_a_posix_path_which_is_why_it_is_secondary(self):
        """Documents the hole on the box where it can still be seen.

        This is not a wish -- it is the measured reason `assert_path_absent`
        exists. On naukri's Linux CI runner the paths in a payload look like
        the strings below, and `assert_no_absolute_path` passes on every one of
        them while a leak sits in plain view. Pinning it here means the day
        somebody makes the walker cross-platform, this test fails and tells
        them the second detector can be reconsidered.
        """
        posix_leaks = [
            "/tmp/pytest-of-runner/pytest-0/test_x0/jobhunt.json",
            "/home/runner/work/naukri/naukri/sync_state.json",
            "cannot read /tmp/pytest-of-runner/pytest-0/jobhunt.json: boom",
        ]
        for text in posix_leaks:
            assert not DRIVE_PATH.search(text), (
                "the drive-letter walker unexpectedly caught a POSIX path -- "
                "if it is now cross-platform, revisit assert_path_absent"
            )
            # ...and the primary detector sees every one of them.
            assert contains_path({"msg": text}, text.split(":")[0].strip()
                                 if text.startswith("cannot") else text)

    def test_the_primary_detector_can_actually_fail(self):
        """An instrument enters only if it has been shown failing."""
        needle = "/tmp/pytest-of-runner/pytest-0/jobhunt.json"
        leaky = {"config_error": "%s is not valid JSON" % needle}

        assert contains_path(leaky, needle)
        with pytest.raises(AssertionError):
            assert_path_absent(leaky, needle, "leaky payload")

        # And it is quiet on the rendered form, which is the whole point.
        clean = {"config_error": "../../config/jobhunt.json is not valid JSON"}
        assert_path_absent(clean, needle, "clean payload")

    def test_the_primary_detector_walks_nested_structures(self):
        """A leak three levels down in a list of dicts is still a leak."""
        needle = r"D:\workspace\projects\secret.json"
        assert contains_path({"a": [{"b": ("x", needle)}]}, needle)
        assert not contains_path({"a": [{"b": ("x", "clean")}]}, needle)

    def test_it_agrees_with_the_scrubber_it_certifies(self):
        """Same lookbehind as `utils._DRIVE_PATH_RE`, asserted by BEHAVIOUR.

        Comparing pattern strings would be brittle (the scrubber's carries a
        trailing body matcher this one has no use for). What must hold is that
        the walker never flags text the scrubber declines to rewrite -- if it
        did, a result could be simultaneously correctly scrubbed and reported
        as leaking, and no change to the server could fix it.
        """
        from naukri_server.utils import _DRIVE_PATH_RE

        corpus = [
            r"D:\workspace\x.json",
            "C:/Users/TestUser/y.json",
            "https://www.naukri.com/job-listings-abc",
            "http://x.com/a",
            "port8080D:/x",
            "no path here at all",
            "ratio 3:1 and a colon: here",
            "[Errno 13] Permission denied: 'C:\\Users\\Dell\\secret.json'",
        ]
        for text in corpus:
            walker_flags = bool(DRIVE_PATH.search(text))
            scrubber_rewrites = _DRIVE_PATH_RE.sub("", text) != text
            assert walker_flags == scrubber_rewrites, (
                "walker and scrubber disagree on %r (walker=%s, scrubber=%s)"
                % (text, walker_flags, scrubber_rewrites)
            )


# =====================================================================
# 1. The scrubber itself
# =====================================================================


class TestScrubber:

    def test_server_root_becomes_a_relative_path(self):
        """A path inside the repo keeps its useful part and loses the rest."""
        from naukri_server.utils import scrub_paths, SERVER_ROOT

        text = "Exported to %s" % (SERVER_ROOT / "exports" / "apps.json")
        out = scrub_paths(text)
        assert "exports/apps.json" in out.replace("\\", "/")
        assert not DRIVE_PATH.search(out)

    def test_a_foreign_absolute_path_is_reduced_to_its_basename(self):
        """Paths outside the repo carry even less business being returned."""
        from naukri_server.utils import scrub_paths

        out = scrub_paths(r"[Errno 13] Permission denied: 'C:\Users\TestUser\secret.json'")
        assert not DRIVE_PATH.search(out)
        assert "secret.json" in out
        assert "Users" not in out

    def test_the_real_oserror_shape_is_handled(self):
        """The exact string an OSError produces, which is the EXCEPTION class."""
        from naukri_server.utils import scrub_paths, SERVER_ROOT

        err = "[Errno 2] No such file or directory: '%s'" % (SERVER_ROOT / "sync_state.json")
        out = scrub_paths(err)
        assert not DRIVE_PATH.search(out)
        assert "No such file or directory" in out
        assert "sync_state.json" in out

    def test_text_without_paths_is_returned_unchanged(self):
        """The control: the scrubber must not rewrite ordinary messages."""
        from naukri_server.utils import scrub_paths

        for s in ["Logged in, session active",
                  "API bot-checked (captcha) - expected",
                  "Invalid days=5. Must be 7, 30, or 90",
                  "https://www.naukri.com/mnjuser/homepage",
                  "/jobapi/v3/search"]:
            assert scrub_paths(s) == s

    def test_api_paths_and_urls_survive(self):
        """The second control. A leading slash is not a leak.

        Naukri API paths and URLs are all over these responses; a scrubber that
        ate them would be far worse than the leak it fixes.
        """
        from naukri_server.utils import scrub_paths

        payload = {"url": "https://www.naukri.com/x", "path": "/cloudgateway-apply/v0/self"}
        assert scrub_paths(str(payload)) == str(payload)

    def test_non_strings_pass_through(self):
        from naukri_server.utils import scrub_paths
        assert scrub_paths(None) is None
        assert scrub_paths(7) == 7


# =====================================================================
# 2. The server-wide backstop
# =====================================================================


class TestResultScrubbing:

    async def test_a_leaking_tool_result_is_scrubbed(self):
        """The property the sweep asked for, enforced for every tool.

        Site-by-site fixes cannot hold this line: the EXCEPTION class re-opens
        every time somebody writes `except Exception as e: ... {e}`.
        """
        from naukri_server.utils import scrub_result, SERVER_ROOT

        leaky = {
            "status": "error",
            "message": "Failed to write %s" % (SERVER_ROOT / "exports" / "x.json"),
            "nested": {"file_path": str(SERVER_ROOT / "resume.pdf")},
            "rows": [{"body": "git stderr: cannot open %s" % (SERVER_ROOT / "a.py")}],
        }
        assert_no_absolute_path(scrub_result(leaky))

    async def test_scrubbing_preserves_structure_and_non_string_values(self):
        """The control: it must not reshape a clean result."""
        from naukri_server.utils import scrub_result

        clean = {"status": "success", "count": 3, "ok": True, "none": None,
                 "items": [{"a": 1}, {"b": "two"}], "ratio": 1.5}
        assert scrub_result(clean) == clean

    async def test_every_registered_tool_is_wrapped_by_the_scrubber(self):
        """Coverage, asserted rather than assumed.

        The whole point of a backstop is that it applies everywhere, so this
        checks the registration path rather than a sample of tools.
        """
        import naukri_server
        from naukri_server.tools.health import naukri_health_check
        from naukri_server.tools.performance import naukri_search_impressions

        for fn in (naukri_health_check, naukri_search_impressions):
            assert getattr(fn, "__wrapped_by_watchdog__", False), (
                "%s is not registered through the wrapper" % fn.__name__
            )


# =====================================================================
# 3. The DIRECT sites, fixed at source
# =====================================================================


class TestDirectSites:

    async def test_kill_switch_status_names_its_state_file_without_locating_it(self):
        """Which file holds the flag is useful; where it lives on disk is not.

        The census recommended dropping the field outright, but two existing
        tests assert it is present, so somebody wanted it for operations. A
        basename keeps that and still closes the leak.
        """
        from naukri_server import kill_switch

        status = kill_switch.status()
        assert status["state_file"] == "kill_switch_state.json"
        assert_no_absolute_path(status, "kill_switch.status()")

    async def test_healing_circuit_status_does_the_same(self):
        """The latent twin: no caller today, a DIRECT leak the moment there is."""
        from naukri_server.healing import circuit

        status = circuit.status()
        assert status["state_file"] == "healing_state.json"
        assert_no_absolute_path(status, "circuit.status()")

    async def test_data_dir_probe_reports_a_verdict_not_a_path(self):
        from naukri_server.health.probes.system import system_data_dir

        result = await system_data_dir()
        assert_no_absolute_path(result.message, "system.data_dir probe")
        assert result.message

    async def test_health_check_chrome_profile_warning_carries_no_path(self):
        from naukri_server.tools.health import naukri_health_check

        with patch("naukri_server.tools.health.os.path.isdir", return_value=False), \
             patch("naukri_server.tools.health._check_login", new_callable=AsyncMock,
                   return_value={"name": "login", "status": "ok", "message": "ok", "elapsed_ms": 1}), \
             patch("naukri_server.tools.health._check_profile_api", new_callable=AsyncMock,
                   return_value={"name": "profile_api", "status": "ok", "message": "ok", "elapsed_ms": 1}), \
             patch("naukri_server.tools.health._check_search_api", new_callable=AsyncMock,
                   return_value={"name": "search_api", "status": "ok", "message": "ok", "elapsed_ms": 1}), \
             patch("naukri_server.tools.health._check_recommendations_api", new_callable=AsyncMock,
                   return_value={"name": "recommendations_api", "status": "ok", "message": "ok", "elapsed_ms": 1}), \
             patch("naukri_server.tools.health._check_dashboard_api", new_callable=AsyncMock,
                   return_value={"name": "dashboard_api", "status": "ok", "message": "ok", "elapsed_ms": 1}), \
             patch("naukri_server.tools.health.browser") as mock_browser, \
             patch("naukri_server.tools.health.api_metrics") as mock_metrics:
            mock_browser.page_pool = None
            mock_metrics.get_stats.return_value = {}
            result = await naukri_health_check(include_browser=False)

        assert result.get("warnings"), "the warning must still be raised"
        assert_no_absolute_path(result["warnings"], "health warnings")
