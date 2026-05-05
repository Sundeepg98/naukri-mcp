"""Tests for the 3 new domain events introduced by the Pragmatic Auto-Fix
healing system: EndpointDriftDetected, AutoFixApplied, AutoFixReverted.
"""

import pytest

from naukri_server.events import (
    EventBus,
    EndpointDriftDetected,
    AutoFixApplied,
    AutoFixReverted,
)


def test_endpoint_drift_detected_default_values():
    """EndpointDriftDetected has reasonable defaults — important for partial
    construction by detector code that may not have every field on hand."""
    e = EndpointDriftDetected()
    assert e.constant_name == ""
    assert e.url == ""
    assert e.severity == "added"
    assert e.drift_type == "field"
    assert e.changed_fields == {}
    assert e.snapshot_age_days == 0.0


def test_endpoint_drift_detected_with_payload():
    e = EndpointDriftDetected(
        constant_name="SEARCH_API",
        url="/jobapi/v3/search",
        severity="removed",
        drift_type="field",
        changed_fields={"jobDetails.0.minSal": "removed"},
        snapshot_age_days=12.5,
    )
    assert e.constant_name == "SEARCH_API"
    assert e.severity == "removed"
    assert e.changed_fields == {"jobDetails.0.minSal": "removed"}


def test_auto_fix_applied_payload():
    e = AutoFixApplied(
        commit_sha="abc1234",
        constant_name="SEARCH_API",
        tier="T1",
        drift_type="field",
        file_path="naukri_server/services/search_service.py",
    )
    assert e.tier == "T1"
    assert e.file_path.endswith("search_service.py")


def test_auto_fix_reverted_payload():
    e = AutoFixReverted(
        original_commit_sha="abc",
        revert_commit_sha="def",
        constant_name="SEARCH_API",
        reason="post-fix validation still failed",
    )
    assert e.original_commit_sha == "abc"
    assert e.revert_commit_sha == "def"
    assert "validation" in e.reason


@pytest.mark.asyncio
async def test_emit_and_subscribe_drift_event():
    """Smoke test: events flow through EventBus end-to-end."""
    bus = EventBus()
    received = []

    async def handler(ev):
        received.append(ev)

    bus.subscribe(EndpointDriftDetected, handler)
    await bus.emit(EndpointDriftDetected(
        constant_name="X_API", url="/x", severity="added",
        drift_type="field", changed_fields={"newField": "added"}, snapshot_age_days=1.0,
    ))

    assert len(received) == 1
    assert received[0].constant_name == "X_API"


@pytest.mark.asyncio
async def test_three_event_types_independently_routed():
    """Subscribers registered for one event type don't receive others."""
    bus = EventBus()
    drifts: list = []
    applieds: list = []
    reverteds: list = []

    async def on_drift(ev):
        drifts.append(ev)

    async def on_applied(ev):
        applieds.append(ev)

    async def on_reverted(ev):
        reverteds.append(ev)

    bus.subscribe(EndpointDriftDetected, on_drift)
    bus.subscribe(AutoFixApplied, on_applied)
    bus.subscribe(AutoFixReverted, on_reverted)

    await bus.emit(EndpointDriftDetected(constant_name="A"))
    await bus.emit(AutoFixApplied(constant_name="B", tier="T1"))
    await bus.emit(AutoFixReverted(constant_name="C"))
    await bus.emit(EndpointDriftDetected(constant_name="D"))

    assert [e.constant_name for e in drifts] == ["A", "D"]
    assert [e.constant_name for e in applieds] == ["B"]
    assert [e.constant_name for e in reverteds] == ["C"]
