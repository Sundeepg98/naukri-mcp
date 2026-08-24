# Local replication of GitHub Actions CI (CI is billing-blocked)

Date: 2026-08-24
Repo: mcp-servers/naukri
Commit under test: 159b266a22b27c526f415ac4adb4d2c29814d5cc
Verdict: THE CODE WOULD PASS CI ON ALL THREE MATRIX VERSIONS.

## 1. Why this exists

Actions run 32705833680 reported `failure` on all three matrix jobs, but the jobs
never started: 0 steps, 2 seconds wall time, and the check-run annotation reads
"The job was not started because recent account payments have failed or your
spending limit needs to be increased." The repo is private, so Actions minutes
are metered. Last green run was c3550d3 on 2026-08-23T11:54Z.

That is a BILLING BLOCK, not a code failure, and must not be recorded as one.
No billing or spending setting was inspected or changed; that surface is the
operator's alone.

This document is the CI-equivalent evidence produced locally instead.

## 2. What CI does (read from .github/workflows/ci.yml)

    runs-on: ubuntu-latest
    matrix: python-version ["3.10", "3.11", "3.12"], fail-fast: false
    python -m pip install --upgrade pip
    pip install -r requirements-ci.txt
    pip install pytest pytest-asyncio
    pytest

`pytest.ini` sets `addopts = -m "not e2e"`, so the 8 e2e items are deselected.
`requirements-ci.txt` is `-r requirements.txt` PLUS jobcore from an exact git
pin; the runner has no `../jobcore` checkout. Locally jobcore is installed
EDITABLE from the sibling repo, so the pinned git version is a real difference
in what gets imported. That asymmetry is the reason this exercise exists.

## 3. Interpreters used (none installed; all three already on the machine)

| Matrix version | Interpreter found | Path |
|---|---|---|
| 3.10 | CPython 3.10.19 | `D:\dev-cache-local\uv-python\cpython-3.10.19-windows-x86_64-none\python.exe` |
| 3.11 | CPython 3.11.14 | `%APPDATA%\uv\python\cpython-3.11.14-windows-x86_64-none\python.exe` |
| 3.12 | CPython 3.12.12 | `D:\dev-cache-local\uv-python\cpython-3.12.12-windows-x86_64-none\python.exe` |

All three matrix versions were available, so NONE was skipped. No Python
runtime was installed. `py` is not present on this machine; the runtimes were
located via `uv python list` plus a direct scan of the uv python roots (the
3.11 build lives in the legacy `%APPDATA%\uv\python` dir that `uv python list`
no longer reports, because `UV_PYTHON_INSTALL_DIR` now points at
`D:\dev-cache-local\uv-python`).

Platform difference from CI that cannot be removed here: CI is
`ubuntu-latest`, these runs are `win32`. Everything else is matched.

## 4. Venv construction (outside the repo, in the scratchpad)

    SCRATCH=%LOCALAPPDATA%\Temp\claude\D--workspace-projects-mcp-servers\<session-id>\scratchpad

    <base-python> -m venv --without-pip $SCRATCH\ci-py<VER>
    # bootstrap pip ONLY (see note), then exactly what CI runs:
    $SCRATCH\ci-py<VER>\Scripts\python.exe -m pip install --upgrade pip
    $SCRATCH\ci-py<VER>\Scripts\python.exe -m pip install -r <repo>\requirements-ci.txt
    $SCRATCH\ci-py<VER>\Scripts\python.exe -m pip install pytest pytest-asyncio

NOTE, and it is a Windows artifact rather than anything about this repo: a
plain `python -m venv` FAILS for 3.10 and 3.11 at this scratchpad path.
`ensurepip` on those versions installs setuptools as well as pip, and
setuptools ships
`pkg_resources/tests/data/my-test-package_unpacked-egg/my_test_package-1.0-py3.7.egg/EGG-INFO`,
which pushes past the 260-character MAX_PATH limit under a 131-character
scratchpad prefix. Python 3.12 dropped setuptools from `ensurepip`, which is
exactly why 3.12 built first try. Fix used: create the venv `--without-pip`
and install ONLY pip from the interpreter's own bundled wheel. The resulting
venvs are structurally identical to the 3.12 one (pip, no setuptools), and
nothing in `requirements-ci.txt` needs setuptools at runtime.

Nothing was installed into the repo's own `venv\`. The scratchpad venvs are
fully separate and hold a NON-editable jobcore; the repo venv's editable
install was re-verified intact afterwards and still resolves to
`mcp-servers\jobcore\src\jobcore\__init__.py`.

Key resolved versions (identical across all three venvs unless noted):
mcp 1.29.0 (the `<2` cap in requirements.txt held on a clean resolve, which is
what that cap exists for), playwright 1.62.0, aiohttp 3.14.3, pydantic 2.13.4,
pytest 9.1.1, pytest-asyncio 1.4.0. Python 3.10 additionally pulled the
`async-timeout` and `exceptiongroup` backports, as its requirement markers
intend. Benign resolver drift: rpds-py 0.30.0 on 3.10 vs 2026.6.3 on 3.11/3.12.

