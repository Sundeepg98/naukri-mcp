"""The dependency pins CI depends on, asserted rather than trusted.

On 2026-08-20 naukri's CI was red for a reason no local run could show. The
local venv holds mcp 1.26.0, installed before mcp 2.0.0 shipped, so every local
suite passed. A CLEAN resolve -- which is what a CI runner does, every time --
picked mcp 2.0.0, where `mcp/server/fastmcp` had moved to `mcp/server/mcpserver`.
`naukri_server/__init__.py:42` raises on import, so all 55 test modules died at
collection: "5 deselected, 55 errors", zero tests run.

That is the whole hazard class: an unbounded `>=` on a dependency whose next
major moves an import path is a time bomb whose fuse is lit by someone else's
release, and it is invisible until a clean install happens somewhere you are not
watching.

These tests read requirements.txt as TEXT on purpose. Asserting against the
installed version would pass happily in exactly the venv that hides the bug --
which is what already happened for the whole day this went unnoticed.

Pure: no network, no install, one small file read of a repo file.
"""

import re
from pathlib import Path

import pytest

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"
CI_REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements-ci.txt"


def _requirement_lines(path):
    """Yield the non-comment, non-blank requirement lines of a pip file."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            yield line


def test_mcp_is_capped_below_the_major_that_moved_fastmcp():
    """mcp 2.x is a MIGRATION, not a version bump. See this module's docstring."""
    lines = [ln for ln in _requirement_lines(REQUIREMENTS) if ln.lower().startswith("mcp")]
    assert lines, "mcp requirement disappeared from requirements.txt"
    assert any("<2" in ln for ln in lines), (
        "mcp must stay capped below 2.0 until naukri_server is migrated to the "
        "mcp 2.x import layout (mcp.server.mcpserver). Found: %r" % lines
    )


def test_jobcore_is_not_in_the_local_requirements_file():
    """It clobbers the editable install -- see the comment in requirements.txt.

    Measured: after `pip install -e ../jobcore`, a `pip install -r` of a file
    carrying the git URL uninstalls the editable package and replaces it with a
    git checkout, silently (pip prints no "already satisfied" line for a
    direct-URL requirement).
    """
    for line in _requirement_lines(REQUIREMENTS):
        assert not line.lower().startswith("jobcore"), (
            "jobcore belongs in requirements-ci.txt, not requirements.txt: %r" % line
        )


def test_ci_requirements_pin_jobcore_to_an_exact_commit():
    """A moving @master lets another repo turn this repo's CI red."""
    text = CI_REQUIREMENTS.read_text(encoding="utf-8")
    match = re.search(r"jobcore @ git\+\S+?@([0-9a-fA-F]{7,40})\b", text)
    assert match, (
        "requirements-ci.txt must pin jobcore to an exact commit SHA, not a branch"
    )
    assert len(match.group(1)) == 40, (
        "pin the full 40-character SHA, not an abbreviation: %r" % match.group(1)
    )


def test_ci_requirements_include_the_runtime_requirements():
    """requirements-ci.txt is an ADDITION to requirements.txt, not a fork.

    If it ever stops including it, CI silently tests a different dependency set
    from the one the server actually runs.
    """
    text = CI_REQUIREMENTS.read_text(encoding="utf-8")
    assert re.search(r"^-r\s+requirements\.txt\s*$", text, re.M), (
        "requirements-ci.txt must start from `-r requirements.txt`"
    )


@pytest.mark.parametrize("package", ["mcp"])
def test_no_dependency_that_moved_an_import_path_is_left_unbounded(package):
    """The general form of the rule, for the package that has already bitten.

    Deliberately narrow: capping every dependency would be cargo-culting. This
    guards the one where an unbounded `>=` is KNOWN to break the build.
    """
    for line in _requirement_lines(REQUIREMENTS):
        name = re.split(r"[<>=\[!~]", line, maxsplit=1)[0].strip().lower()
        if name == package:
            assert re.search(r"<\s*\d", line), (
                "%r has no upper bound; its next major already broke this build once"
                % line
            )
