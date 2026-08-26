"""Shared test fixtures for Naukri MCP tests."""

import asyncio
import importlib
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# HARD-DEPENDENCY PREFLIGHT - runs BEFORE collection, so a broken environment
# reports one actionable line instead of a wall of import errors.
#
# MEASURED 2026-08-22, both mechanisms, on this repo:
#
#   1. With `jobcore` unimportable, `pytest tests/` produces "Interrupted: 50
#      errors during collection" and exits 2. That is loud on its own - but
#      `pytest ... | tail -5` reports the PIPELINE's exit code, which is
#      tail's 0. A caller that trusts the exit code sees a broken run as a
#      passing one, and the 50 ERROR lines say nothing about what to do.
#   2. `pytest.importorskip("jobcore.scoring")` converted the same missing
#      dependency into SKIPS at four sites, and a skip reads as green. See
#      tests/test_suite_integrity.py, which now bans that on a hard dep.
#
# This preflight answers (1): it aborts at sessionstart, so its message is the
# LAST thing printed and survives a `| tail`. It names the remediation, which
# a collection traceback does not.
#
# The check is a plain function so it can be pointed at synthetic input and
# shown both detecting and staying silent - a guard that has never been run
# against a known-bad environment certifies nothing.
# ---------------------------------------------------------------------------

#: Every module the package imports UNCONDITIONALLY. Not "nice to have":
#: naukri_server/domain/salary.py, scoring.py and policy.py all import these
#: at module scope, so if one is missing the package does not import at all.
HARD_DEPENDENCIES = (
    "jobcore",
    "jobcore.buildinfo",
    "jobcore.config",
    "jobcore.fit",
    "jobcore.paths",
    "jobcore.policy",
    "jobcore.salary",
    "jobcore.scoring",
    "jobcore.skills",
)

REMEDIATION = "python -m pip install -e ../jobcore"

#: The likelier fix on a dev box, and the one that actually applied on
#: 2026-08-22: the repo ships its own venv (the MCP server runs from it), and
#: `python` on PATH was the Windows Store interpreter, which has never had
#: jobcore. The dependency was not missing from the project - the run was
#: pointed at the wrong interpreter, which no "pip install" message would have
#: told anyone. Named first because it costs nothing to check.
VENV_HINT = "naukri/venv/Scripts/python.exe -m pytest tests/ -q"


def missing_hard_dependencies(names=HARD_DEPENDENCIES):
    """Return [(module_name, exception)] for every name that will not import.

    Deliberately catches Exception, not just ImportError: a dependency that
    imports but raises on import is just as broken, and a bare ImportError
    catch would let that one through silently.
    """
    missing = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - any failure is a broken dep
            missing.append((name, exc))
    return missing


def pytest_collection_finish(session):
    """Record which test MODULES actually contribute a test that will RUN.

    `pytest_collection_finish`, not `pytest_collection_modifyitems`: marker
    deselection (`addopts = -m "not e2e"`) is itself implemented as a
    modifyitems hook, and hook ordering meant an earlier version of this saw
    the PRE-deselection list - so a module whose every test had been deselected
    still counted as contributing. That is precisely the silent shrinkage this
    exists to catch, so it has to measure what SURVIVES selection.
    `session.items` at collection_finish is that final list.

    Stashed on `config` rather than a module global: pytest may load this
    conftest under a different module identity than a plain
    `from tests.conftest import ...` would resolve, and a stale empty set would
    make the completeness check vacuous.
    """
    modules = set()
    for item in session.items:
        path = getattr(item, "path", None) or getattr(item, "fspath", None)
        if path is not None:
            modules.add(Path(str(path)).name)
    session.config._naukri_collected_modules = modules


def pytest_sessionstart(session):
    missing = missing_hard_dependencies()
    if not missing:
        return
    raise pytest.UsageError(
        "HARD DEPENDENCY MISSING - this suite cannot run and any result it "
        "printed would be measuring a partial tree.\n"
        + "\n".join("    %s -> %s: %s" % (name, type(exc).__name__, exc)
                    for name, exc in missing)
        + "\n\n    CHECK THE INTERPRETER FIRST - this repo ships its own venv "
          "and the\n    server runs from it:\n        %s\n" % VENV_HINT
        + "\n    Otherwise install it into the interpreter you are using:\n"
          "        %s\n" % REMEDIATION
        + "    (jobcore is installed EDITABLE from the sibling checkout on "
          "purpose - putting its git URL in requirements.txt clobbers the "
          "editable install. See requirements-ci.txt.)"
    )


