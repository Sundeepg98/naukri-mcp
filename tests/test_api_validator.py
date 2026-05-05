"""Tests for naukri_server.probes.api_validator — pure unit, mocked.

The api_validator probe hits 5 endpoints, snapshots schemas on first run, and
reports drift on subsequent runs. We mock _fetch_one (the sole I/O point) so
no network call ever happens, and we reset the module-level drift detector
before each test to keep state isolated.
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.probes import api_validator
from naukri_server.probes.drift_detector import detector as _drift_detector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_drift_detector():
    """Clear the module-level drift detector before AND after every test.

    The detector is a singleton; without this, snapshots from one test would
    leak into the next and make assertions order-dependent.
    """
    _drift_detector.clear()
    yield
    _drift_detector.clear()


def _ok_response_for(label: str) -> dict:
    """Build a small canonical response per endpoint label."""
    return {
        "profile": {"name": "alice", "experience": 5, "skills": ["py"]},
        "dashboard": {"profileViews": 42, "completeness": 80},
        "applications": {"applyDetails": [{"id": "j1"}], "matchingRowsCount": 1},
        "recommendations": {"jobDetails": [{"id": "j2", "title": "SDE"}]},
        "settings": {"sections": [{"name": "notifications", "settings": []}]},
    }[label]


# ---------------------------------------------------------------------------
# Probe registration
# ---------------------------------------------------------------------------


def test_probe_registered():
    """Importing the package must register api_validator with the probe registry."""
    import naukri_server.probes  # noqa: F401 — triggers registration
    from naukri_server.health import probe_registry

    probe = probe_registry.get("probes.api_validator")
    assert probe is not None, "api_validator probe must be registered after package import"


def test_probe_metadata_correct():
    """Verify the registered probe carries our intended metadata."""
    import naukri_server.probes  # noqa: F401
    from naukri_server.health import probe_registry

    probe = probe_registry.get("probes.api_validator")
    assert probe is not None
    assert probe.criticality == "informational"
    assert probe.interval_seconds == 900  # 15 minutes
    assert probe.subsystem == "probes"


# ---------------------------------------------------------------------------
# First-run behavior — snapshot only, no drift report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_snapshots_all_endpoints_no_drift():
    """First run with all 5 endpoints OK should snapshot each and report healthy."""
    async def fake_fetch(path, params):
        # Reverse-map path back to label by checking which constant matches.
        # Easier: just identify by substring.
        if "users/self" in path and "settings" not in path:
            return _ok_response_for("profile")
        if "dashboard" in path:
            return _ok_response_for("dashboard")
        if "history" in path:
            return _ok_response_for("applications")
        if "recommend" in path or "rec" in path.lower():
            return _ok_response_for("recommendations")
        if "settings" in path:
            return _ok_response_for("settings")
        return {"unknown": True}

    with patch.object(api_validator, "_fetch_one",
                       new=AsyncMock(side_effect=fake_fetch)):
        result = await api_validator.api_validator_probe()

    assert result.status == "healthy"
    assert "5/5 endpoints OK, no drift" in result.message
    assert result.metadata["endpoints_total"] == 5
    assert result.metadata["endpoints_ok"] == 5
    assert result.metadata["endpoints_failed"] == 0
    assert result.metadata["drift_detected"] == 0


# ---------------------------------------------------------------------------
# Second-run behavior — same shape → still healthy, no drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_run_same_shape_stays_healthy():
    """Run twice with identical responses; second run should NOT report drift."""
    fixed = {"name": "alice", "experience": 5}

    async def fake(path, params):
        return fixed

    with patch.object(api_validator, "_fetch_one",
                       new=AsyncMock(side_effect=fake)):
        first = await api_validator.api_validator_probe()
        second = await api_validator.api_validator_probe()

    assert first.status == "healthy"
    assert second.status == "healthy"
    assert second.metadata["drift_detected"] == 0


# ---------------------------------------------------------------------------
# Second-run behavior — drift detected → degraded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_run_with_drift_returns_degraded():
    """Snapshot one shape, return a different shape → degraded with drift metadata."""
    call_count = {"n": 0}

    async def fake(path, params):
        call_count["n"] += 1
        # First batch (calls 1-5): canonical shape.
        # Second batch (calls 6-10): added a new field.
        if call_count["n"] <= 5:
            return {"a": 1, "b": "hi"}
        return {"a": 1, "b": "hi", "newField": True}

    with patch.object(api_validator, "_fetch_one",
                       new=AsyncMock(side_effect=fake)):
        first = await api_validator.api_validator_probe()  # snapshot
        second = await api_validator.api_validator_probe()  # drift

    assert first.status == "healthy"
    assert second.status == "degraded"
    assert "drift" in second.message.lower()
    assert second.metadata["drift_detected"] == 5  # all 5 endpoints drifted
    assert second.metadata["drift_severity"] == "added"
    assert "drift_details" in second.metadata


@pytest.mark.asyncio
async def test_drift_severity_promotes_to_worst():
    """When endpoints have different drift severities, report the worst one."""
    call_count = {"n": 0}

    async def fake(path, params):
        call_count["n"] += 1
        # First batch: baseline.
        if call_count["n"] <= 5:
            return {"a": 1, "b": "hi"}
        # Second batch: first endpoint just adds, others REMOVE — removed wins.
        if call_count["n"] == 6:
            return {"a": 1, "b": "hi", "extra": "yes"}  # added only
        return {"a": 1}  # removed b

    with patch.object(api_validator, "_fetch_one",
                       new=AsyncMock(side_effect=fake)):
        await api_validator.api_validator_probe()  # snapshot
        result = await api_validator.api_validator_probe()  # mixed drift

    assert result.metadata["drift_severity"] == "removed"


# ---------------------------------------------------------------------------
# Failure handling — endpoint raises → tracked in failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_failure_returns_degraded():
    """Some endpoints succeed, some raise → degraded status with failures list."""
    call_count = {"n": 0}

    async def fake(path, params):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("boom")
        return {"x": 1}

    with patch.object(api_validator, "_fetch_one",
                       new=AsyncMock(side_effect=fake)):
        result = await api_validator.api_validator_probe()

    assert result.status == "degraded"
    assert "1 of 5 endpoints failed" in result.message
    assert result.metadata["endpoints_ok"] == 4
    assert result.metadata["endpoints_failed"] == 1
    assert "failures" in result.metadata
    # The failing endpoint label should be one of the 5 critical ones.
    assert len(result.metadata["failures"]) == 1


@pytest.mark.asyncio
async def test_all_failures_returns_unhealthy():
    """If every endpoint raises, status escalates to unhealthy."""
    async def fake(path, params):
        raise RuntimeError("everything is broken")

    with patch.object(api_validator, "_fetch_one",
                       new=AsyncMock(side_effect=fake)):
        result = await api_validator.api_validator_probe()

    assert result.status == "unhealthy"
    assert "All 5 endpoints failed" in result.message
    assert result.metadata["endpoints_failed"] == 5
    assert result.metadata["endpoints_ok"] == 0


# ---------------------------------------------------------------------------
# Probe is wrapped — exceptions inside the probe must be caught by health framework
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_handles_unexpected_exception_gracefully():
    """The @health_probe wrapper should catch any exception _fetch_one didn't."""
    async def fake(path, params):
        raise RuntimeError("simulated outer failure")

    with patch.object(api_validator, "_fetch_one",
                       new=AsyncMock(side_effect=fake)):
        # Even though every fetch raises, the outer probe still returns a
        # ProbeResult — it doesn't itself raise.
        result = await api_validator.api_validator_probe()

    assert result.status == "unhealthy"


# ---------------------------------------------------------------------------
# Config-constant resolution — missing constant doesn't crash the probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_config_constant_is_recorded_as_failure():
    """If a config constant disappeared, the probe records it as a failure
    rather than crashing."""
    async def fake(path, params):
        return {"x": 1}

    # Patch _resolve_path so one specific constant lookup raises AttributeError.
    real_resolve = api_validator._resolve_path

    def fake_resolve(name):
        if name == "DASHBOARD_API":
            raise AttributeError(f"module has no attribute {name!r}")
        return real_resolve(name)

    with patch.object(api_validator, "_fetch_one",
                       new=AsyncMock(side_effect=fake)), \
         patch.object(api_validator, "_resolve_path", side_effect=fake_resolve):
        result = await api_validator.api_validator_probe()

    assert "dashboard" in result.metadata["failures"]
    assert "missing" in result.metadata["failures"]["dashboard"]
    assert result.status == "degraded"  # 4/5 still worked
