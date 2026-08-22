"""A green suite must be green over the WHOLE suite.

MEASURED 2026-08-22. `jobcore` was not installed in this box's interpreter and
48 test modules failed collection before anything ran. That is recoverable on
its own - what is not is that the run could still be REPORTED as green. Two
independent mechanisms, both reproduced rather than argued, using a meta_path
blocker that makes `jobcore` unimportable without touching the interpreter:

  1. THE EXIT CODE DOES NOT SURVIVE A PIPE. `pytest tests/ -q` on the broken
     environment printed "Interrupted: 50 errors during collection" and exited
     2 - but `pytest tests/ -q 2>&1 | tail -5` exits with TAIL's status, 0. A
     caller that trusts the exit code reads a wholly uncollected run as a pass.
     Fixed by the hard-dependency preflight in conftest.pytest_sessionstart:
     it aborts before collection, so its message - which names the remediation,
     unlike a collection traceback - is the LAST thing printed and survives the
     pipe. Verified: `| tail -6` on the broken environment now shows
     "FIX: python -m pip install -e ../jobcore".

  2. A SKIP READS AS GREEN. `pytest.importorskip("jobcore.scoring")` sat at
     four sites in test_work_mode_codes.py. It converts a missing HARD
     dependency into skips, and `-q` reports skips in a line most readers scan
     as success. It did not bite today only because that same module also
     imports jobcore unconditionally, so collection failed first - remove that
     and these four go quietly green on a broken tree. Banned below.

The general form of both: a suite can shrink without turning red. These guards
make shrinkage loud. Every one ships a CONTROL that can fail it - a guard never
run against a known-bad tree certifies nothing, and this repo has produced
fourteen checks-that-could-not-fail in three days.
"""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.conftest import (
    HARD_DEPENDENCIES, REMEDIATION, missing_hard_dependencies,
)

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "naukri_server"
TESTS = ROOT / "tests"

#: Modules that legitimately contribute zero collected items under the default
#: `addopts = -m "not e2e"`. Both are entirely e2e-marked. Measured, not
#: assumed: 142 test modules on disk, 140 contributing, and these are the two.
#: An entry here is a claim that the module is deselected BY DESIGN.
ZERO_ITEM_BY_DESIGN = {
    "test_browser_liveness_e2e.py",
    "test_e2e_smoke.py",
}


# =====================================================================
# 1. THE PREFLIGHT - shown detecting, shown silent, shown aborting a real run
# =====================================================================

class TestHardDependencyPreflight:

    def test_CONTROL_detects_a_module_that_is_not_installed(self):
        missing = missing_hard_dependencies(["definitely_not_installed_zzz"])
        assert [n for n, _ in missing] == ["definitely_not_installed_zzz"]

    def test_CONTROL_is_silent_on_an_importable_module(self):
        """The other direction. A preflight that flags everything is noise and
        would abort every run on every box."""
        assert missing_hard_dependencies(["json", "pathlib"]) == []

    def test_CONTROL_catches_a_module_that_imports_but_raises(self, tmp_path, monkeypatch):
        """`except Exception`, not `except ImportError`, is load-bearing: a
        dependency that is present but blows up on import is just as broken,
        and a bare ImportError catch would wave it through."""
        (tmp_path / "explodes_on_import_zzz.py").write_text(
            "raise RuntimeError('bad install')\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop("explodes_on_import_zzz", None)

        missing = missing_hard_dependencies(["explodes_on_import_zzz"])

        assert len(missing) == 1
        assert isinstance(missing[0][1], RuntimeError)

    def test_the_preflight_aborts_a_real_pytest_run(self, tmp_path):
        """The decisive one: the function above is only worth having if it is
        WIRED. Runs a real pytest subprocess against a tree where jobcore is
        unimportable and proves the run dies at sessionstart carrying the
        remediation - not 50 collection tracebacks that say nothing about
        what to do.
        """
        blocker = tmp_path / "blockjobcore_zzz.py"
        blocker.write_text(textwrap.dedent('''
            import sys

            class _Block:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "jobcore" or fullname.startswith("jobcore."):
                        raise ImportError("No module named %r (test blocker)" % fullname)
                    return None

            for _n in list(sys.modules):
                if _n == "jobcore" or _n.startswith("jobcore."):
                    del sys.modules[_n]

            sys.meta_path.insert(0, _Block())
        ''').lstrip(), encoding="utf-8")

        env = dict(**_clean_env())
        env["PYTHONPATH"] = str(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q",
             "-p", "blockjobcore_zzz", "--collect-only"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600,
        )
        out = proc.stdout + proc.stderr

        assert proc.returncode != 0, "a broken environment must not exit 0"
        assert "HARD DEPENDENCY MISSING" in out, out[-3000:]
        assert REMEDIATION in out, (
            "the abort message must carry the fix, or it is just another "
            "traceback:\n%s" % out[-3000:])
        assert "errors during collection" not in out, (
            "the preflight must fire BEFORE collection - if collection ran, "
            "the wall of import errors is back and the message is buried"
        )

    def test_the_dependency_list_covers_every_unconditional_jobcore_import(self):
        """HARD_DEPENDENCIES has to stay honest as the package grows. Anything
        imported from jobcore at MODULE scope in naukri_server is by definition
        required for the package to import at all."""
        required = module_level_jobcore_imports(PKG)
        uncovered = sorted(required - set(HARD_DEPENDENCIES))
        assert uncovered == [], (
            "these jobcore modules are imported unconditionally but are not in "
            "conftest.HARD_DEPENDENCIES, so a broken install of them would "
            "still produce a wall of collection errors: %s" % uncovered
        )


def _clean_env():
    """Subprocess env without the vars that would change what pytest collects."""
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "JOBHUNT_CONFIG", "JOBHUNT_HOME", "JOBHUNT_DISABLE")}
    return env