CI installs `pytest` UNPINNED and therefore got pytest 9.1.1; the repo venv
holds pytest 9.0.2. That is a real, permanent CI-vs-local difference, and it
did not change any outcome here.

## 5. The jobcore pin

Pin line in `requirements-ci.txt`:

    jobcore @ git+https://github.com/Sundeepg98/jobcore@6acc7e6c6949676863a4e22e68bcfeae479f5026

Resolved, identically in all three venvs, read back from the installed
`direct_url.json`:

    {"url": "https://github.com/Sundeepg98/jobcore",
     "vcs_info": {"commit_id": "6acc7e6c6949676863a4e22e68bcfeae479f5026",
                  "requested_revision": "6acc7e6c6949676863a4e22e68bcfeae479f5026",
                  "vcs": "git"}}

Local sibling checkout `mcp-servers/jobcore`: HEAD
6acc7e6c6949676863a4e22e68bcfeae479f5026, branch master, clean working tree,
and level with `origin/master` (0 ahead, 0 behind). The commit is present on
the remote, so CI can resolve the pin.

THE PIN IS NOT STALE. Pinned commit == local editable checkout commit ==
origin/master. Installed distribution version 0.2.0 in both.

Content was compared file by file rather than inferred from the SHA. The only
differences found were CRLF vs LF line terminators: pip's git clone on Windows
applies `core.autocrlf`, the local checkout does not. Byte-identical once CR
is stripped, across every differing file (`__init__.py`, `salary.py`,
`scoring.py`). Semantically identical; on a Linux runner the question does not
arise.

## 6. Results

### 6.1 Authoritative runs -- frozen clone at 159b266

Because the working tree would not hold still (section 7), the authoritative
measurement was taken on a clone of the repo made INTO the scratchpad and
checked out at exactly the commit under test. Cloning only reads the source
repo; every write landed in the scratchpad. This also matches CI more closely
than the live tree does: `actions/checkout` gives a pristine tree with no
uncommitted work and no `../jobcore` sibling, which is precisely what the
clone is.

    git clone --no-hardlinks <repo> $SCRATCH\nk-ci
    cd $SCRATCH\nk-ci && git checkout 159b266a22b27c526f415ac4adb4d2c29814d5cc
    $SCRATCH\ci-py<VER>\Scripts\pytest.exe

Clone HEAD and `git status --porcelain` were sampled before and after every
run; HEAD stayed 159b266 and status stayed empty throughout all three.

| Version | Result | Exit | Exact pytest summary line |
|---|---|---|---|
| 3.10.19 | PASS | 0 | `==== 3210 passed, 4 skipped, 8 deselected, 1 warning in 299.27s (0:04:59) =====` |
| 3.11.14 | PASS | 0 | `==== 3210 passed, 4 skipped, 8 deselected, 1 warning in 228.76s (0:03:48) =====` |
| 3.12.12 | PASS | 0 | `==== 3210 passed, 4 skipped, 8 deselected, 1 warning in 233.18s (0:03:53) =====` |

Collection was identical on all three: `collected 3222 items / 8 deselected /
3214 selected`. No version-specific behaviour of any kind appeared.

### 6.2 The 4 skips, and why they are not a regression

3210 passed + 4 skipped = 3214 selected, the same 3214 the local reference
runs. The brief's local reference of "3214 passed, 0 skipped" was taken in the
repo root, where the `../jobcore` sibling EXISTS; the clone has no sibling,
exactly like CI. Skip reasons, read off a `-rs` run:

    SKIPPED tests\test_jobcore_pin.py:215: ../jobcore clone is absent or does not
        know commit 6acc7e6...; this check is offline by design and does not fetch
    SKIPPED tests\test_jobcore_pin.py:325: ../jobcore does not know d1720c3
    SKIPPED tests\test_jobcore_pin.py:349: ../jobcore does not know 6acc7e6...
    SKIPPED tests\test_server_info.py:240: jobcore is not a git work tree here

The first three are the pin checker skipping by explicit design when the
sibling clone is absent -- its own docstring says it "runs off the local
../jobcore clone and skips when that clone is absent". These three skip on
real CI too.

The fourth is the one genuine environment-driven difference, and it is the
difference this exercise was built to surface: under the git pin jobcore is an
ordinary installed package, not a git work tree, so the check has nothing to
interrogate. It skips on real CI for the same reason.

A differential run isolates it. The repo's OWN venv (Python 3.13.14, pytest
9.0.2, jobcore EDITABLE from the sibling work tree) pointed at the SAME frozen
clone gives:

    ========== 3211 passed, 3 skipped, 8 deselected in 206.97s (0:03:26) ==========

