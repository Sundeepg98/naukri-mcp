"""A reminder must be able to END - the notify-once stamp and the discharge path.

Measured on the operator's live DB, 2026-08-30:

    reminders                50 rows, ALL created 2026-03-03, ALL due
                             2026-03-17 (166 days overdue). The `is_due`
                             column is dead: 0 in all 50, written by nothing.
    event_log ReminderDue    7,571 events over 10 days - the single largest
                             event type in the database, ahead of
                             ScheduledTaskCompleted (2,230).

All 7,571 come from those same 50 rows. The hourly `reminder_check` task
re-emits every past-due reminder on every tick, forever, at ~757/day.

tests/test_reminder_storm.py already stopped the READ paths from emitting.
This file covers the half that was left: the ONE sanctioned emitter had no
memory and the reminder had no end.

  1. NO MEMORY. `list_reminders(emit_events=...)` emitted for every past-due
     row on every opted-in call. A `notified_at` stamp turns that into one
     emit per due-transition, and `set_reminder` clears the stamp so a snooze
     genuinely re-arms the notification.
  2. NO END. `naukri_list_reminders` and `naukri_set_reminder` were the only
     two reminder tools. `database.delete_reminder` existed since the SQLite
     port and had no caller outside `ReminderRepository.delete`, which is
     itself uncalled. A reminder could be created and re-dated but never
     discharged. `dismiss_reminder` is that discharge.

Every test here was RED against the tree before the change: cases 1-3 because
`notified_at` did not exist, cases 4-5 because `dismiss_reminder` did not
exist, case 6 because the migration did not exist.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server.events import ReminderDue


# =====================================================================
# Helpers
# =====================================================================

def _iso(offset_days: float = 0) -> str:
    """ISO-8601 UTC timestamp offset_days from now (negative = past)."""
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat()


def _emitted_for(mock_emit, job_id):
    """ReminderDue events for ONE job_id passed to a patched event_bus.emit.

    Scoped to the job on purpose: the session-scoped test DB is shared, so an
    absolute count would couple this file to whatever else is in the table.
    """
    return [
        c.args[0] for c in mock_emit.call_args_list
        if isinstance(c.args[0], ReminderDue) and c.args[0].job_id == job_id
    ]


async def _seed_due(job_id, days_overdue=166):
    """Insert one months-overdue reminder, matching his live row shape."""
    from naukri_server.database import upsert_reminder, delete_reminder

    await delete_reminder(job_id)
    await upsert_reminder({
        "job_id": job_id,
        "title": "Senior Backend Developer",
        "company": "Acme",
        "remind_at": _iso(-days_overdue),
        "note": "Follow up on application",
        "created_at": _iso(-days_overdue - 14),
    })


async def _notified_at(job_id):
    from naukri_server.database import get_reminder
    row = await get_reminder(job_id)
    return row.get("notified_at") if row else None


@pytest.fixture
async def due_job():
    """One overdue reminder in the isolated session DB, removed afterwards."""
    from naukri_server.database import delete_reminder

    job_id = "LIFECYCLE-DUE-1"
    await _seed_due(job_id)
    try:
        yield job_id
    finally:
        await delete_reminder(job_id)


# =====================================================================
# 1-2. Emit AT MOST ONCE per due-transition
# =====================================================================

class TestNotifyOnce:

    @pytest.mark.asyncio
    async def test_unnotified_due_reminder_emits_exactly_once(self, due_job):
        """The baseline: a due reminder nobody has been told about yet."""
        from naukri_server.services.reminder_service import list_reminders

        assert await _notified_at(due_job) is None, "fixture must start unnotified"

        with patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await list_reminders(include_past=True, emit_events=True)

        assert len(_emitted_for(mock_emit, due_job)) == 1
        assert await _notified_at(due_job) is not None, (
            "a successful emit must stamp notified_at, or the next call "
            "re-emits and the storm is back"
        )
        row = [r for r in result["reminders"] if r["job_id"] == due_job][0]
        assert row["is_due"] is True
        assert "notified_at" in row, "the stamp must be visible to the caller"

    @pytest.mark.asyncio
    async def test_the_7571_event_storm_ten_calls_emit_exactly_one(self, due_job):
        """The live defect in miniature.

        7,571 ReminderDue events in event_log came from 50 rows re-emitted on
        every hourly tick. Ten opted-in calls over ONE due reminder must
        produce ONE event, not ten.
        """
        from naukri_server.services.reminder_service import list_reminders

        with patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            for _ in range(10):
                await list_reminders(include_past=True, emit_events=True)

        events = _emitted_for(mock_emit, due_job)
        assert len(events) == 1, (
            "ten reads emitted %d ReminderDue for one reminder; at the hourly "
            "cadence that is how 50 rows became 7,571 events" % len(events)
        )

    @pytest.mark.asyncio
    async def test_set_reminder_rearms_the_notification(self, due_job):
        """A snooze must re-arm. Without this, notified_at is a one-way latch
        and a re-dated reminder never notifies again - a silent failure that
        is strictly worse than the storm it replaces."""
        from naukri_server.services.reminder_service import (
            list_reminders, set_reminder,
        )

        with patch("naukri_server.events.event_bus.emit", new_callable=AsyncMock):
            await list_reminders(include_past=True, emit_events=True)
        assert await _notified_at(due_job) is not None

        result = await set_reminder(job_id=due_job, days=1)
        assert result["status"] == "success"
        assert await _notified_at(due_job) is None, (
            "set_reminder must clear notified_at"
        )

        # Re-date into the past so it is due again, keeping the cleared stamp.
        from naukri_server.database import get_reminder, upsert_reminder
        row = await get_reminder(due_job)
        row["remind_at"] = _iso(-1)
        await upsert_reminder(row)
        assert await _notified_at(due_job) is None

        with patch("naukri_server.events.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            await list_reminders(include_past=True, emit_events=True)

        assert len(_emitted_for(mock_emit, due_job)) == 1


# =====================================================================
# 3. The discharge path
# =====================================================================

class TestDismissReminder:

    @pytest.mark.asyncio
    async def test_dismiss_removes_the_reminder(self):
        """The thing that did not exist: a reminder that can END."""
        from naukri_server.services.reminder_service import (
            dismiss_reminder, list_reminders,
        )

        job_id = "LIFECYCLE-DISMISS-1"
        await _seed_due(job_id)
        before = await list_reminders(include_past=True)
        assert job_id in [r["job_id"] for r in before["reminders"]]

        result = await dismiss_reminder(job_id)

        assert result["status"] == "success"
        assert result["job_id"] == job_id
        assert result["message"]

        after = await list_reminders(include_past=True)
        assert job_id not in [r["job_id"] for r in after["reminders"]]
        assert after["total"] == before["total"] - 1

    @pytest.mark.asyncio
    async def test_dismiss_unknown_job_id_errors_and_removes_nothing(self):
        """A miss must be a reported error, not an exception and not a
        silent success that hides a typo'd job_id."""
        from naukri_server.services.reminder_service import (
            dismiss_reminder, list_reminders,
        )

        survivor = "LIFECYCLE-SURVIVOR-1"
        await _seed_due(survivor)
        try:
            before = await list_reminders(include_past=True)

            result = await dismiss_reminder("NO-SUCH-REMINDER-9999")

            assert result["status"] == "error"
            assert result["error_code"] == "NOT_FOUND"
            assert result["message"]

            after = await list_reminders(include_past=True)
            assert after["total"] == before["total"]
            assert survivor in [r["job_id"] for r in after["reminders"]]
        finally:
            from naukri_server.database import delete_reminder
            await delete_reminder(survivor)


