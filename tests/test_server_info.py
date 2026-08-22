"""`naukri_server_info()` must answer WHICH CODE, not merely "I am alive".

A fix committed to disk changes nothing for a process that is already up. On
2026-08-21 that cost real time: a bug was diagnosed as a regression and an agent
dispatched to re-fix it, and the fix was already on disk -- the PROCESS was
stale. Every check available at the time was a behavioural fingerprint, which
cannot separate "the code is wrong" from "the code is old".

THE TEST THAT MATTERS is `test_the_stamp_is_not_re_resolved_per_call`. A tool
that shells out to git on each call reports the commit currently on DISK, and a
stale process would then confirm its own freshness -- strictly worse than
reporting nothing, because the answer looks like evidence. So the stamp is a
module constant resolved at import, and this file proves it by making
`subprocess.run` raise on any use and calling the tool twice.

`build.jobcore` is stamped separately and on purpose: the scoring engine, the
config loader and the path renderer live in jobcore, installed editable from a
sibling checkout that moves on its own schedule. A stale jobcore is exactly as
invisible as a stale naukri.

All tests are PURE -- no network, no browser, no live MCP server.
"""

import shutil
import subprocess

import pytest

from tests.test_path_leaks import DRIVE_PATH, assert_no_absolute_path

BUILD_STAMP_KEYS = {
    "commit", "commit_full", "branch", "committed_at", "dirty", "dirty_files",
    "resolved_at", "source", "detail",
}


def _git_head(cwd) -> str:
    """`git rev-parse HEAD` on disk -- the honest counterpart to a held stamp."""
    out = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0, "git rev-parse failed: %s" % out.stderr
    return out.stdout.strip()


needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="no git executable on PATH")


class TestTheStampIsFrozen:

    async def test_the_stamp_is_not_re_resolved_per_call(self, monkeypatch):
        """The load-bearing property: the request path touches NO git.

        `jobcore.buildinfo` reaches git only through `subprocess.run`, so making
        that raise turns any per-call resolution into a hard failure. A frozen
        module constant never notices.

        Proven red before the fix by temporarily making the tool call
        `jobcore.buildinfo.resolve(SERVER_ROOT)` instead of reading the
        constant -- see the handoff for that output.
        """
        import jobcore.buildinfo as bi
        from naukri_server.tools.server_info import naukri_server_info

        def _explode(*args, **kwargs):
            raise AssertionError(
                "git was shelled out to on a request path -- the stamp is "
                "being re-resolved per call, so a stale process would report "
                "the commit on DISK and confirm its own freshness"
            )

        monkeypatch.setattr(bi.subprocess, "run", _explode)

        first = await naukri_server_info()
        second = await naukri_server_info()

        assert first["status"] == "success"
        assert first["build"]["code"] == second["build"]["code"]
        assert first["build"]["jobcore"] == second["build"]["jobcore"]

    async def test_uptime_moves_even_though_the_stamp_does_not(self):
        """The counterpart. Freezing the stamp must not freeze the clock too:
        a cached uptime is a lie that grows."""
        import asyncio

        from naukri_server.tools.server_info import naukri_server_info

        first = await naukri_server_info()
        await asyncio.sleep(0.15)
        second = await naukri_server_info()

        assert (second["build"]["process"]["uptime_seconds"]
                >= first["build"]["process"]["uptime_seconds"])
        assert (first["build"]["process"]["started_at"]
                == second["build"]["process"]["started_at"])
        assert first["build"]["process"]["pid"] == second["build"]["process"]["pid"]


