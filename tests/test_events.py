"""Tests for the event-driven pub/sub system."""

import asyncio
import logging
import pytest

from naukri_server.events import (
    EventBus,
    DomainEvent,
    ApplicationSubmitted,
    ApplicationStatusChanged,
    RecruiterEngaged,
    ApplicationStale,
)


@pytest.fixture
def bus():
    return EventBus()


# 1. Subscribe + emit calls handler
@pytest.mark.asyncio
async def test_subscribe_and_emit_calls_handler(bus):
    received = []

    async def handler(event):
        received.append(event)

    bus.subscribe(ApplicationSubmitted, handler)
    event = ApplicationSubmitted(job_id="j1", company="Acme", title="SDE", fit_score=85)
    await bus.emit(event)

    assert len(received) == 1
    assert received[0] is event
    assert received[0].company == "Acme"


# 2. Multiple subscribers all called
@pytest.mark.asyncio
async def test_multiple_subscribers_all_called(bus):
    calls_a = []
    calls_b = []

    async def handler_a(event):
        calls_a.append(event)

    async def handler_b(event):
        calls_b.append(event)

    bus.subscribe(RecruiterEngaged, handler_a)
    bus.subscribe(RecruiterEngaged, handler_b)

    event = RecruiterEngaged(recruiter_name="Alice", company="BigCo", action="VIEWED")
    await bus.emit(event)

    assert len(calls_a) == 1
    assert len(calls_b) == 1


# 3. Handler exception doesn't crash bus (logged, not raised)
@pytest.mark.asyncio
async def test_handler_exception_does_not_crash_bus(bus, caplog):
    ok_calls = []

    async def bad_handler(event):
        raise ValueError("boom")

    async def good_handler(event):
        ok_calls.append(event)

    bus.subscribe(ApplicationSubmitted, bad_handler)
    bus.subscribe(ApplicationSubmitted, good_handler)

    with caplog.at_level(logging.WARNING):
        await bus.emit(ApplicationSubmitted(job_id="j2", company="X"))

    # Good handler still ran despite bad handler exploding
    assert len(ok_calls) == 1
    assert "boom" in caplog.text


# 4. Unsubscribe removes handler
@pytest.mark.asyncio
async def test_unsubscribe_removes_handler(bus):
    calls = []

    async def handler(event):
        calls.append(event)

    bus.subscribe(ApplicationStale, handler)
    await bus.emit(ApplicationStale(job_id="j3"))
    assert len(calls) == 1

    bus.unsubscribe(ApplicationStale, handler)
    await bus.emit(ApplicationStale(job_id="j4"))
    assert len(calls) == 1  # no new call


# 5. Event log stores recent events
@pytest.mark.asyncio
async def test_event_log_stores_recent_events(bus):
    await bus.emit(ApplicationSubmitted(job_id="a"))
    await bus.emit(ApplicationSubmitted(job_id="b"))
    await bus.emit(RecruiterEngaged(recruiter_name="R1"))

    log = bus.get_log()
    assert len(log) == 3
    assert log[0].job_id == "a"
    assert log[2].recruiter_name == "R1"


# 6. Event log respects max size
@pytest.mark.asyncio
async def test_event_log_respects_max_size(bus):
    bus._max_log_size = 5
    for i in range(10):
        await bus.emit(ApplicationSubmitted(job_id=str(i)))

    log = bus.get_log()
    assert len(log) == 5
    # Should keep the last 5 (ids 5-9)
    assert [e.job_id for e in log] == ["5", "6", "7", "8", "9"]


# 7. get_log filters by event type
@pytest.mark.asyncio
async def test_get_log_filters_by_event_type(bus):
    await bus.emit(ApplicationSubmitted(job_id="s1"))
    await bus.emit(RecruiterEngaged(recruiter_name="R"))
    await bus.emit(ApplicationSubmitted(job_id="s2"))
    await bus.emit(ApplicationStale(job_id="st1"))

    submitted_only = bus.get_log(event_type=ApplicationSubmitted)
    assert len(submitted_only) == 2
    assert all(isinstance(e, ApplicationSubmitted) for e in submitted_only)

    recruiter_only = bus.get_log(event_type=RecruiterEngaged)
    assert len(recruiter_only) == 1