def module_level_jobcore_imports(root: Path) -> set:
    """Every `jobcore[.x]` imported at MODULE scope under `root`.

    Module scope only, on purpose: naukri_server/agent.py imports
    jobcore.config and jobcore.policy INSIDE functions behind a try/except
    with a documented literal fallback, so those are optional at import time
    and do not belong in a hard-dependency list.
    """
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # top level only - not ast.walk
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "jobcore" or alias.name.startswith("jobcore."):
                        found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "jobcore":
                    # `from jobcore import X` - X may be a submodule or a
                    # re-export; the package itself is what must import.
                    found.add("jobcore")
                elif mod.startswith("jobcore."):
                    found.add(mod)
    return found


# =====================================================================
# 2. A HARD DEPENDENCY MAY NOT BE importorskip-ed
# =====================================================================

def importorskip_targets(root: Path) -> set:
    """Every (relpath, function, module) passed to pytest.importorskip."""
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relpath = path.relative_to(root).as_posix()
        stack = []

        class _V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", None))
                if name == "importorskip" and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add((relpath, stack[-1] if stack else "<module>",
                                   arg.value))
                self.generic_visit(node)

        _V().visit(tree)
    return found


_SKIPPING_A_HARD_DEP = '''
import pytest

def test_scoring():
    engine = pytest.importorskip("jobcore.scoring").ScoringEngine()
    assert engine
'''

_SKIPPING_SOMETHING_OPTIONAL = '''
import pytest

def test_optional():
    numpy = pytest.importorskip("numpy")
    assert numpy
'''


class TestNoImportOrSkipOnAHardDependency:
    """`importorskip` is the right tool for a genuinely optional extra. Used on
    something the package imports unconditionally it does the opposite of its
    job: it hides a broken environment behind a green-looking skip."""

    def test_CONTROL_the_scanner_finds_one(self, tmp_path):
        (tmp_path / "bad.py").write_text(_SKIPPING_A_HARD_DEP, encoding="utf-8")
        assert importorskip_targets(tmp_path) == {
            ("bad.py", "test_scoring", "jobcore.scoring"),
        }

    def test_CONTROL_the_scanner_leaves_an_optional_extra_alone(self, tmp_path):
        """No false positives, or the rule cannot coexist with a real optional
        dependency."""
        (tmp_path / "ok.py").write_text(_SKIPPING_SOMETHING_OPTIONAL, encoding="utf-8")
        offenders = _hard_dep_offenders(importorskip_targets(tmp_path))
        assert offenders == []

    def test_no_test_importorskips_a_hard_dependency(self):
        offenders = _hard_dep_offenders(importorskip_targets(TESTS))
        assert offenders == [], (
            "importorskip on a HARD dependency turns a broken environment into "
            "a green-looking skip. Import it directly and let the preflight "
            "speak:\n%s" % "\n".join(
                "  %s::%s -> %s" % o for o in offenders)
        )