# ---------------------------------------------------------------------------
# Config isolation — no test ever reads the operator's real jobhunt.json.
#
# Without this the suite becomes machine-dependent: a weight he edits on his
# laptop would change assertions here, and CI (which has no such file) and this
# box would disagree about what "the score" is. That is the single largest risk
# in the whole config design, and it is why `locate()` has an explicit no-file
# mode rather than only a path.
#
# `:none:` is an EXPLICIT disable token. An EMPTY value means "unset, keep
# searching" — CI runners, shell scripts and MCP `env` blocks produce an empty
# var by accident all the time, and one stray `JOBHUNT_CONFIG=` silently
# running every server on defaults would be the worst possible failure mode for
# a config system.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUNT_CONFIG", ":none:")
    monkeypatch.delenv("JOBHUNT_HOME", raising=False)
    monkeypatch.delenv("JOBHUNT_DISABLE", raising=False)

    # And no test writes RUNTIME STATE into the repo. `agent.POLICY_STATE_PATH`
    # defaults to DATA_DIR/agent_policy_state.json, and DATA_DIR is the repo
    # root — so a test that runs an agent cycle silently dirtied the working
    # tree, exactly as an un-isolated DB_PATH would. Caught the hard way: it
    # got committed once. Same treatment as _isolated_test_db below.
    try:
        from naukri_server import agent as _agent

        monkeypatch.setattr(_agent, "POLICY_STATE_PATH",
                            tmp_path / "agent_policy_state.json")
    except Exception:  # pragma: no cover
        pass

    try:
        from naukri_server import policy as _policy
    except Exception:  # pragma: no cover - jobcore missing in a bare checkout
        yield
        return
    _policy.invalidate()
    try:
        yield
    finally:
        _policy.invalidate()


# ---------------------------------------------------------------------------
# Snapshot isolation - no test writes a profile snapshot into the checkout.
#
# Exactly the reason `agent.POLICY_STATE_PATH` is redirected above:
# `profile_snapshot._SNAPSHOT_DIR` is `DATA_DIR / "profile_snapshots"` and
# DATA_DIR is the repo root, so an un-isolated save dirties the working tree.
# These files carry the FULL live profile read, which is the one artifact that
# must never reach a commit. tests/test_no_tree_writes.py is what notices.
#
# It lives HERE rather than only in tests/test_profile_snapshot.py because the
# WRITE paths now call `save_snapshot` themselves: every test that exercises
# `profile_sections.write_section` or a restore takes a pre-write snapshot, in
# whatever module that test happens to live.
#
# FUNCTION-SCOPED, not session-scoped, deliberately. `monkeypatch` and
# `tmp_path` are function-scoped; more to the point, the snapshot tests COUNT
# files in this directory (`test_listing_orders_newest_first`,
# `test_prune_keeps_exactly_the_n_newest`), and one directory shared across
# the whole session would make those depend on execution order.
#
# tests/test_profile_snapshot.py defines a fixture of the same name, which
# SHADOWS this one for that module - same redirect, same target, so that
# module behaves identically with or without it. This one covers every other
# module.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def snapshot_home(monkeypatch, tmp_path):
    """Redirect the profile-snapshot directory to a per-test tmp dir.

    Returns the target path. Nothing is created here: `snapshot_dir()` is
    pure and `save_snapshot` is the only thing that ever mkdirs it.
    """
    target = tmp_path / "profile_snapshots"
    try:
        from naukri_server.tools import profile_snapshot as _ps
    except Exception:  # pragma: no cover - bare checkout without jobcore
        return target
    monkeypatch.setattr(_ps, "_SNAPSHOT_DIR", target)
    return target


# ---------------------------------------------------------------------------
# DB isolation — point DB_PATH at a session-tmp file and create tables once.
# Without this, any test that calls a database helper without patching it
# (e.g. tests/test_workflows_integration.py::test_prep_step_fails_gracefully
# which exercises the real follow_up_reminder saga step) hits the real
# project-root naukri.db, which may be missing or have a stale schema.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _isolated_test_db(tmp_path_factory):
    """Redirect naukri_server.database.DB_PATH to a session-scoped tmp file.

    Creates fresh tables via init_db(). Every test that touches the DB without
    patching now sees an isolated, fully-migrated schema. Runs exactly once.

    The patch is held for the full session via patcher.start()/stop() — using
    a `with patch` block would revert the redirect as soon as the fixture
    yielded, leaving subsequent tests pointing at the real project DB again.
    """
    tmp_db = tmp_path_factory.mktemp("naukri-test-db") / "naukri.db"
    patcher = patch("naukri_server.database.DB_PATH", tmp_db)
    patcher.start()
    try:
        from naukri_server.database import init_db
        # init_db is async — run it on a fresh loop because session fixtures
        # execute outside any pytest-asyncio test loop.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(init_db())
        finally:
            loop.close()
        yield tmp_db
    finally:
        patcher.stop()


