"""The jobcore pin in requirements-ci.txt must satisfy what this package uses.

THE HOLE THIS CLOSES, walked into three times.

`requirements-ci.txt` pins jobcore to an exact commit. A local venv has jobcore
installed EDITABLE from ../jobcore, so it always has the newest code -- which
means using something jobcore only just grew leaves the local suite fully green
and kills CI at import time. Every local run agrees; the runner disagrees alone.

  2026-08-21  `jobcore.policy` added while the pin said d144bb.  Two pushes red,
              37 collection errors.
  2026-08-22  `jobcore.buildinfo` and `jobcore.paths` added by 8a8f596 while the
              pin said 16ae934, which has NEITHER MODULE. Red on 3.10/3.11/3.12.
  2026-08-22  (sibling repo, caught before push) `jobcore.buildinfo.self_stamp()`
              used while pinned at d1720c3. The MODULE exists there; the
              ATTRIBUTE does not. `AttributeError: module 'jobcore.buildinfo'
              has no attribute 'self_stamp'`.

THE THIRD CASE IS WHY THIS FILE CHECKS NAMES, NOT JUST MODULES. A module-level
check passes on case 3 and CI still dies -- and it is the check most likely to
be trusted next time, which makes a half-instrument worse than none. Measured
across the three pins:

    16ae934 : buildinfo.py present=0  def self_stamp=n/a
    d1720c3 : buildinfo.py present=1  def self_stamp=0
    998baf1 : buildinfo.py present=1  def self_stamp=1

So this reads the pinned tree and asserts, for every jobcore module this package
touches, that the module exists AND that every name it pulls out of that module
is defined there.

WHAT IT DOES NOT CHECK, stated so nobody trusts it further than it reaches:
SIGNATURES. `Loaded.report(display=...)` passes this check at any commit where
`report` merely exists, because the name is present. A keyword argument added
upstream is a fourth failure class this cannot see. It also does not resolve
class attributes -- only module-level names.

IT IS OFFLINE AND CHEAP: `git ls-tree` + `git show` against the ../jobcore clone
already on the box, no fetch, and it never imports from the pinned commit. When
the clone is absent (a runner checks out naukri alone) or does not know the pin,
it SKIPS with a reason rather than inventing an answer -- the value is catching
this before a push.
"""

import re
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_CI = REPO_ROOT / "requirements-ci.txt"
JOBCORE_CLONE = REPO_ROOT.parent / "jobcore"
PACKAGE = REPO_ROOT / "naukri_server"

#: `jobcore @ git+https://github.com/Sundeepg98/jobcore@<hex>`
PIN_RE = re.compile(r"^jobcore\s*@\s*git\+\S+?@([0-9a-fA-F]{7,40})\s*$", re.MULTILINE)

#: `from jobcore.buildinfo import stamp, ProcessClock`
FROM_SUBMODULE = re.compile(r"^\s*from\s+jobcore\.(\w+)\s+import\s+(.+)$", re.MULTILINE)
#: `import jobcore.buildinfo as bi` / `import jobcore.buildinfo`
IMPORT_SUBMODULE = re.compile(
    r"^\s*import\s+jobcore\.(\w+)(?:\s+as\s+(\w+))?\s*$", re.MULTILINE)
#: `from jobcore import buildinfo as _bi, config`
FROM_PACKAGE = re.compile(r"^\s*from\s+jobcore\s+import\s+(.+)$", re.MULTILINE)


def _parse_names(chunk):
    """`a, b as c, (d)` -> [(a, a), (b, c), (d, d)]."""
    out = []
    for piece in chunk.split("#")[0].split(","):
        piece = piece.strip().strip("()").strip()
        if not piece:
            continue
        if " as " in piece:
            orig, alias = [p.strip() for p in piece.split(" as ", 1)]
        else:
            orig = alias = piece
        if orig.isidentifier() and alias.isidentifier():
            out.append((orig, alias))
    return out


def _git(*args):
    """Run git and decode as UTF-8 EXPLICITLY.

    Not `text=True`: that decodes with the console codepage, which is cp1252 on
    this box, and jobcore's sources contain non-ASCII. The reader thread then
    died with a UnicodeDecodeError and the captured stdout came back EMPTY with
    a zero return code -- so `_source_at` would hand `_defines` an empty string
    and every name in that module would read as missing, or the module would be
    skipped outright. An instrument that silently answers wrong is worse than
    one that fails, so the decode is pinned and lossy-but-total.
    """
    proc = subprocess.run(
        ["git", "-C", str(JOBCORE_CLONE), *args], capture_output=True)
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"))


def _pinned_sha() -> str:
    match = PIN_RE.search(REQUIREMENTS_CI.read_text(encoding="utf-8"))
    assert match, "no jobcore pin line found in requirements-ci.txt"
    return match.group(1)


def _clone_knows(sha) -> bool:
    if not (JOBCORE_CLONE / ".git").exists():
        return False
    return _git("cat-file", "-e", "%s^{commit}" % sha).returncode == 0