class TestTheBuildBlock:

    async def test_server_info_reports_a_commit_and_a_dirty_flag(self):
        """Both halves of "which code": the commit, and whether the tree differs
        from it. A commit alone is not enough -- an edited working tree is
        running code that no commit describes."""
        from naukri_server.tools.server_info import naukri_server_info

        result = await naukri_server_info()
        code = result["build"]["code"]

        assert set(code) == BUILD_STAMP_KEYS, (
            "the build block is not a full BuildStamp: %s" % sorted(code)
        )
        assert code["source"] in ("git", "unknown")
        if code["source"] == "unknown":
            pytest.skip("no resolvable git work tree here: %s" % code["detail"])

        assert code["commit"], "source is git but no commit was reported"
        assert len(code["commit"]) == 12
        assert code["commit_full"].startswith(code["commit"])
        assert len(code["commit_full"]) == 40
        assert isinstance(code["dirty"], bool)
        assert isinstance(code["dirty_files"], int)
        assert code["dirty"] == (code["dirty_files"] > 0)
        assert code["resolved_at"]

    @needs_git
    async def test_the_reported_commit_is_the_one_on_disk_right_now(self):
        """The documented use of the field, executed.

        This is what a reader is told to do: compare `build.code.commit` against
        `git rev-parse HEAD`. It holds here because nothing has committed since
        this process imported -- which is exactly the condition the comparison
        is designed to detect the absence of.
        """
        from naukri_server.tools.server_info import naukri_server_info
        from naukri_server.utils import SERVER_ROOT

        result = await naukri_server_info()
        code = result["build"]["code"]
        if code["source"] != "git":
            pytest.skip("no resolvable git work tree here")

        assert code["commit_full"] == _git_head(SERVER_ROOT)

    async def test_the_process_block_names_this_process(self):
        import os

        from naukri_server.tools.server_info import naukri_server_info

        proc = (await naukri_server_info())["build"]["process"]
        assert set(proc) == {"pid", "started_at", "uptime_seconds"}
        assert proc["pid"] == os.getpid()
        assert proc["uptime_seconds"] >= 0


class TestJobcoreIsStampedSeparately:

    async def test_jobcore_is_stamped_separately_from_naukri(self):
        """Two repositories, two stamps, either one able to be stale alone.

        jobcore is editable-installed from a sibling checkout on its own
        schedule. Reporting only naukri's commit would leave the scoring engine
        unaccounted for -- and scoring is where a silent behaviour change hurts
        most.
        """
        from naukri_server.tools.server_info import naukri_server_info

        build = (await naukri_server_info())["build"]
        code, jc = build["code"], build["jobcore"]

        assert set(jc) == BUILD_STAMP_KEYS
        if code["source"] != "git" or jc["source"] != "git":
            pytest.skip("one of the two checkouts is not a git work tree")

        assert code["commit_full"] != jc["commit_full"], (
            "both blocks name the same commit -- jobcore is not being stamped "
            "from its own checkout"
        )

    def test_the_two_stamps_come_from_two_different_anchors(self):
        """Structural: separate memo keys, so neither can shadow the other.

        `jobcore.buildinfo.stamp` memoises on the RESOLVED start path. Equal
        keys would mean one repository answering for both, which the payload
        would present as two independent measurements.
        """
        from pathlib import Path

        import jobcore
        import jobcore.buildinfo as bi
        from naukri_server import buildinfo as nb
        from naukri_server.utils import SERVER_ROOT

        naukri_key = str(Path(SERVER_ROOT).resolve())
        jobcore_key = str(Path(jobcore.__file__).resolve())
        assert naukri_key != jobcore_key

        assert nb.BUILD is not nb.JOBCORE_BUILD
        # Identity, not equality: these must be the very objects resolved at
        # import, not fresh ones that happen to agree.
        assert nb.BUILD is bi.stamp(SERVER_ROOT)
        assert nb.JOBCORE_BUILD is bi.stamp(jobcore.__file__)

    @needs_git
    async def test_the_jobcore_commit_is_jobcores_own_head(self):
        from pathlib import Path

        import jobcore
        from naukri_server.tools.server_info import naukri_server_info

        jc = (await naukri_server_info())["build"]["jobcore"]
        if jc["source"] != "git":
            pytest.skip("jobcore is not a git work tree here")

        assert jc["commit_full"] == _git_head(Path(jobcore.__file__).parent)


