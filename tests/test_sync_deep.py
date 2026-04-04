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

class TestAutoPurge:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.database.delete_applications_before", new_callable=AsyncMock)
    @patch("naukri_server.database.upsert_application", new_callable=AsyncMock)
    @patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock)
    async def test_old_apps_purged_during_sync(self, mock_fetch, mock_list_all, mock_upsert, mock_delete, mock_load_state, mock_save_state):
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
        # Check that only "recent1" was upserted (old1 was purged)
        upserted_ids = [call.args[0]["job_id"] for call in mock_upsert.call_args_list]
        assert "old1" not in upserted_ids
        assert "recent1" in upserted_ids
        assert result["purged"] >= 1

    @pytest.mark.asyncio
    @patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.database.delete_applications_before", new_callable=AsyncMock)
    @patch("naukri_server.database.upsert_application", new_callable=AsyncMock)
    @patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock)
    async def test_manual_apps_not_purged(self, mock_fetch, mock_list_all, mock_upsert, mock_delete, mock_load_state, mock_save_state):
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
        assert "synced1" not in upserted_ids

    @pytest.mark.asyncio
    @patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.database.delete_applications_before", new_callable=AsyncMock)
    @patch("naukri_server.database.upsert_application", new_callable=AsyncMock)
    @patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock)
    async def test_recent_apps_kept(self, mock_fetch, mock_list_all, mock_upsert, mock_delete, mock_load_state, mock_save_state):
        dates = [
            (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()
            for d in (1, 30, 90, 179)
        ]
        mock_list_all.return_value = [
            {"job_id": f"app{i}", "applied_at": d, "status": "applied", "source": "naukri_sync"}
            for i, d in enumerate(dates)
        ]
        mock_fetch.return_value = []
        mock_load_state.return_value = {}

        from naukri_server.tools.sync import _sync_applications
        result = await _sync_applications()
        assert mock_upsert.call_count == 4
        assert result.get("purged", 0) == 0


# ---------------------------------------------------------------------------
# 2. _merge_applications — field refresh on resync (tier 23)
# ---------------------------------------------------------------------------

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
