"""Deep tests for naukri_server/tools/reminders.py.

Every test is PURE — no network, no browser, no file I/O.
We patch database functions (get_reminder, upsert_reminder, list_reminders)
at naukri_server.database since production code uses lazy imports.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =====================================================================
# Helpers
# =====================================================================

def _utc(offset_days: float = 0) -> datetime:
    """Return a UTC datetime offset_days from now (negative = past)."""
    return datetime.now(timezone.utc) + timedelta(days=offset_days)


def _iso(offset_days: float = 0) -> str:
    return _utc(offset_days).isoformat()


# =====================================================================
# 1. Atomic tool routing — naukri_list_reminders / naukri_set_reminder
# =====================================================================

class TestAtomicRouting:
    """Tests for the atomic naukri_list_reminders / naukri_set_reminder tools."""

    @pytest.mark.asyncio
    async def test_list_calls_list_reminders(self):
        """naukri_list_reminders should delegate to _list_reminders with correct kwargs."""
        from naukri_server.tools.reminders import naukri_list_reminders

        list_result = {"status": "success", "total": 0, "due_count": 0, "reminders": []}

        with patch(
            "naukri_server.tools.reminders._list_reminders",
            new_callable=AsyncMock,
            return_value=list_result,
        ) as mock_list:
            result = await naukri_list_reminders(include_past=False, include_app_status=False)

        mock_list.assert_awaited_once_with(include_past=False, include_app_status=False)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_set_calls_set_reminder(self):
        """naukri_set_reminder with a valid job_id should delegate to _set_reminder."""
        from naukri_server.tools.reminders import naukri_set_reminder

        set_result = {
            "status": "success",
            "job_id": "J001",
            "remind_at": _iso(7),
            "note": "Call recruiter",
            "message": "Reminder set — due in 7 days.",
        }

        with patch(
            "naukri_server.tools.reminders._set_reminder",
            new_callable=AsyncMock,
            return_value=set_result,
        ) as mock_set:
            result = await naukri_set_reminder(
                job_id="J001",
                days=7,
                note="Call recruiter",
                title="SDE",
                company="Acme",
            )

        mock_set.assert_awaited_once_with(
            job_id="J001", days=7, note="Call recruiter", title="SDE", company="Acme"
        )
        assert result["status"] == "success"
        assert result["job_id"] == "J001"


# =====================================================================
# 2. Validation — _set_reminder days range
# =====================================================================

class TestSetValidation:
    """Validation tests for _set_reminder days range."""

    @pytest.mark.asyncio
    async def test_days_less_than_1_returns_validation_error(self):
        """days=0 must return VALIDATION_ERROR from _set_reminder."""
        from naukri_server.tools.reminders import _set_reminder

        with patch("naukri_server.database.get_reminder", new_callable=AsyncMock, return_value=None) as mock_get, \
             patch("naukri_server.database.upsert_reminder", new_callable=AsyncMock) as mock_upsert:
            result = await _set_reminder(job_id="J002", days=0)

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "365" in result["message"]
        mock_upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_days_greater_than_365_returns_validation_error(self):
        """days=366 must return VALIDATION_ERROR from _set_reminder."""
        from naukri_server.tools.reminders import _set_reminder

        with patch("naukri_server.database.get_reminder", new_callable=AsyncMock, return_value=None) as mock_get, \
             patch("naukri_server.database.upsert_reminder", new_callable=AsyncMock) as mock_upsert:
            result = await _set_reminder(job_id="J003", days=366)

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        mock_upsert.assert_not_awaited()


# =====================================================================
# 3. _set_reminder — create and update paths
# =====================================================================

class TestSetReminder:
    """Tests for the _set_reminder internal helper."""

    @pytest.mark.asyncio
    async def test_creates_new_reminder(self):
        """A job_id not already in reminders should be inserted as a new entry."""
        from naukri_server.tools.reminders import _set_reminder

        captured = {}

        async def fake_upsert(data):
            captured["data"] = data

        with patch("naukri_server.database.get_reminder", new_callable=AsyncMock, return_value=None), \
             patch("naukri_server.database.upsert_reminder", new_callable=AsyncMock, side_effect=fake_upsert):
            result = await _set_reminder(
                job_id="J010",
                days=14,
                note="Follow up",
                title="Backend Dev",
                company="StartupCo",
            )

        assert result["status"] == "success"
        assert result["job_id"] == "J010"
        assert "14 days" in result["message"]
        assert "set" in result["message"]

        entry = captured["data"]
        assert entry["job_id"] == "J010"
        assert entry["title"] == "Backend Dev"
        assert entry["company"] == "StartupCo"
        assert entry["note"] == "Follow up"
        assert "created_at" in entry
        assert "remind_at" in entry

    @pytest.mark.asyncio
    async def test_updates_existing_reminder_by_job_id(self):
        """An existing job_id must be updated in-place — not duplicated."""
        from naukri_server.tools.reminders import _set_reminder

        existing_remind_at = _iso(-1)  # already overdue
        existing_reminder = {
            "job_id": "J020",
            "title": "Old Title",
            "company": "OldCo",
            "remind_at": existing_remind_at,
            "note": "Old note",
            "created_at": _iso(-10),
        }
        captured = {}

        async def fake_upsert(data):
            captured["data"] = data

        with patch("naukri_server.database.get_reminder", new_callable=AsyncMock, return_value=dict(existing_reminder)), \
             patch("naukri_server.database.upsert_reminder", new_callable=AsyncMock, side_effect=fake_upsert):
            result = await _set_reminder(
                job_id="J020",
                days=5,
                note="New note",
                title="New Title",
                company="NewCo",
            )

        assert result["status"] == "success"
        assert "updated" in result["message"]

        entry = captured["data"]
        assert entry["job_id"] == "J020"
        assert entry["title"] == "New Title"
        assert entry["company"] == "NewCo"
        assert entry["note"] == "New note"
        assert entry["remind_at"] != existing_remind_at
        assert "updated_at" in entry

    @pytest.mark.asyncio
    async def test_update_preserves_existing_note_when_none_passed(self):
        """When note=None is passed on update, the existing note must be preserved."""
        from naukri_server.tools.reminders import _set_reminder

        existing_reminder = {
            "job_id": "J021",
            "title": "Dev",
            "company": "Corp",
            "remind_at": _iso(-2),
            "note": "Original note",
            "created_at": _iso(-5),
        }
        captured = {}

        async def fake_upsert(data):
            captured["data"] = data

        with patch("naukri_server.database.get_reminder", new_callable=AsyncMock, return_value=dict(existing_reminder)), \
             patch("naukri_server.database.upsert_reminder", new_callable=AsyncMock, side_effect=fake_upsert):
            result = await _set_reminder(job_id="J021", days=3, note=None)

        # note=None means "keep existing note"
        entry = captured["data"]
        assert entry["note"] == "Original note"


# =====================================================================
# 4. _list_reminders — is_due, days_until_due, filtering, sorting
# =====================================================================

class TestListReminders:
    """Tests for the _list_reminders internal helper."""

    @pytest.mark.asyncio
    async def test_is_due_flag_set_for_past_reminder(self):
        """A reminder whose remind_at is in the past must have is_due=True."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {
                "job_id": "J100",
                "remind_at": _iso(-3),
                "title": "Dev",
                "company": "A",
                "note": None,
                "created_at": _iso(-10),
            }
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders):
            result = await _list_reminders(include_past=True, include_app_status=False)

        assert result["status"] == "success"
        assert result["total"] == 1
        r = result["reminders"][0]
        assert r["is_due"] is True
        assert r["days_until_due"] <= 0

    @pytest.mark.asyncio
    async def test_is_due_false_for_future_reminder(self):
        """A reminder whose remind_at is in the future must have is_due=False."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {
                "job_id": "J101",
                "remind_at": _iso(10),
                "title": "QA",
                "company": "B",
                "note": None,
                "created_at": _iso(-1),
            }
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders):
            result = await _list_reminders(include_past=True, include_app_status=False)

        r = result["reminders"][0]
        assert r["is_due"] is False
        assert r["days_until_due"] >= 0

    @pytest.mark.asyncio
    async def test_include_past_false_excludes_due_reminders(self):
        """include_past=False must exclude reminders that are already due."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {"job_id": "J200", "remind_at": _iso(-1), "title": "Past", "company": "X",
             "note": None, "created_at": _iso(-5)},
            {"job_id": "J201", "remind_at": _iso(5), "title": "Future", "company": "Y",
             "note": None, "created_at": _iso(-1)},
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders):
            result = await _list_reminders(include_past=False, include_app_status=False)

        job_ids = [r["job_id"] for r in result["reminders"]]
        assert "J200" not in job_ids
        assert "J201" in job_ids
        assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_sort_due_first(self):
        """Due reminders must come before upcoming ones in the returned list."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {"job_id": "J300", "remind_at": _iso(10), "title": "Future", "company": "A",
             "note": None, "created_at": _iso(-1)},
            {"job_id": "J301", "remind_at": _iso(-2), "title": "Due", "company": "B",
             "note": None, "created_at": _iso(-5)},
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders):
            result = await _list_reminders(include_past=True, include_app_status=False)

        assert result["reminders"][0]["job_id"] == "J301"  # due first
        assert result["reminders"][1]["job_id"] == "J300"  # upcoming second

    @pytest.mark.asyncio
    async def test_due_count_calculation(self):
        """due_count must reflect the exact number of reminders with is_due=True."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {"job_id": "J400", "remind_at": _iso(-5), "title": "A", "company": "C1",
             "note": None, "created_at": _iso(-10)},
            {"job_id": "J401", "remind_at": _iso(-1), "title": "B", "company": "C2",
             "note": None, "created_at": _iso(-3)},
            {"job_id": "J402", "remind_at": _iso(7), "title": "C", "company": "C3",
             "note": None, "created_at": _iso(-1)},
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders):
            result = await _list_reminders(include_past=True, include_app_status=False)

        assert result["due_count"] == 2
        assert result["total"] == 3

    @pytest.mark.asyncio
    async def test_malformed_datetime_skipped_gracefully(self):
        """A reminder with an unparseable remind_at must be silently skipped."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {"job_id": "J500", "remind_at": "not-a-date", "title": "Bad", "company": "X",
             "note": None, "created_at": _iso(-2)},
            {"job_id": "J501", "remind_at": _iso(3), "title": "Good", "company": "Y",
             "note": None, "created_at": _iso(-1)},
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders):
            result = await _list_reminders(include_past=True, include_app_status=False)

        job_ids = [r["job_id"] for r in result["reminders"]]
        assert "J500" not in job_ids  # malformed skipped
        assert "J501" in job_ids
        assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_missing_remind_at_skipped_gracefully(self):
        """A reminder dict with no remind_at key must also be silently skipped."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {"job_id": "J502", "title": "No date", "company": "Z",
             "note": None, "created_at": _iso(-1)},
            {"job_id": "J503", "remind_at": _iso(2), "title": "OK", "company": "W",
             "note": None, "created_at": _iso(-1)},
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders):
            result = await _list_reminders(include_past=True, include_app_status=False)

        job_ids = [r["job_id"] for r in result["reminders"]]
        assert "J502" not in job_ids
        assert "J503" in job_ids


