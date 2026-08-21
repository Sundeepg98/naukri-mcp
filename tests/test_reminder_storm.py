"""The ReminderDue notification storm - both halves, each with its control.

Measured on the operator's live DB, 2026-08-21: 1,084 ReminderDue notifications
for 50 distinct job_ids (~21 duplicates each), 1,075 of them still undelivered,
out of 1,241 undelivered notifications total. `event_stats_24h` showed 1,127
ReminderDue events in a single day. His daily brief ranks pending high-priority
notifications first, so his single top recommended action was 85% noise.

Two independent defects produced that, and either one alone still storms:

  1. `reminder_service.list_reminders` emitted a ReminderDue for every past-due
     reminder. That is a READ - `naukri_list_reminders`, `naukri_daily_brief`,
     `naukri_follow_up_priority` all call it - carrying an unbounded write side
     effect. Every one of his reminders is months past due, so each read minted
     ~50 more high-priority rows. The brief was self-poisoning: it delivers 10
     notifications per call and minted ~50 in the same call.

  2. The `ReminderDue` subscriber stored a notification unconditionally, so even
     the legitimate hourly `reminder_check` task banked 50 duplicates a day
     forever.

Every guard below ships with a CONTROL proving it can fail - a suppression fix
is trivially satisfiable by a check that suppresses everything, and this
codebase has already produced six checks that could not fail.
"""

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from naukri_server.events import ReminderDue


def _past_due(job_id="STORM-1", days=120):
    remind_at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {
        "job_id": job_id,
        "title": None,
        "company": None,
        "remind_at": remind_at,
        "note": "Follow up on application",
        "created_at": remind_at,
    }