class TestTheRestOfThePayload:

    async def test_server_info_leaks_no_absolute_path(self):
        """Every path in the payload is relativised.

        A debug tool is the LAST place a leak is acceptable: it is the one most
        likely to be pasted into a shared transcript or an issue.
        """
        from naukri_server.tools.server_info import naukri_server_info

        result = await naukri_server_info()

        assert_no_absolute_path(result, "naukri_server_info")
        assert not DRIVE_PATH.search(repr(result))
        # And still an ANSWER, not a redaction.
        assert result["database"], "the database path was removed, not relativised"
        assert "naukri.db" in result["database"]

    async def test_the_config_block_carries_both_fingerprints(self):
        """`policy_hash` and `scoring_hash` are not interchangeable: a stored
        score carries the latter, a config readout needs the former. A server
        report that published only one would invite the wrong comparison."""
        from naukri_server import policy
        from naukri_server.tools.server_info import naukri_server_info

        cfg = (await naukri_server_info())["config"]
        stamp = policy.policy_stamp()

        assert set(cfg) == {"source", "policy_rev", "policy_hash", "scoring_hash"}
        assert cfg["policy_rev"] == stamp["policy_rev"]
        assert cfg["policy_hash"] == stamp["policy_hash"]
        assert cfg["scoring_hash"] == stamp["scoring_hash"]

    async def test_the_surface_counts_match_the_live_registries(self):
        from naukri_server import mcp
        from naukri_server.tools.server_info import naukri_server_info

        surface = (await naukri_server_info())["surface"]

        assert surface["tools"] == len(mcp._tool_manager._tools)
        assert surface["resources"] == len(mcp._resource_manager._resources)
        assert surface["prompts"] == len(mcp._prompt_manager._prompts)
        assert surface["tools"] > 100

    async def test_a_renamed_fastmcp_internal_degrades_to_none(self, monkeypatch):
        """The counts read FastMCP privates. A rename upstream must cost a null
        field, not the whole tool -- this is the tool a reader reaches for when
        something is already broken."""
        from naukri_server import mcp
        from naukri_server.tools.server_info import naukri_server_info

        class _Exploding:
            def __getattr__(self, name):
                raise AttributeError("renamed upstream")

        monkeypatch.setattr(mcp, "_tool_manager", _Exploding())

        result = await naukri_server_info()
        assert result["status"] == "success"
        assert result["surface"]["tools"] is None
        # The siblings still answer -- one rename must not blank the block.
        assert result["surface"]["resources"] == len(mcp._resource_manager._resources)

    async def test_the_envelope_names_the_server(self):
        from naukri_server.tools.server_info import naukri_server_info

        result = await naukri_server_info()
        assert result["status"] == "success"
        assert result["server"] == "naukri"
        assert set(result) == {"status", "server", "build", "config", "surface",
                               "database"}
        assert set(result["build"]) == {"code", "jobcore", "process"}


class TestItIsRegisteredAsATool:

    def test_the_tool_is_registered_and_wrapped(self):
        """It must be reachable as an MCP tool, and it must not have bypassed
        the server-wide watchdog/scrub wrapper on the way in."""
        from naukri_server import mcp
        from naukri_server.tools.server_info import naukri_server_info

        assert "naukri_server_info" in mcp._tool_manager._tools
        assert getattr(naukri_server_info, "__wrapped_by_watchdog__", False)

    def test_the_docstring_tells_the_reader_what_to_do_with_it(self):
        """A build hash nobody knows how to compare is decoration."""
        from naukri_server.tools.server_info import naukri_server_info

        doc = naukri_server_info.__doc__ or ""
        assert "git rev-parse HEAD" in doc
        assert "stale" in doc.lower()
        assert "restart" in doc.lower()