# =====================================================================
# 5. _list_reminders — include_app_status enrichment
# =====================================================================

class TestListRemindersAppStatus:
    """Tests for the include_app_status enrichment path in _list_reminders."""

    @pytest.mark.asyncio
    async def test_enriches_with_app_status_when_requested(self):
        """With include_app_status=True, each reminder should get application_status."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {"job_id": "J600", "remind_at": _iso(5), "title": "Dev", "company": "Co",
             "note": None, "created_at": _iso(-1)},
        ]
        detail_result = {
            "status": "success",
            "current_status": "viewed",
            "view_count": 3,
            "ars_score": 78,
        }

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders), \
             patch(
                 "naukri_server.tools.tracking._get_application_detail",
                 new_callable=AsyncMock,
                 return_value=detail_result,
             ):
            result = await _list_reminders(include_past=True, include_app_status=True)

        r = result["reminders"][0]
        assert "application_status" in r
        assert r["application_status"]["current_status"] == "viewed"
        assert r["application_status"]["view_count"] == 3
        assert r["application_status"]["ars_score"] == 78

    @pytest.mark.asyncio
    async def test_app_status_error_detail_not_attached(self):
        """If _get_application_detail returns an error dict, no application_status should appear."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {"job_id": "J601", "remind_at": _iso(5), "title": "QA", "company": "Co",
             "note": None, "created_at": _iso(-1)},
        ]
        detail_result = {"status": "error", "message": "Not found"}

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders), \
             patch(
                 "naukri_server.tools.tracking._get_application_detail",
                 new_callable=AsyncMock,
                 return_value=detail_result,
             ):
            result = await _list_reminders(include_past=True, include_app_status=True)

        r = result["reminders"][0]
        assert "application_status" not in r

    @pytest.mark.asyncio
    async def test_app_status_exception_not_attached(self):
        """If _get_application_detail raises, that reminder should not get application_status."""
        from naukri_server.tools.reminders import _list_reminders

        reminders = [
            {"job_id": "J602", "remind_at": _iso(5), "title": "Ops", "company": "Co",
             "note": None, "created_at": _iso(-1)},
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=reminders), \
             patch(
                 "naukri_server.tools.tracking._get_application_detail",
                 new_callable=AsyncMock,
                 side_effect=RuntimeError("Timeout"),
             ):
            result = await _list_reminders(include_past=True, include_app_status=True)

        r = result["reminders"][0]
        assert "application_status" not in r


