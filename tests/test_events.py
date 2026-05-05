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


# =====================================================================
# CQRS gap-fill: verify the 5 newly-added emits fire from production paths
# =====================================================================

from unittest.mock import AsyncMock, patch
from naukri_server.events import (
    ReminderSet,
    ApplicationsPurged,
    InboxMessageRead,
    InboxInviteAccepted,
    AgentJobApplied,
)


@pytest.mark.asyncio
async def test_set_reminder_emits_reminder_set():
    """_set_reminder() must emit ReminderSet on successful create."""
    from naukri_server.tools.reminders import _set_reminder

    with patch("naukri_server.database.get_reminder", new_callable=AsyncMock, return_value=None), \
         patch("naukri_server.database.upsert_reminder", new_callable=AsyncMock), \
         patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock) as mock_emit:
        result = await _set_reminder(job_id="J100", days=7, company="Acme")

    assert result["status"] == "success"
    # Find the ReminderSet emit (others may also fire from default subscribers)
    reminder_set_calls = [
        c for c in mock_emit.call_args_list
        if c.args and isinstance(c.args[0], ReminderSet)
    ]
    assert len(reminder_set_calls) >= 1
    event = reminder_set_calls[0].args[0]
    assert event.job_id == "J100"
    assert event.days == 7
    assert event.company == "Acme"


@pytest.mark.asyncio
async def test_purge_applications_emits_applications_purged():
    """purge_applications() must emit ApplicationsPurged after a real (non-dry-run) purge."""
    from naukri_server.services.application_service import purge_applications

    sample = [{"job_id": "J1", "title": "T", "applied_at": "2024-01-01"}]
    with patch("naukri_server.database.list_applications",
               new_callable=AsyncMock, return_value=(sample, 3)), \
         patch("naukri_server.database.delete_applications_before",
               new_callable=AsyncMock, return_value=3), \
         patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock) as mock_emit:
        result = await purge_applications(before_date="2025-01-01", dry_run=False)

    assert result["status"] == "success"
    purged_calls = [
        c for c in mock_emit.call_args_list
        if c.args and isinstance(c.args[0], ApplicationsPurged)
    ]
    assert len(purged_calls) == 1
    event = purged_calls[0].args[0]
    assert event.purged_count == 3
    assert event.before_date == "2025-01-01"


@pytest.mark.asyncio
async def test_purge_applications_dry_run_does_not_emit():
    """Dry-run purge must NOT emit ApplicationsPurged (no actual delete)."""
    from naukri_server.services.application_service import purge_applications

    with patch("naukri_server.database.list_applications",
               new_callable=AsyncMock, return_value=([], 0)), \
         patch("naukri_server.database.delete_applications_before",
               new_callable=AsyncMock, return_value=0), \
         patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock) as mock_emit:
        await purge_applications(before_date="2025-01-01", dry_run=True)

    purged_calls = [
        c for c in mock_emit.call_args_list
        if c.args and isinstance(c.args[0], ApplicationsPurged)
    ]
    assert len(purged_calls) == 0


@pytest.mark.asyncio
async def test_read_message_emits_inbox_message_read():
    """_read_message() must emit InboxMessageRead with thread_id and message_id."""
    from naukri_server.tools.inbox import _read_message

    fake_resp = {
        "mail": {
            "subject": "Hi",
            "titleText": "Hello there",
            "dateTime": "2026-01-01",
            "messageType": "msg",
            "conversationId": "C42",
        }
    }
    with patch("naukri_server.tools.inbox.api_client.get",
               new_callable=AsyncMock, return_value=fake_resp), \
         patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock) as mock_emit:
        result = await _read_message(message_id="M1", vcard_id="V1", unique_id="U1")

    assert result["status"] == "success"
    read_calls = [
        c for c in mock_emit.call_args_list
        if c.args and isinstance(c.args[0], InboxMessageRead)
    ]
    assert len(read_calls) == 1
    event = read_calls[0].args[0]
    assert event.message_id == "M1"
    assert event.thread_id == "C42"


