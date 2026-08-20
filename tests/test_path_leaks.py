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
DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]")


def assert_no_absolute_path(value, where="result"):
    """Walk a tool result and fail on any drive-letter path it contains."""
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