# =====================================================================
# 6. Error paths — NaukriAPIError caught by naukri_reminders tool
# =====================================================================

class TestErrorPaths:
    """Tests for exception handling in the atomic reminder tools."""

    @pytest.mark.asyncio
    async def test_list_error_returns_api_error(self):
        """If _list_reminders raises NaukriAPIError, naukri_list_reminders returns API_ERROR."""
        from naukri_server.tools.reminders import naukri_list_reminders
        from naukri_server.api import NaukriAPIError

        with patch(
            "naukri_server.tools.reminders._list_reminders",
            new_callable=AsyncMock,
            side_effect=NaukriAPIError(500, "Internal Server Error"),
        ):
            result = await naukri_list_reminders()

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Internal Server Error" in result["message"]

    @pytest.mark.asyncio
    async def test_set_error_returns_api_error(self):
        """If _set_reminder raises NaukriAPIError, naukri_set_reminder returns API_ERROR."""
        from naukri_server.tools.reminders import naukri_set_reminder
        from naukri_server.api import NaukriAPIError

        with patch(
            "naukri_server.tools.reminders._set_reminder",
            new_callable=AsyncMock,
            side_effect=NaukriAPIError(503, "Service Unavailable"),
        ):
            result = await naukri_set_reminder(job_id="J999", days=7)

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Service Unavailable" in result["message"]

    @pytest.mark.asyncio
    async def test_list_generic_exception_caught(self):
        """A plain RuntimeError from _list_reminders is caught as INTERNAL_ERROR."""
        from naukri_server.tools.reminders import naukri_list_reminders

        with patch(
            "naukri_server.tools.reminders._list_reminders",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Disk full"),
        ):
            result = await naukri_list_reminders()

        assert result["status"] == "error"
        assert result["error_code"] == "INTERNAL_ERROR"
        assert "RuntimeError" in result["message"]