@pytest.mark.asyncio
async def test_mark_interested_emits_recruiter_engaged():
    """_mark_interested() must emit RecruiterEngaged with the right action."""
    from naukri_server.tools.inbox import _mark_interested

    with patch("naukri_server.tools.inbox.api_client.post",
               new_callable=AsyncMock, return_value={}), \
         patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock) as mock_emit:
        result = await _mark_interested(mail_id="MAIL1", conversation_id="C1", interested=True)

    assert result["status"] == "success"
    recruiter_calls = [
        c for c in mock_emit.call_args_list
        if c.args and isinstance(c.args[0], RecruiterEngaged)
    ]
    assert len(recruiter_calls) == 1
    event = recruiter_calls[0].args[0]
    assert event.action == "MARKED_INTERESTED"

    # Negative case
    mock_emit.reset_mock()
    with patch("naukri_server.tools.inbox.api_client.post",
               new_callable=AsyncMock, return_value={}), \
         patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock) as mock_emit2:
        await _mark_interested(mail_id="MAIL2", conversation_id="C2", interested=False)
    not_interested_calls = [
        c for c in mock_emit2.call_args_list
        if c.args and isinstance(c.args[0], RecruiterEngaged)
        and c.args[0].action == "MARKED_NOT_INTERESTED"
    ]
    assert len(not_interested_calls) == 1


@pytest.mark.asyncio
async def test_accept_nvite_emits_invite_accepted_on_success():
    """_accept_nvite() must emit InboxInviteAccepted only when apply succeeded."""
    from naukri_server.tools import inbox as inbox_mod

    # Success path
    with patch("naukri_server.tools.apply._apply_single",
               new_callable=AsyncMock, return_value={"status": "applied", "job_id": "NV1"}), \
         patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock) as mock_emit:
        result = await inbox_mod._accept_nvite("NV1", None, None, None)

    assert result["status"] == "applied"
    accepted_calls = [
        c for c in mock_emit.call_args_list
        if c.args and isinstance(c.args[0], InboxInviteAccepted)
    ]
    assert len(accepted_calls) == 1
    assert accepted_calls[0].args[0].invite_id == "NV1"

    # Failure path: no emit
    with patch("naukri_server.tools.apply._apply_single",
               new_callable=AsyncMock, return_value={"status": "needs_input", "job_id": "NV2"}), \
         patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock) as mock_emit2:
        await inbox_mod._accept_nvite("NV2", None, None, None)

    accepted_calls_2 = [
        c for c in mock_emit2.call_args_list
        if c.args and isinstance(c.args[0], InboxInviteAccepted)
    ]
    assert len(accepted_calls_2) == 0


@pytest.mark.asyncio
async def test_agent_approve_emits_agent_job_applied_per_success():
    """_agent_approve() must emit AgentJobApplied for each successfully-applied job."""
    from naukri_server.tools.agent_tool import _agent_approve

    pending_decisions = [
        {"job_id": "JA1", "decision": "apply", "apply_status": "pending",
         "title": "SDE1", "company": "Acme", "fit_score": 80},
        {"job_id": "JA2", "decision": "apply", "apply_status": "pending",
         "title": "SDE2", "company": "Beta", "fit_score": 75},
    ]
    fake_run = {"cycle_id": "CYC1"}

    with patch("naukri_server.database.get_agent_run",
               new_callable=AsyncMock, return_value=fake_run), \
         patch("naukri_server.database.list_agent_decisions",
               new_callable=AsyncMock, return_value=pending_decisions), \
         patch("naukri_server.database.update_agent_decision", new_callable=AsyncMock), \
         patch("naukri_server.tools.apply._apply_single",
               new_callable=AsyncMock, return_value={"status": "applied"}), \
         patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock) as mock_emit:
        result = await _agent_approve(cycle_id="CYC1", approve=True)

    assert result["status"] == "success"
    assert result["applied"] == 2
    agent_calls = [
        c for c in mock_emit.call_args_list
        if c.args and isinstance(c.args[0], AgentJobApplied)
    ]
    assert len(agent_calls) == 2
    assert {c.args[0].job_id for c in agent_calls} == {"JA1", "JA2"}
    assert {c.args[0].fit_score for c in agent_calls} == {80, 75}


@pytest.mark.asyncio
async def test_inbox_message_read_subscriber_registered():
    """The InboxMessageRead subscriber should be wired and respond to events."""
    import naukri_server.subscribers  # noqa: F401 — ensure decorators ran
    from naukri_server.framework.registry import registry

    inbox_read_subs = [
        it for it in registry.by_kind("subscriber")
        if it.key is InboxMessageRead
    ]
    assert len(inbox_read_subs) >= 1


@pytest.mark.asyncio
async def test_inbox_invite_accepted_subscriber_registered():
    """The InboxInviteAccepted subscriber should be wired and respond to events."""
    import naukri_server.subscribers  # noqa: F401
    from naukri_server.framework.registry import registry

    invite_subs = [
        it for it in registry.by_kind("subscriber")
        if it.key is InboxInviteAccepted
    ]
    assert len(invite_subs) >= 1