@pytest.fixture
def mock_api_client():
    """Pre-configured mock for api_client.get/post."""
    client = MagicMock()
    client.get = AsyncMock(return_value={})
    client.post = AsyncMock(return_value={})
    return client


@pytest.fixture
def sample_job():
    """Standard job dict for reuse across tests."""
    return {
        "job_id": "TEST001",
        "title": "Senior Backend Developer",
        "company": "TestCorp",
        "salary": "20-30 LPA",
        "salary_min_lakhs": 20.0,
        "salary_max_lakhs": 30.0,
        "location": "Bengaluru",
        "experience": "3-8 Yrs",
        "experience_min": 3,
        "experience_max": 8,
        "tags": ["Node.js", "TypeScript", "AWS"],
        "is_applied": False,
        "is_agent_eligible": False,
    }


@pytest.fixture
def sample_application():
    """Standard application dict for reuse."""
    return {
        "job_id": "TEST001",
        "title": "Senior Backend Developer",
        "company": "TestCorp",
        "status": "applied",
        "applied_at": "2026-03-01T10:00:00+00:00",
        "source": "single",
    }


@pytest.fixture(autouse=True)
def _clear_route_caches():
    """Reset dashboard route caches between tests so cached state doesn't leak."""
    yield
    try:
        from naukri_server.dashboard.routes import _reset_caches
        _reset_caches()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _reset_apply_rate_limiter():
    """Reset the process-wide apply RateLimiter between tests.

    The limiter is a module-level singleton that accumulates call timestamps;
    without this, timestamps from earlier tests could fill the window and make
    a later test that calls the real apply path block on a sleep. Resetting to
    None forces a fresh, empty limiter to be lazily rebuilt per test.
    """
    import naukri_server.resilience as _resilience
    _resilience._apply_rate_limiter = None
    yield
    _resilience._apply_rate_limiter = None


@pytest.fixture
def sample_profile():
    """Standard profile for reuse."""
    return {
        "status": "success",
        "name": "Test User",
        "key_skills": ["Node.js", "TypeScript", "AWS", "PostgreSQL", "Docker"],
        "total_experience": "5 years 0 months",
        "current_location": "Bengaluru",
        "expected_ctc": "25",
    }


@pytest.fixture(autouse=True)
def _never_write_the_live_auth_state(tmp_path, monkeypatch):
    """No test may write the real `chrome-profile/auth_state.json`.

    MEASURED 2026-08-26, and it was live: a test built a real `TokenManager`,
    bound a fake context returning `nauk_at="jwt"`, and called `extract()`.
    `extract()` unconditionally calls `_export_auth_state`, which writes
    `_AUTH_STATE_FILE` -- and nothing patched it. So EVERY full pytest run
    overwrote the operator's credential file with::

        {"token": "jwt", "cookies": "nauk_at=jwt"}

    That file is the cross-process auth bridge. Clobbering it breaks the token
    for every other agent and script on this machine, not just for the test.

    THE FAILURE WAS SILENT IN BOTH DIRECTIONS, which is why it survived:
    `get_auth_state()` accepts the stub rather than rejecting it -- `jwt_exp("jwt")`
    is None, so the expiry gate cannot fire, and the file-age fallback sees a
    file written seconds ago and calls it fresh. The result is a credential
    that looks valid to every check and returns 401 from Naukri. Hours were
    spent this week attributing exactly that 401 to "a transport shape I could
    not find".

    Autouse and suite-wide ON PURPOSE. Patching the one test that does it today
    fixes today; the next test to touch `extract()` re-breaks it, and the
    breakage is invisible until someone's unrelated probe starts failing.
    """
    import naukri_server.auth_bridge as _bridge
    import naukri_server.browser as _browser

    fake = tmp_path / "auth_state.json"

    # `_export_auth_state` writes `self._AUTH_STATE_FILE` -- a CLASS attribute
    # on TokenManager, not the module-level name. Patching the module alone
    # looks right, runs green, and still clobbers the real file; that was
    # measured before this line was written.
    monkeypatch.setattr(_browser.TokenManager, "_AUTH_STATE_FILE", fake,
                        raising=False)
    monkeypatch.setattr(_bridge, "_AUTH_STATE_FILE", fake, raising=False)
    monkeypatch.setattr(_browser, "_AUTH_STATE_FILE", fake, raising=False)
    yield