def _not_yet_due(job_id="STORM-FUTURE", days=30):
    remind_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    return {
        "job_id": job_id,
        "title": "SDE",
        "company": "Acme",
        "remind_at": remind_at,
        "note": "later",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _emitted_reminder_due(mock_emit):
    """The ReminderDue events passed to a patched event_bus.emit."""
    return [c.args[0] for c in mock_emit.call_args_list
            if isinstance(c.args[0], ReminderDue)]


# =====================================================================
# 1. The read path must not emit
# =====================================================================

class TestReadPathIsPure:

    @pytest.mark.asyncio
    async def test_read_does_not_emit_reminder_due(self):
        """RED before the fix: listing reminders minted an event per due row."""
        from naukri_server.tools.reminders import _list_reminders

        with patch("naukri_server.database.list_reminders",
                   new_callable=AsyncMock, return_value=[_past_due()]), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await _list_reminders(include_past=True)

        assert result["due_count"] == 1, "the reminder must still be REPORTED due"
        assert _emitted_reminder_due(mock_emit) == [], (
            "a read must not mutate: %d ReminderDue event(s) emitted"
            % len(_emitted_reminder_due(mock_emit))
        )

    @pytest.mark.asyncio
    async def test_fifty_past_due_reminders_emit_nothing_on_read(self):
        """His actual shape: 50 months-overdue reminders, one brief call."""
        from naukri_server.tools.reminders import _list_reminders

        fifty = [_past_due("STORM-%d" % i) for i in range(50)]
        with patch("naukri_server.database.list_reminders",
                   new_callable=AsyncMock, return_value=fifty), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await _list_reminders(include_past=True)

        assert result["due_count"] == 50
        assert _emitted_reminder_due(mock_emit) == []

    @pytest.mark.asyncio
    async def test_CONTROL_opting_in_still_emits(self):
        """CONTROL. Without this, deleting the emit entirely would pass the
        suite while silently killing the only reminder notification he gets."""
        from naukri_server.tools.reminders import _list_reminders

        with patch("naukri_server.database.list_reminders",
                   new_callable=AsyncMock, return_value=[_past_due()]), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            await _list_reminders(include_past=True, emit_events=True)

        events = _emitted_reminder_due(mock_emit)
        assert len(events) == 1
        assert events[0].job_id == "STORM-1"

    @pytest.mark.asyncio
    async def test_CONTROL_not_yet_due_never_emits_even_when_opted_in(self):
        """CONTROL on the control: emit_events must not mean "emit everything"."""
        from naukri_server.tools.reminders import _list_reminders

        with patch("naukri_server.database.list_reminders",
                   new_callable=AsyncMock, return_value=[_not_yet_due()]), \
             patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await _list_reminders(include_past=True, emit_events=True)

        assert result["due_count"] == 0
        assert _emitted_reminder_due(mock_emit) == []

    @pytest.mark.asyncio
    async def test_scheduled_task_opts_in(self):
        """The hourly reminder_check is the one path that SHOULD notify him."""
        from naukri_server.scheduler_tasks import _task_reminder_check

        with patch("naukri_server.tools.reminders._list_reminders",
                   new_callable=AsyncMock,
                   return_value={"due_count": 2, "reminders": []}) as mock_rem:
            await _task_reminder_check()

        mock_rem.assert_awaited_once_with(include_past=True, emit_events=True)

    def test_only_the_scheduler_opts_into_emission(self):
        """Source guard: no read path may re-arm emission.

        Modelled on test_scheduler_cadence.test_only_non_mutating_tasks_opt_into
        _catch_up. A behavioural test cannot see a NEW caller added next month;
        this can. The daily brief, follow-up priority and the MCP list tool all
        call _list_reminders, and any of them passing emit_events=True would
        restore the storm exactly.
        """
        pkg = Path(__file__).resolve().parent.parent / "naukri_server"
        opted_in = sorted(
            p.relative_to(pkg).as_posix()
            for p in pkg.rglob("*.py")
            if "emit_events=True" in p.read_text(encoding="utf-8")
        )
        assert opted_in == ["scheduler_tasks.py"], (
            "only the scheduled reminder_check may emit ReminderDue; found %s"
            % opted_in
        )


# =====================================================================
# 2. The subscriber must dedupe
# =====================================================================

class TestSubscriberDedupe:

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_suppressed_when_one_is_already_pending(self, mock_has, mock_store):
        """RED before the fix: the subscriber stored unconditionally."""
        from naukri_server.subscribers import _on_reminder_due
        mock_has.return_value = True

        await _on_reminder_due(ReminderDue(job_id="J1", company="Acme", note="Follow up"))

        mock_store.assert_not_called()
        mock_has.assert_awaited_once_with("ReminderDue", "J1")

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_CONTROL_stores_when_nothing_is_pending(self, mock_has, mock_store):
        """CONTROL. "Suppress everything" is the cheapest way to pass the test
        above and would mean he never hears about a reminder again."""
        from naukri_server.subscribers import _on_reminder_due
        mock_has.return_value = False

        await _on_reminder_due(ReminderDue(job_id="J1", company="Acme", note="Follow up"))

        mock_store.assert_called_once()
        assert mock_store.call_args[0][0]["event_type"] == "ReminderDue"
        assert mock_store.call_args[0][0]["priority"] == "high"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_dedupe_is_scoped_to_the_job(self, mock_has, mock_store):
        """A pending notification for J1 must not silence J2."""
        from naukri_server.subscribers import _on_reminder_due
        pending = {"J1"}
        mock_has.side_effect = lambda event_type, job_id: job_id in pending

        await _on_reminder_due(ReminderDue(job_id="J1", company="Acme"))
        await _on_reminder_due(ReminderDue(job_id="J2", company="BigCo"))

        assert mock_store.call_count == 1
        assert mock_store.call_args[0][0]["metadata"]["job_id"] == "J2"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_dedupe_failure_fails_open(self, mock_has, mock_store):
        """If the dedupe query breaks, still notify.

        The notification is the payload; the dedupe is an optimisation over it.
        A broken dedupe degrades to the old (noisy) behaviour, which is
        recoverable; silently dropping his reminders is not.
        """
        from naukri_server.subscribers import _on_reminder_due
        mock_has.side_effect = RuntimeError("no such column")

        await _on_reminder_due(ReminderDue(job_id="J1", company="Acme"))

        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_no_job_id_is_not_deduped_away(self, mock_has, mock_store):
        """An event with no job_id cannot be matched against anything, so it
        must not be silenced by a dedupe lookup that returns True for ''."""
        from naukri_server.subscribers import _on_reminder_due
        mock_has.return_value = True

        await _on_reminder_due(ReminderDue(job_id="", company="Acme", note="orphan"))

        mock_store.assert_called_once()


# =====================================================================
# 3. The dedupe predicate itself, against a real SQLite schema
# =====================================================================
# conftest._isolated_test_db points DB_PATH at a session tmp file with the real
# schema. These run the actual json_extract query - the part most likely to be
# quietly wrong, since `metadata` is a JSON TEXT column, not a job_id column.

class TestHasPendingNotification:

    @pytest.mark.asyncio
    async def test_true_for_an_undelivered_row(self):
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "ReminderDue", "title": "t", "body": "b",
            "priority": "high", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": "HPN-A"},
        })
        assert await has_pending_notification("ReminderDue", "HPN-A") is True

    @pytest.mark.asyncio
    async def test_false_once_delivered(self):
        """Delivery is what re-arms the reminder: he has now been shown it."""
        from naukri_server.database import (
            store_notification, has_pending_notification, mark_notifications_delivered,
        )
        row_id = await store_notification({
            "event_type": "ReminderDue", "title": "t", "body": "b",
            "priority": "high", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": "HPN-B"},
        })
        assert await has_pending_notification("ReminderDue", "HPN-B") is True
        await mark_notifications_delivered([row_id])
        assert await has_pending_notification("ReminderDue", "HPN-B") is False

    @pytest.mark.asyncio
    async def test_false_for_an_unrelated_job(self):
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "ReminderDue", "title": "t", "body": "b",
            "priority": "high", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": "HPN-C"},
        })
        assert await has_pending_notification("ReminderDue", "HPN-NOPE") is False

    @pytest.mark.asyncio
    async def test_false_across_event_types(self):
        """A pending ApplicationStale for a job must not silence its ReminderDue."""
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "ApplicationStale", "title": "t", "body": "b",
            "priority": "high", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": "HPN-D"},
        })
        assert await has_pending_notification("ReminderDue", "HPN-D") is False
        assert await has_pending_notification("ApplicationStale", "HPN-D") is True

    @pytest.mark.asyncio
    async def test_job_id_is_matched_exactly_not_by_substring(self):
        """The obvious cheap implementation is `metadata LIKE '%<job_id>%'`.
        Naukri job_ids are numeric strings and routinely share prefixes, so a
        LIKE would suppress a real reminder for a different job."""
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "ReminderDue", "title": "t", "body": "b",
            "priority": "high", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": "020426009351"},
        })
        assert await has_pending_notification("ReminderDue", "020426009351") is True
        assert await has_pending_notification("ReminderDue", "0204260093") is False

    @pytest.mark.asyncio
    async def test_false_when_metadata_is_null(self):
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "ReminderDue", "title": "t", "body": "b",
            "priority": "high", "created_at": datetime.now(timezone.utc).isoformat(),
        })
        assert await has_pending_notification("ReminderDue", "HPN-E") is False
