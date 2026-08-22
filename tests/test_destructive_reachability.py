"""Which tools can DELETE -- decided by what they reach, not what they are called.

`test_read_path_purity.py` guards the emit shape, and it decides what counts as
a read from the FUNCTION NAME: `READ_PREFIXES = get_ list_ fetch_ read_ audit_
search_ find_ load_`, matched after stripping underscores. That is a
name-shaped blind spot, and the defect it missed is the most destructive one
this server has had.

`detect_status_changes` matches none of those prefixes. It is analytics-shaped
and read-named in spirit -- `naukri_status_changes(days=30)` -- and until
2026-08-22 it called `_sync_applications`, whose saga ran
`delete_applications_before`. A read deleted his application history, and no
amount of mutation inside it could ever have failed the name-gated guard.
`_sync_applications`, `_cached_answers`, `_on_result` and `_execute_task` are
exempt from that guard for the same reason.

So this census asks a different question, with no names in it: starting from
every `@mcp.tool()`, can control reach a destructive sink? A tool that can must
be on DESTRUCTIVE_TOOLS with a reason. Anything else is a failure, whatever it
is called.

Verified against the real pre-fix tree (`git archive 7e8aeb4^`): the census
reported five tools reaching a sink, including the exact historical chain

    naukri_status_changes -> detect_status_changes
                          -> _sync_applications -> delete_applications_before

and reports two on the fixed tree.
"""

import ast
from collections import defaultdict
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "naukri_server"

# Functions whose execution removes something the operator cannot recreate.
# Narrow on purpose: a broad sink list produces noise, and a noisy guard gets
# suppressed rather than fixed.
DESTRUCTIVE_SINKS = frozenset((
    "delete_applications_before", "cleanup_old_records",
    "purge_applications", "_purge_applications",
    "delete_alert", "_delete_alert",
    "delete_photo", "_delete_photo",
    "unsave_job", "_unsave_job", "remove_saved_job",
    "delete_reminder",
))

# Tools allowed to reach one. Deleting IS their job; each says so.
DESTRUCTIVE_TOOLS = {
    "naukri_purge_applications":
        "Retention. THE only path that removes an application since the sync "
        "saga's silent copy was deleted. Previews by default (dry_run=True), "
        "skips rows with no trustworthy apply date, and archives every row it "
        "removes into applications_archive first.",
    "naukri_unsave_job":
        "Removing a bookmark is the entire point of the tool, and a saved job "
        "is re-savable from the listing it came from.",
}


def _census(root: Path) -> dict:
    """{tool_name: chain} for every MCP tool that can reach a destructive sink.

    Name-keyed and import-alias-aware. The alias half is load-bearing rather
    than defensive: `tools/insights.py:31` imports `detect_status_changes as
    _detect_status_changes`, so a graph that does not resolve aliases silently
    loses the edge and reports the historical bug as clean. That exact false
    negative happened while this was being built.
    """
    refs = defaultdict(set)
    alias = {}
    tools = {}

    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.asname and a.asname != a.name:
                        alias[a.asname] = a.name.split(".")[-1]
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    # helpers are often passed as BARE REFERENCES to
                    # handle_tool_action, so reference edges matter as much as
                    # call edges; Attribute covers `mod.helper` forms.
                    names.add(sub.attr)
            names.discard(node.name)
            refs[node.name] |= names
            for dec in node.decorator_list:
                if "mcp.tool" in ast.unparse(dec):
                    tools[node.name] = path.name

    def expand(name):
        out, seen = {name}, set()
        while name in alias and name not in seen:
            seen.add(name)
            name = alias[name]
            out.add(name)
        return out

    def reach(start):
        seen, stack, back = {start}, [start], {}
        while stack:
            cur = stack.pop()
            for raw in refs.get(cur, ()):
                for nxt in expand(raw):
                    if nxt in seen:
                        continue
                    back[nxt] = cur
                    if nxt in DESTRUCTIVE_SINKS:
                        chain, n = [nxt], nxt
                        while n in back:
                            n = back[n]
                            chain.append(n)
                        return list(reversed(chain))
                    seen.add(nxt)
                    stack.append(nxt)
        return None

    out = {}
    for tool in tools:
        chain = reach(tool)
        if chain:
            out[tool] = chain
    return out


