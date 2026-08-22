"""Deep tests for naukri_server.tools.sync — auto-purge during sync, merge field refresh.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier25_auto_purge.py and tier23.py.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# 1. Auto-Purge During Sync
# ---------------------------------------------------------------------------

class TestSyncNeverDeletes:
    """A sync merges and persists. It does NOT delete -- at any age.

    This class used to be TestAutoPurge and asserted the opposite: that
    _sync_applications dropped applications past AUTO_PURGE_DAYS. Those two
    tests are why the bug shipped green. The purge compared the horizon against
    `applied_at`, which is the timestamp the row was RECORDED, so on the live
    account (2026-08-22) it was set to delete 149 of his 162 applications
    between 2026-08-27 and 2026-09-08 -- and naukri_status_changes, a read,
    ran the same sync. The behaviour is gone; the tests now pin its absence.
    """

    @pytest.mark.asyncio
    @patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.database.delete_applications_before", new_callable=AsyncMock)
    @patch("naukri_server.database.upsert_application", new_callable=AsyncMock)
    @patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock)
    async def test_applications_past_the_old_horizon_are_kept_and_persisted(
        self, mock_fetch, mock_list_all, mock_upsert, mock_delete, mock_load_state, mock_save_state
    ):
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        mock_list_all.return_value = [
            {"job_id": "old1", "applied_at": old_date, "status": "applied", "source": "naukri_sync"},
            {"job_id": "recent1", "applied_at": recent_date, "status": "applied", "source": "naukri_sync"},
        ]
        mock_fetch.return_value = []
        mock_load_state.return_value = {}

        from naukri_server.tools.sync import _sync_applications
        result = await _sync_applications()

        assert result["status"] == "success"
        upserted_ids = [call.args[0]["job_id"] for call in mock_upsert.call_args_list]
        assert "old1" in upserted_ids, "a 200-day-old application must survive a sync"
        assert "recent1" in upserted_ids
        assert result["purged"] == 0
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.database.delete_applications_before", new_callable=AsyncMock)
    @patch("naukri_server.database.upsert_application", new_callable=AsyncMock)
    @patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock)
    async def test_source_no_longer_decides_who_survives(
        self, mock_fetch, mock_list_all, mock_upsert, mock_delete, mock_load_state, mock_save_state
    ):
        """`source == "manual"` was the only thing exempting a row from the
        purge, which meant every naukri_sync row -- 161 of his 162 -- was
        exposed. With no purge, source is irrelevant to survival."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        mock_list_all.return_value = [
            {"job_id": "manual1", "applied_at": old_date, "status": "applied", "source": "manual"},
            {"job_id": "synced1", "applied_at": old_date, "status": "applied", "source": "naukri_sync"},
        ]
        mock_fetch.return_value = []
        mock_load_state.return_value = {}

        from naukri_server.tools.sync import _sync_applications
        result = await _sync_applications()

        upserted_ids = [call.args[0]["job_id"] for call in mock_upsert.call_args_list]
        assert "manual1" in upserted_ids
        assert "synced1" in upserted_ids, "a synced row must survive exactly as a manual one does"
        assert result["purged"] == 0
        mock_delete.assert_not_called()


class TestSyncMergeFieldRefresh:
    def test_ars_score_updated_on_resync(self):
        from naukri_server.tools.sync import _merge_applications
        local = [{"job_id": "J1", "ars_score": 50, "status": "applied"}]
        remote = [{"job_id": "J1", "ars_score": 80, "status": "applied"}]
        _merge_applications(local, remote)
        assert local[0]["ars_score"] == 80

    def test_star_rating_updated_on_resync(self):
        from naukri_server.tools.sync import _merge_applications
        local = [{"job_id": "J1", "star_rating": 3, "status": "applied"}]
        remote = [{"job_id": "J1", "star_rating": 5, "status": "applied"}]
        _merge_applications(local, remote)
        assert local[0]["star_rating"] == 5

    def test_company_rating_updated_on_resync(self):
        from naukri_server.tools.sync import _merge_applications
        local = [{"job_id": "J1", "company_rating": {"rating": 3.5}, "status": "applied"}]
        remote = [{"job_id": "J1", "company_rating": {"rating": 4.2, "reviews": 500}, "status": "applied"}]
        _merge_applications(local, remote)
        assert local[0]["company_rating"] == {"rating": 4.2, "reviews": 500}