# =====================================================================
# Atomic tools — additional routing variants
# =====================================================================

class TestRemindersAtomicExtras:
    """Additional routing tests for atomic reminder tools."""

    @pytest.mark.asyncio
    async def test_list_routes_to_helper(self):
        from naukri_server.tools.reminders import naukri_list_reminders
        with patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "total": 0, "reminders": []}
            result = await naukri_list_reminders()
            mock_helper.assert_awaited_once_with(include_past=True, include_app_status=True)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_list_passes_include_app_status_false(self):
        from naukri_server.tools.reminders import naukri_list_reminders
        with patch("naukri_server.tools.reminders._list_reminders", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "total": 0, "reminders": []}
            result = await naukri_list_reminders(include_app_status=False)
            mock_helper.assert_awaited_once_with(include_past=True, include_app_status=False)
            assert result["status"] == "success"


# =====================================================================
# From test_consolidation.py — reminder enrichment with application status
# =====================================================================

class TestRemindersAppStatus:
    """Tests for reminder enrichment with live application status."""

    @pytest.mark.asyncio
    async def test_list_reminders_with_app_status(self):
        """Reminders include application_status when include_app_status=True."""
        from datetime import datetime, timezone, timedelta
        from naukri_server.tools.reminders import _list_reminders

        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        fake_reminders = [
            {"job_id": "j1", "title": "Dev", "company": "Acme", "remind_at": future, "note": "follow up", "created_at": "2026-01-01T00:00:00+00:00"},
            {"job_id": "j2", "title": "QA", "company": "Beta", "remind_at": future, "note": None, "created_at": "2026-01-02T00:00:00+00:00"},
        ]
        fake_detail_j1 = {"status": "success", "current_status": "Application Viewed", "view_count": 5, "ars_score": 72}
        fake_detail_j2 = {"status": "success", "current_status": "Applied", "view_count": 0, "ars_score": 45}

        async def mock_get_detail(jid):
            return {"j1": fake_detail_j1, "j2": fake_detail_j2}[jid]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=fake_reminders), \
             patch("naukri_server.tools.tracking._get_application_detail", side_effect=mock_get_detail):
            result = await _list_reminders(include_app_status=True)

        assert result["status"] == "success"
        assert result["total"] == 2
        reminders = result["reminders"]

        # Both reminders should have application_status attached
        j1_rem = next(r for r in reminders if r["job_id"] == "j1")
        assert "application_status" in j1_rem
        assert j1_rem["application_status"]["current_status"] == "Application Viewed"
        assert j1_rem["application_status"]["view_count"] == 5
        assert j1_rem["application_status"]["ars_score"] == 72

        j2_rem = next(r for r in reminders if r["job_id"] == "j2")
        assert "application_status" in j2_rem
        assert j2_rem["application_status"]["current_status"] == "Applied"
        assert j2_rem["application_status"]["view_count"] == 0
        assert j2_rem["application_status"]["ars_score"] == 45

    @pytest.mark.asyncio
    async def test_list_reminders_without_app_status(self):
        """Reminders don't fetch app status when include_app_status=False."""
        from datetime import datetime, timezone, timedelta
        from naukri_server.tools.reminders import _list_reminders

        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        fake_reminders = [
            {"job_id": "j1", "title": "Dev", "company": "Acme", "remind_at": future, "note": None, "created_at": "2026-01-01T00:00:00+00:00"},
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=fake_reminders), \
             patch("naukri_server.tools.tracking._get_application_detail", new_callable=AsyncMock) as mock_detail:
            result = await _list_reminders(include_app_status=False)

        assert result["status"] == "success"
        assert result["total"] == 1
        # _get_application_detail should NOT have been called
        mock_detail.assert_not_awaited()
        # No application_status key on reminders
        assert "application_status" not in result["reminders"][0]

    @pytest.mark.asyncio
    async def test_app_status_error_is_silently_skipped(self):
        """If _get_application_detail raises for one job, that reminder has no application_status."""
        from datetime import datetime, timezone, timedelta
        from naukri_server.tools.reminders import _list_reminders

        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        fake_reminders = [
            {"job_id": "j1", "title": "Dev", "company": "Acme", "remind_at": future, "note": None, "created_at": "2026-01-01T00:00:00+00:00"},
            {"job_id": "j2", "title": "QA", "company": "Beta", "remind_at": future, "note": None, "created_at": "2026-01-02T00:00:00+00:00"},
        ]

        async def mock_get_detail(jid):
            if jid == "j1":
                raise ConnectionError("API down")
            return {"status": "success", "current_status": "Applied", "view_count": 1, "ars_score": 50}

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=fake_reminders), \
             patch("naukri_server.tools.tracking._get_application_detail", side_effect=mock_get_detail):
            result = await _list_reminders(include_app_status=True)

        assert result["status"] == "success"
        j1_rem = next(r for r in result["reminders"] if r["job_id"] == "j1")
        j2_rem = next(r for r in result["reminders"] if r["job_id"] == "j2")

        # j1 had an error — no application_status
        assert "application_status" not in j1_rem
        # j2 succeeded
        assert "application_status" in j2_rem
        assert j2_rem["application_status"]["current_status"] == "Applied"

    @pytest.mark.asyncio
    async def test_app_status_error_response_is_skipped(self):
        """If _get_application_detail returns status=error, that reminder has no application_status."""
        from datetime import datetime, timezone, timedelta
        from naukri_server.tools.reminders import _list_reminders

        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        fake_reminders = [
            {"job_id": "j1", "title": "Dev", "company": "Acme", "remind_at": future, "note": None, "created_at": "2026-01-01T00:00:00+00:00"},
        ]

        async def mock_get_detail(jid):
            return {"status": "error", "message": "Not found"}

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=fake_reminders), \
             patch("naukri_server.tools.tracking._get_application_detail", side_effect=mock_get_detail):
            result = await _list_reminders(include_app_status=True)

        assert result["status"] == "success"
        assert "application_status" not in result["reminders"][0]

    @pytest.mark.asyncio
    async def test_app_status_skips_reminders_without_job_id(self):
        """Reminders without job_id don't trigger detail fetches."""
        from datetime import datetime, timezone, timedelta
        from naukri_server.tools.reminders import _list_reminders

        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        fake_reminders = [
            {"job_id": None, "title": "Generic", "company": "Co", "remind_at": future, "note": None, "created_at": "2026-01-01T00:00:00+00:00"},
            {"title": "No ID", "company": "Co2", "remind_at": future, "note": None, "created_at": "2026-01-02T00:00:00+00:00"},
        ]

        with patch("naukri_server.database.list_reminders", new_callable=AsyncMock, return_value=fake_reminders), \
             patch("naukri_server.tools.tracking._get_application_detail", new_callable=AsyncMock) as mock_detail:
            result = await _list_reminders(include_app_status=True)

        # No job_ids to fetch, so _get_application_detail should never be called
        mock_detail.assert_not_awaited()
        assert result["total"] == 2
        for rem in result["reminders"]:
            assert "application_status" not in rem