CENSUS = _census(PKG)


class TestNoUndeclaredToolCanDelete:
    def test_the_census_found_tools_to_check(self):
        assert len(CENSUS) >= 1, "a census that finds nothing is not measuring"

    def test_every_deleting_tool_is_declared(self):
        undeclared = {t: c for t, c in CENSUS.items() if t not in DESTRUCTIVE_TOOLS}
        assert not undeclared, (
            "these tools can reach a destructive sink and are not declared:\n"
            + "\n".join("  %s: %s" % (t, " -> ".join(c))
                        for t, c in sorted(undeclared.items()))
            + "\nEither stop them deleting, or add them to DESTRUCTIVE_TOOLS "
              "with a reason. A read that deletes is the defect this exists for."
        )

    def test_the_declaration_list_has_no_stale_entries(self):
        """CONTROL the other way: once a tool stops deleting it must leave the
        list, or the list becomes where old permissions go to hide."""
        stale = [t for t in DESTRUCTIVE_TOOLS if t not in CENSUS]
        assert not stale, "no longer reaches a sink, remove from DESTRUCTIVE_TOOLS: %s" % stale

    def test_sync_and_status_changes_specifically_cannot_delete(self):
        """The three that could, named, so a regression is unambiguous."""
        for tool in ("naukri_sync_applications", "naukri_sync_saved",
                     "naukri_status_changes"):
            assert tool not in CENSUS, (
                "%s can reach a destructive sink again: %s"
                % (tool, " -> ".join(CENSUS[tool]))
            )


_HISTORICAL_SHAPE = '''
from naukri_server import mcp
from naukri_server.services.insights_service import (
    detect_status_changes as _detect_status_changes,
)

async def delete_applications_before(date):
    ...

async def _sync_applications(days_back=365):
    await delete_applications_before("cutoff")

async def detect_status_changes(days_back=30):
    return await _sync_applications(days_back=days_back)

@mcp.tool()
async def naukri_status_changes(days: int = 30):
    """Analytics-shaped, read in spirit, deletes his history."""
    return await _detect_status_changes(days_back=days)
'''

_CLEAN_SHAPE = '''
from naukri_server import mcp

async def _sync_applications(days_back=365):
    return {"merged": 1}

@mcp.tool()
async def naukri_status_changes(days: int = 30):
    return await _sync_applications(days_back=days)
'''


class TestTheCensusItself:
    """An instrument that has never been shown failing certifies nothing."""

    def test_CONTROL_it_catches_the_historical_chain_through_an_alias(self, tmp_path):
        (tmp_path / "m.py").write_text(_HISTORICAL_SHAPE, encoding="utf-8")
        found = _census(tmp_path)

        assert "naukri_status_changes" in found, (
            "the census missed the exact defect it was built for -- most likely "
            "it stopped resolving `import X as _X`"
        )
        chain = found["naukri_status_changes"]
        assert chain[-1] == "delete_applications_before"
        assert "detect_status_changes" in chain, chain

    def test_CONTROL_it_stays_quiet_on_a_sync_that_does_not_delete(self, tmp_path):
        (tmp_path / "m.py").write_text(_CLEAN_SHAPE, encoding="utf-8")
        assert _census(tmp_path) == {}

    def test_CONTROL_it_ignores_a_plain_function_that_is_not_a_tool(self, tmp_path):
        """Only @mcp.tool() entry points are roots -- a helper that deletes is
        not itself a finding, or every sink would report itself."""
        (tmp_path / "m.py").write_text(
            "async def delete_applications_before(d):\n    ...\n"
            "async def helper():\n    await delete_applications_before('x')\n",
            encoding="utf-8")
        assert _census(tmp_path) == {}

    def test_CONTROL_the_name_gate_would_have_missed_it(self):
        """Why this file exists, asserted rather than described.

        `detect_status_changes` passes the read-path guard's name test, so that
        guard could never have failed on it however much it mutated.
        """
        from tests.test_read_path_purity import _is_read_shaped

        assert not _is_read_shaped("detect_status_changes")
        assert not _is_read_shaped("_sync_applications")
        assert not _is_read_shaped("_cached_answers")
        # ...while the names it does cover pass, so the gate is real, just narrow.
        assert _is_read_shaped("_read_message")
        assert _is_read_shaped("list_applications")