def _modules_at(sha):
    listing = _git("ls-tree", "--name-only", sha, "src/jobcore/")
    if listing.returncode != 0:
        return set()
    names = set()
    for line in listing.stdout.splitlines():
        name = Path(line.strip()).name
        if name.endswith(".py"):
            names.add(name[:-3])
    return names


def _source_at(sha, module):
    out = _git("show", "%s:src/jobcore/%s.py" % (sha, module))
    return out.stdout if out.returncode == 0 else None


def _defines(source: str, name: str) -> bool:
    """Is `name` defined at module level in this source text?

    Text matching on purpose: importing from the pinned commit would need that
    commit installed, which is the whole thing this test exists to avoid.
    """
    patterns = [
        r"^\s*def\s+%s\b",
        r"^\s*async\s+def\s+%s\b",
        r"^\s*class\s+%s\b",
        r"^%s\s*(?::[^=]+)?=",          # module constant, annotated or not
    ]
    if any(re.search(p % re.escape(name), source, re.MULTILINE) for p in patterns):
        return True
    # A re-export listed in __all__ counts as provided by the module.
    all_block = re.search(r"__all__\s*=\s*\[(.*?)\]", source, re.DOTALL)
    if all_block and re.search(r"['\"]%s['\"]" % re.escape(name), all_block.group(1)):
        return True
    return False


def _requirements():
    """What this package needs from jobcore.

    Returns ``(needed, package_names)``:
      * ``needed``   -- {submodule: {names used from it}}
      * ``package_names`` -- names taken straight off the package,
        ``from jobcore import X``, where X may be a SUBMODULE or a re-exported
        symbol and this function deliberately does not try to tell which.

    THE AMBIGUITY IS RESOLVED LATE, against the pinned commit, because that is
    the only place the answer matters and the only place it can be had for
    free. The previous version resolved it EARLY and needed a set of "current"
    module names to do it -- and the only source on hand was the ../jobcore
    clone, which does not exist on a CI runner. The set came back empty, the
    gate `if orig in current_modules` was never true, and the scanner silently
    stopped seeing `jobcore.buildinfo` at all: the one module reached only
    through `from jobcore import buildinfo as _bi`. The attribute pass that
    would have found `_bi.self_stamp` never ran either.
    """
    needed = defaultdict(set)
    package_names = set()
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        aliases = {}

        for module, names in FROM_SUBMODULE.findall(text):
            needed[module].update(n for n, _ in _parse_names(names))
            needed[module]  # ensure the module itself is required

        for module, alias in IMPORT_SUBMODULE.findall(text):
            aliases[alias or module] = module
            needed[module]

        for chunk in FROM_PACKAGE.findall(text):
            for orig, alias in _parse_names(chunk):
                # Recorded UNCONDITIONALLY, module or not. Nothing here needs
                # to know jobcore's shape, so nothing here can go blind when
                # the clone is missing.
                package_names.add(orig)
                aliases[alias] = orig

        # Attribute use through a module alias: `_bi.self_stamp(...)`.
        for alias, module in aliases.items():
            for attr in re.findall(r"\b%s\.(\w+)" % re.escape(alias), text):
                if not (attr.startswith("__") and attr.endswith("__")):
                    needed[module].add(attr)
    # A plain dict: a defaultdict would let `needed["typo"]` in a test silently
    # create an empty entry and assert nothing.
    return dict(needed), package_names


def test_the_pin_line_is_present_and_is_an_exact_commit():
    """A moving `@master` would let another repo turn this repo's CI red."""
    sha = _pinned_sha()
    assert len(sha) >= 7 and re.fullmatch(r"[0-9a-fA-F]+", sha)


def test_every_jobcore_module_and_name_used_here_exists_at_the_pinned_commit():
    """Both failure classes: a missing MODULE and a missing ATTRIBUTE."""
    sha = _pinned_sha()
    if not _clone_knows(sha):
        pytest.skip(
            "../jobcore clone is absent or does not know commit %s; this check "
            "is offline by design and does not fetch" % sha)

    available = _modules_at(sha)
    init_source = _source_at(sha, "__init__")
    needed, package_names = _requirements()
    assert needed or package_names, (
        "no jobcore usage found at all -- the scanner is broken")

    problems = []

    # `from jobcore import X`: at the pin, X must be a submodule OR a name the
    # package re-exports. This is where the module-or-symbol ambiguity is
    # settled, using the pinned tree rather than a guess about the local one.
    for name in sorted(package_names):
        if name in available:
            continue
        if init_source and _defines(init_source, name):
            continue
        problems.append("jobcore.%s (MODULE OR PACKAGE NAME MISSING)" % name)

    for module, names in sorted(needed.items()):
        if module not in available:
            # Already judged above if it came off the package; otherwise it was
            # imported as `jobcore.<module>` and is simply not there.
            if module not in package_names:
                problems.append("jobcore.%s (MODULE MISSING)" % module)
            continue
        source = _source_at(sha, module)
        if source is None:
            continue
        for name in sorted(names):
            if not _defines(source, name):
                problems.append("jobcore.%s.%s (ATTRIBUTE MISSING)" % (module, name))

    assert not problems, (
        "requirements-ci.txt pins jobcore at %s, which does not satisfy this "
        "package: %s -- CI dies at import with ModuleNotFoundError or "
        "AttributeError while every local run stays green, because the local "
        "venv has jobcore editable from ../jobcore. Bump the pin in the SAME "
        "commit that adds the usage."
        % (sha[:12], "; ".join(problems)))