def _hard_dep_offenders(targets) -> list:
    hard = set(HARD_DEPENDENCIES)
    return sorted(t for t in targets
                  if t[2] in hard or t[2].split(".")[0] in
                  {h.split(".")[0] for h in hard})


# =====================================================================
# 3. NO TEST MODULE ON DISK MAY GO DARK
# =====================================================================

def is_full_run(args) -> bool:
    """True when the invocation collects the whole suite.

    A targeted run (`pytest tests/test_foo.py`, or a `::` node id) legitimately
    collects one module, so the completeness rule must not fire there.
    """
    for arg in args:
        if arg.startswith("-"):
            continue
        if "::" in arg or arg.endswith(".py"):
            return False
    return True


def dark_modules(on_disk, collected, exempt) -> list:
    """Modules present on disk that contributed nothing and are not exempt."""
    return sorted(set(on_disk) - set(collected) - set(exempt))


class TestEveryModuleContributes:

    def test_CONTROL_the_comparison_reports_a_dark_module(self):
        assert dark_modules({"a.py", "b.py"}, {"a.py"}, set()) == ["b.py"]

    def test_CONTROL_the_comparison_is_silent_when_all_contribute(self):
        assert dark_modules({"a.py", "b.py"}, {"a.py", "b.py"}, set()) == []

    def test_CONTROL_the_live_comparison_can_produce_a_hit(self, pytestconfig):
        """Not a synthetic tree - the REAL collected set. Drop the exemptions
        and the comparison must name the two e2e modules. If this returns
        empty, the exemption list is not what is keeping the check quiet and
        the green result above means nothing."""
        collected = getattr(pytestconfig, "_naukri_collected_modules", None)
        assert collected is not None
        if not is_full_run(list(pytestconfig.invocation_params.args)):
            pytest.skip("targeted run - the live set is not the whole suite")

        on_disk = {p.name for p in TESTS.glob("test_*.py")}
        assert dark_modules(on_disk, collected, set()) == sorted(ZERO_ITEM_BY_DESIGN)

    def test_CONTROL_is_full_run_tells_the_two_apart(self):
        assert is_full_run([]) is True
        assert is_full_run(["tests/"]) is True
        assert is_full_run(["-q", "tests/", "--tb=short"]) is True
        assert is_full_run(["tests/test_foo.py"]) is False
        assert is_full_run(["tests/test_foo.py::TestBar::test_baz"]) is False
        assert is_full_run(["-q", "tests/test_foo.py"]) is False

    def test_every_test_module_on_disk_contributed_a_test(self, pytestconfig):
        """142 files on disk, 140 contributing, 2 e2e-by-design. A 141st going
        dark - a renamed file, a module-level skip, a marker typo, a class that
        stopped being collected - would otherwise leave the pass count looking
        healthy."""
        collected = getattr(pytestconfig, "_naukri_collected_modules", None)
        assert collected is not None, (
            "conftest.pytest_collection_finish did not run, so this check "
            "has nothing to compare against and would pass vacuously"
        )

        if not is_full_run(list(pytestconfig.invocation_params.args)):
            pytest.skip("targeted run - completeness is only meaningful on a full one")

        on_disk = {p.name for p in TESTS.glob("test_*.py")}
        dark = dark_modules(on_disk, collected, ZERO_ITEM_BY_DESIGN)
        assert dark == [], (
            "test module(s) on disk contributed ZERO collected tests. Either "
            "they are broken or they are deselected - both make the suite "
            "smaller than it looks:\n%s" % "\n".join("  " + d for d in dark)
        )

    def test_the_by_design_exemptions_are_real(self):
        """An exemption list nobody checks becomes a place to hide a broken
        module. Each entry must exist on disk and must actually be e2e."""
        for name in sorted(ZERO_ITEM_BY_DESIGN):
            path = TESTS / name
            assert path.exists(), "stale exemption, delete it: %s" % name
            assert "e2e" in path.read_text(encoding="utf-8"), (
                "%s is exempted as e2e-by-design but carries no e2e marker" % name
            )
