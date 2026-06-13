"""Shared test fixtures for Naukri MCP tests."""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


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