# =====================================================================
# 4. The migration
# =====================================================================

# The reminders table exactly as it stood before notified_at. Written out
# rather than derived, so this test keeps testing the real upgrade path even
# after _SCHEMA_SQL grows the column.
_PRE_MIGRATION_REMINDERS_SQL = """
CREATE TABLE IF NOT EXISTS reminders (
    job_id TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    remind_at TEXT,
    note TEXT,
    created_at TEXT,
    is_due INTEGER DEFAULT 0
);
"""


async def _reminder_columns(db):
    cursor = await db.execute("PRAGMA table_info(reminders)")
    return [row["name"] for row in await cursor.fetchall()]


class TestNotifiedAtMigration:

    @pytest.mark.asyncio
    async def test_migration_is_idempotent(self, tmp_path):
        """Twice run, one ledger row, one column.

        Runs against a hand-built PRE-migration database - the shape of his
        live naukri.db - because a fresh database gets the column from
        _SCHEMA_SQL and would never exercise the ALTER at all.
        """
        from naukri_server import database

        db_path = tmp_path / "naukri.db"
        with patch("naukri_server.database.DB_PATH", db_path):
            db = await database.get_db()
            try:
                await db.executescript(_PRE_MIGRATION_REMINDERS_SQL)
                await db.executescript(database._MIGRATIONS_TABLE_SQL)
                await db.commit()
                assert "notified_at" not in await _reminder_columns(db)
            finally:
                await db.close()

            first = await database.migrate_reminders_notified_at()
            second = await database.migrate_reminders_notified_at()

            assert first["status"] == "applied"
            assert first["column_added"] is True
            assert second["column_added"] is False, (
                "the second run must not re-ALTER: sqlite raises "
                "'duplicate column name' and init_db would fail on every "
                "subsequent server start"
            )

            db = await database.get_db()
            try:
                cols = await _reminder_columns(db)
                cursor = await db.execute(
                    "SELECT name FROM migrations WHERE name = ?",
                    (database.REMINDERS_NOTIFIED_AT_MIGRATION,),
                )
                ledger = await cursor.fetchall()
            finally:
                await db.close()

        assert cols.count("notified_at") == 1, cols
        assert len(ledger) == 1, "one ledger row, not %d" % len(ledger)
