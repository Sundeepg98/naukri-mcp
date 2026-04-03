"""Tests for Tier 25: auto-purge during sync and DATA_DIR file path configuration.

Every test is PURE: no network, no browser, no file I/O.

Auto-purge behavior:
  - Applications older than AUTO_PURGE_DAYS (180) are removed during sync.
  - Applications with source="manual" are never auto-purged regardless of age.
  - The sync result includes a 'purged' count field.

File path configuration:
  - DATA_DIR can be overridden via NAUKRI_DATA_DIR env var.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# 1. Auto-Purge During Sync
# ---------------------------------------------------------------------------

class TestAutoPurge:
    """Verify that old applications are auto-purged during sync."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._save_json")
    @patch("naukri_server.tools.sync._load_json")
    @patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock)
    async def test_old_apps_purged_during_sync(
        self, mock_fetch, mock_load, mock_save, mock_load_state, mock_save_state
    ):
        """Applications older than AUTO_PURGE_DAYS (180) are removed during sync."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()

        # Local has 1 old app and 1 recent app
        mock_load.return_value = [
            {"job_id": "old1", "applied_at": old_date, "status": "applied", "source": "naukri_sync"},
            {"job_id": "recent1", "applied_at": recent_date, "status": "applied", "source": "naukri_sync"},
        ]
        # Remote returns empty (no new apps to merge)
        mock_fetch.return_value = []
        mock_load_state.return_value = {}

        from naukri_server.tools.sync import _sync_applications
        result = await _sync_applications()

        assert result["status"] == "success"
        # Verify that _save_json was called
        assert mock_save.called
        saved_apps = mock_save.call_args[0][1]

        # The old app (200 days) should be purged, only recent app remains
        job_ids = [a["job_id"] for a in saved_apps]
        assert "old1" not in job_ids, "Old app (200 days) should be auto-purged"
        assert "recent1" in job_ids, "Recent app (10 days) should be kept"

        # Result should include purged count
        assert "purged" in result, "Sync result should include 'purged' count"
        assert result["purged"] >= 1

    @pytest.mark.asyncio
    @patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._save_json")
    @patch("naukri_server.tools.sync._load_json")
    @patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock)
    async def test_manual_apps_not_purged(
        self, mock_fetch, mock_load, mock_save, mock_load_state, mock_save_state
    ):
        """Applications with source='manual' are never auto-purged even if old."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()

        # Local has 1 old manual app and 1 old synced app
        mock_load.return_value = [
            {"job_id": "manual1", "applied_at": old_date, "status": "applied", "source": "manual"},
            {"job_id": "synced1", "applied_at": old_date, "status": "applied", "source": "naukri_sync"},
        ]
        mock_fetch.return_value = []
        mock_load_state.return_value = {}

        from naukri_server.tools.sync import _sync_applications
        result = await _sync_applications()

        assert result["status"] == "success"
        assert mock_save.called
        saved_apps = mock_save.call_args[0][1]

        job_ids = [a["job_id"] for a in saved_apps]
        assert "manual1" in job_ids, "Manual-source app should NOT be auto-purged"
        assert "synced1" not in job_ids, "Old synced app should be auto-purged"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.sync._save_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._load_sync_state_async", new_callable=AsyncMock)
    @patch("naukri_server.tools.sync._save_json")
    @patch("naukri_server.tools.sync._load_json")
    @patch("naukri_server.tools.sync._fetch_applied_jobs_rest", new_callable=AsyncMock)
    async def test_recent_apps_kept(
        self, mock_fetch, mock_load, mock_save, mock_load_state, mock_save_state
    ):
        """Applications within AUTO_PURGE_DAYS (180) are kept during sync."""
        dates = [
            (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
            (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),
            (datetime.now(timezone.utc) - timedelta(days=179)).isoformat(),
        ]

        mock_load.return_value = [
            {"job_id": f"app{i}", "applied_at": d, "status": "applied", "source": "naukri_sync"}
            for i, d in enumerate(dates)
        ]
        mock_fetch.return_value = []
        mock_load_state.return_value = {}

        from naukri_server.tools.sync import _sync_applications
        result = await _sync_applications()

        assert result["status"] == "success"
        assert mock_save.called
        saved_apps = mock_save.call_args[0][1]

        # All 4 apps are within 180 days, none should be purged
        assert len(saved_apps) == 4, f"All 4 recent apps should be kept, got {len(saved_apps)}"
        assert result.get("purged", 0) == 0


# ---------------------------------------------------------------------------
# 2. DATA_DIR File Path Configuration via Env Var
# ---------------------------------------------------------------------------

class TestDataDirConfig:
    """Verify that DATA_DIR (file paths) can be overridden via environment variable."""

    def test_data_dir_override_via_env(self, tmp_path):
        """NAUKRI_DATA_DIR env var overrides the default data directory for tracking files."""
        import importlib
        custom_dir = str(tmp_path / "custom_data")

        with patch.dict("os.environ", {"NAUKRI_DATA_DIR": custom_dir}):
            # Force re-import config first (where DATA_DIR is defined), then tracking
            import naukri_server.config as config_mod
            importlib.reload(config_mod)
            import naukri_server.tools.tracking as tracking_mod
            importlib.reload(tracking_mod)

            apps_file = tracking_mod.APPLICATIONS_FILE
            saved_file = tracking_mod.SAVED_JOBS_FILE

            # Verify the files point to the custom directory
            assert str(apps_file).startswith(custom_dir), (
                f"APPLICATIONS_FILE should use custom dir, got {apps_file}"
            )
            assert str(saved_file).startswith(custom_dir), (
                f"SAVED_JOBS_FILE should use custom dir, got {saved_file}"
            )

        # Restore defaults after test
        with patch.dict("os.environ", {}, clear=False):
            if "NAUKRI_DATA_DIR" in __import__("os").environ:
                del __import__("os").environ["NAUKRI_DATA_DIR"]
            importlib.reload(config_mod)
            importlib.reload(tracking_mod)