3211 + 3 = 3214. Same selected set; the single delta versus the CI-shaped
environment is `test_server_info.py:240`, which runs under the editable
install and skips under the git pin. Everything else is identical.

### 6.3 Tests that pass locally but fail under the CI-shaped environment

NONE. Zero.

Across all three CI-shaped Python versions on the commit under test there were
no failures and no errors -- only the 4 skips above, every one of them
accounted for and every one of them expected on the real runner. No test
changes status from pass to fail between the repo venv and the CI-shaped
environment.

### 6.4 First-pass runs in the live repo root (superseded, recorded for honesty)

The first three runs were done as briefed, from the repo working tree. They
are NOT clean measurements, because other agents were writing to that tree
while they ran (section 7). Recorded so the numbers are not mistaken for
version-specific behaviour if they surface elsewhere:

| Version | Exit | Collected | Exact pytest summary line |
|---|---|---|---|
| 3.10.19 | 0 | 3222 | `==== 3213 passed, 1 skipped, 8 deselected, 1 warning in 198.23s (0:03:18) =====` |
| 3.11.14 | 1 | 3222 | `= 2 failed, 3211 passed, 1 skipped, 8 deselected, 1 warning in 279.92s (0:04:39) =` |
| 3.12.12 | 0 | 3255 | `==== 3246 passed, 1 skipped, 8 deselected, 1 warning in 233.57s (0:03:53) =====` |

The two failures on 3.11 are a concurrent-write race, NOT a Python 3.11
defect. Both are in `tests/test_suite_integrity.py`:

    FAILED tests/test_suite_integrity.py::TestEveryModuleContributes::test_CONTROL_the_live_comparison_can_produce_a_hit
    FAILED tests/test_suite_integrity.py::TestEveryModuleContributes::test_every_test_module_on_disk_contributed_a_test

    AssertionError: test module(s) on disk contributed ZERO collected tests.
    Either they are broken or they are deselected - both make the suite
    smaller than it looks:
        test_profile_section_id_mapping.py

That guard globs `test_*.py` from disk at assertion time and compares it
against the modules pytest collected at startup. Another agent created
`tests/test_profile_section_id_mapping.py` BETWEEN those two moments, so the
file was on disk but had not been collected. The collection counts corroborate
it exactly: 3222 collected on 3.10 and 3.11, then 3255 on 3.12 once the new
file was present at startup -- the same file, +33 tests. The guard worked
correctly; it simply fired on a live-tree race. On the frozen clone all three
versions collect 3222 and pass.

## 7. Tree state -- the brief's premise did not hold

The brief specified HEAD 159b266, clean, read-and-run. That was true when
sampled at the start and stopped being true during the work. Other agents in
this session are actively writing to and committing in this repo.

Observed, with timestamps:

- start of slice: HEAD 159b266, `git status --porcelain` EMPTY (verified)
- 14:01:25 a commit landed, moving HEAD to 75d7d2f
- 14:05:48 `naukri_server/tools/profile_sections.py` modified in the working
  tree (+57/-17, a `read_id_field` feature edit -- not something a test run
  can produce)
- during the 3.11 run: `tests/test_profile_section_id_mapping.py` created
- later: `_audit/2026-08-24-naukri-profile-writes.md` modified and
  `_audit/_slices/naukri-id-mapping-slice.md` created

The HEAD move 159b266..75d7d2f is DOCS-ONLY -- a single file,
`_audit/2026-08-24-naukri-profile-writes.md`, +237 lines, no code -- and
159b266 is a fast-forward ancestor of 75d7d2f, no history rewrite. So the
commit move does not affect the verdict. The uncommitted code edit and the new
test file are what contaminated the first-pass runs, and are the reason the
authoritative measurement was moved onto a frozen clone.

Nothing in the source repo was reverted, stashed, checked out, staged or
committed. The other agents' work was left exactly as found.

### Final working-tree state

    HEAD=75d7d2f51dc70a16635e66f7999e2689e4222305
     M _audit/2026-08-24-naukri-profile-writes.md
     M naukri_server/tools/profile_sections.py
    ?? _audit/_slices/naukri-id-mapping-slice.md
    ?? tests/test_profile_section_id_mapping.py

`git status --porcelain` is NOT empty at the end of this slice. Every entry
above belongs to another agent and was present before this slice touched
anything; none of it is this slice's work. This slice's only writes to the
repo are this file and gitignored caches (`__pycache__/` via .gitignore,
`.pytest_cache/` via its own self-written `.gitignore` containing `*`), so it
contributes no visible working-tree change of its own.

The live MCP server (PID 43228) was confirmed still running at the end of the
slice and was never restarted or signalled.

## 8. Verdict

The code at 159b266 passes the full CI matrix -- Python 3.10, 3.11 and 3.12,
CI-shaped environment with the git-pinned jobcore, 3210 passed / 4 skipped /
8 deselected / 0 failed on every one of them -- so run 32705833680 is a
billing block and nothing else, and no code fix is owed.
