"""The jobcore pin in requirements-ci.txt must satisfy what this package imports.

THE HOLE THIS CLOSES, walked into twice. `requirements-ci.txt` pins jobcore to
an exact commit. A local venv has jobcore installed EDITABLE from ../jobcore, so
it always has the newest modules -- which means adding
`from jobcore.something import ...` without bumping the pin leaves the local
suite fully green and kills CI at COLLECTION with
`ModuleNotFoundError: No module named 'jobcore.something'`.

2026-08-21: `jobcore.policy` added while the pin said d1d44bb. Two pushes red,
37 collection errors.
2026-08-22: `jobcore.buildinfo` and `jobcore.paths` added by commit 8a8f596
while the pin said 16ae934, which contains neither. The bump discipline was
already written in a 15-line comment directly above the pin, and was still
forgotten -- because nothing mechanical checked it.

This is the mechanical check. It reads the pinned SHA out of requirements-ci.txt,
lists the module names present in jobcore's tree AT THAT COMMIT, and asserts
every `jobcore.<module>` this package imports is among them.

IT IS OFFLINE AND CHEAP: one `git ls-tree` against the ../jobcore clone that is
already on the box. It does NOT fetch, so it can only answer when that clone
already has the pinned commit. When the clone is absent (a CI runner checks out
naukri alone) or the commit is unknown to it, the test SKIPS with a reason
rather than inventing an answer -- the value is catching the mistake locally,
BEFORE the push, which is the round trip that actually costs time.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_CI = REPO_ROOT / "requirements-ci.txt"
JOBCORE_CLONE = REPO_ROOT.parent / "jobcore"

#: `jobcore @ git+https://github.com/Sundeepg98/jobcore@<40 hex>`
PIN_RE = re.compile(
    r"^jobcore\s*@\s*git\+\S+?@([0-9a-fA-F]{7,40})\s*$", re.MULTILINE)

#: `from jobcore.X import ...` / `import jobcore.X`
IMPORT_RE = re.compile(
    r"^\s*(?:from\s+jobcore\.(\w+)\s+import|import\s+jobcore\.(\w+))",
    re.MULTILINE)

#: `from jobcore import X` -- X may be a module or a re-exported name, so these
#: are checked against the tree too but tolerated when they are not files.
FROM_PACKAGE_RE = re.compile(r"^\s*from\s+jobcore\s+import\s+(.+)$", re.MULTILINE)


def _pinned_sha() -> str:
    text = REQUIREMENTS_CI.read_text(encoding="utf-8")
    match = PIN_RE.search(text)
    assert match, "no jobcore pin line found in requirements-ci.txt"
    return match.group(1)


def _modules_at(sha: str):
    """Module names in jobcore's package tree at `sha`, or None if unknowable."""
    if not (JOBCORE_CLONE / ".git").exists():
        return None
    probe = subprocess.run(
        ["git", "-C", str(JOBCORE_CLONE), "cat-file", "-e", "%s^{commit}" % sha],
        capture_output=True, text=True)
    if probe.returncode != 0:
        return None
    listing = subprocess.run(
        ["git", "-C", str(JOBCORE_CLONE), "ls-tree", "--name-only", sha,
         "src/jobcore/"],
        capture_output=True, text=True)
    if listing.returncode != 0:
        return None
    names = set()
    for line in listing.stdout.splitlines():
        name = Path(line.strip()).name
        if name.endswith(".py"):
            names.add(name[:-3])
        elif name:
            names.add(name)
    return names


def _imported_submodules():
    """Every `jobcore.<module>` named anywhere in naukri_server/."""
    found = set()
    for path in (REPO_ROOT / "naukri_server").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for a, b in IMPORT_RE.findall(text):
            found.add(a or b)
        for names in FROM_PACKAGE_RE.findall(text):
            for chunk in names.split("#")[0].split(","):
                name = chunk.strip().split(" as ")[0].strip().strip("()")
                if name and name.isidentifier():
                    found.add(name)
    return found


def test_the_pin_line_is_present_and_is_an_exact_commit():
    """A moving `@master` would let another repo turn this repo's CI red."""
    sha = _pinned_sha()
    assert len(sha) >= 7
    assert re.fullmatch(r"[0-9a-fA-F]+", sha)


def test_every_jobcore_module_this_package_imports_exists_at_the_pinned_commit():
    """The check that would have caught 8a8f596 before it was pushed."""
    sha = _pinned_sha()
    available = _modules_at(sha)
    if available is None:
        pytest.skip(
            "../jobcore clone is absent or does not know commit %s; this check "
            "is offline by design and does not fetch" % sha)

    imported = _imported_submodules()
    assert imported, "no jobcore imports found at all -- the scanner is broken"

    # `from jobcore import X` also yields re-exported names that are not
    # modules; only flag names that are modules in the CURRENT tree, so a
    # re-exported symbol is not mistaken for a missing module.
    current = _modules_at("HEAD") or set()
    missing = sorted(
        name for name in imported
        if name not in available and name in current
    )
    assert not missing, (
        "requirements-ci.txt pins jobcore at %s, which does NOT contain %s -- "
        "CI will die at collection with ModuleNotFoundError while every local "
        "run stays green (the local venv has jobcore editable from ../jobcore). "
        "Bump the pin in the SAME commit that adds the import."
        % (sha[:12], ", ".join("jobcore.%s" % m for m in missing))
    )


def test_the_scanner_actually_sees_the_imports_it_is_meant_to_guard():
    """Guards the guard.

    A scanner that silently found nothing would make the test above pass
    vacuously forever. These three are imported by naukri_server today:
    `config` (policy.py), `buildinfo` (buildinfo.py), `paths` (policy.py).
    """
    imported = _imported_submodules()
    for expected in ("config", "buildinfo", "paths", "policy"):
        assert expected in imported, (
            "scanner missed `jobcore.%s`, which naukri_server does import"
            % expected
        )