def test_the_scanner_sees_both_import_forms_it_is_meant_to_guard():
    """Guards the guard.

    A scanner that silently found nothing would make the test above pass
    vacuously forever. These are things naukri_server genuinely uses today, one
    of each shape: a submodule import, a from-package alias, and an ATTRIBUTE
    reached through that alias -- the shape that slipped past the module-only
    version of this check.
    """
    needed, package_names = _requirements()

    for module in ("config", "buildinfo", "paths", "policy"):
        assert module in needed or module in package_names, (
            "scanner missed jobcore.%s" % module)

    assert "display_path" in needed.get("paths", set()), (
        "scanner missed the NAME imported from jobcore.paths")
    assert "self_stamp" in needed.get("buildinfo", set()), (
        "scanner missed an attribute reached through a module alias "
        "(`_bi.self_stamp`) -- this is the exact shape that slips through a "
        "module-only check")


def test_the_scanner_resolves_from_package_imports_without_a_clone(monkeypatch):
    """THE CI FAILURE, as a regression test.

    This test file used to disambiguate `from jobcore import X` EARLY, against
    a set of module names read from the ../jobcore clone. A runner checks out
    naukri alone, so the clone is absent, the set came back EMPTY, and the gate
    was never true. `naukri_server/buildinfo.py` reaches jobcore.buildinfo
    ONLY through `from jobcore import buildinfo as _bi`, so the scanner stopped
    seeing that module -- and the attribute pass that finds `_bi.self_stamp`
    never ran either. The other modules survived because they arrive through
    `from jobcore.X import ...`, which never consulted the clone.

    Red before the fix with exactly the message CI produced:
    `AssertionError: scanner missed jobcore.buildinfo`.

    The scanner now needs no shape source at all, so pointing the clone at
    nowhere must change nothing about what it finds.
    """
    import tests.test_jobcore_pin as mod

    monkeypatch.setattr(mod, "JOBCORE_CLONE", Path("/nonexistent/jobcore"))
    assert not _clone_knows("HEAD"), "the clone override did not take"

    needed, package_names = _requirements()

    assert "buildinfo" in package_names, (
        "scanner missed jobcore.buildinfo with no clone present -- this is the "
        "CI failure")
    assert "self_stamp" in needed.get("buildinfo", set()), (
        "the attribute pass through the alias did not run without a clone")
    assert "display_path" in needed.get("paths", set()), (
        "a from-submodule import regressed")


def test_the_name_check_can_tell_present_from_absent():
    """`_defines` shipped shown failing, on real pinned sources.

    d1720c3 has buildinfo.py WITHOUT self_stamp; 998baf1 has it. If `_defines`
    ever answers True for both, the attribute half of this file is decorative.
    """
    for sha in ("d1720c3", "998baf1"):
        if not _clone_knows(sha):
            pytest.skip("../jobcore does not know %s" % sha)

    old = _source_at("d1720c3", "buildinfo")
    new = _source_at("998baf1", "buildinfo")
    assert old and new

    assert _defines(old, "stamp"), "a name that IS there reads as absent"
    assert _defines(new, "stamp")
    assert _defines(new, "self_stamp"), "a name that IS there reads as absent"
    assert not _defines(old, "self_stamp"), (
        "_defines says d1720c3 has self_stamp; it does not, and the attribute "
        "check is therefore incapable of failing")


def test_the_pinned_sources_are_actually_readable():
    """Guards the reader itself.

    `git show` output used to be decoded with the console codepage and came
    back EMPTY on any module containing non-ASCII -- silently turning the name
    check into either a false alarm or a skip. This asserts every module the
    package needs yields real source text at the pinned commit.
    """
    sha = _pinned_sha()
    if not _clone_knows(sha):
        pytest.skip("../jobcore does not know %s" % sha)

    available = _modules_at(sha)
    needed, _ = _requirements()
    checked = 0
    for module in sorted(needed):
        if module not in available:
            continue
        source = _source_at(sha, module)
        assert source, "git show returned nothing for jobcore.%s" % module
        assert len(source) > 200, (
            "jobcore.%s came back suspiciously short (%d chars) -- a decode "
            "failure truncates silently" % (module, len(source)))
        checked += 1
    assert checked >= 3, "only %d modules were readable" % checked
