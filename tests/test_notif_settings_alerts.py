"""Tests for notifications, settings, and alerts MCP tools — action routing and parameter validation.

Every test is PURE: no network, no browser, no file I/O.
We only exercise validation / routing logic that runs BEFORE any async helper call.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. naukri_notifications
# =====================================================================

class TestNotifications:
    """Tests for naukri_server.tools.notifications atomic tools."""

    @pytest.mark.asyncio
    async def test_notifications_mark_read_requires_id(self):
        """naukri_mark_notification_read without notification_id should fail validation."""
        from naukri_server.tools.notifications import naukri_mark_notification_read
        result = await naukri_mark_notification_read()
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "notification_id" in result["message"]

    @pytest.mark.asyncio
    async def test_notifications_mark_read_requires_date(self):
        """naukri_mark_notification_read with notification_id but without date should fail validation."""
        from naukri_server.tools.notifications import naukri_mark_notification_read
        result = await naukri_mark_notification_read(notification_id="n123")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "notification_id" in result["message"] or "date" in result["message"]

    @pytest.mark.asyncio
    async def test_notifications_list_routes(self):
        """naukri_list_notifications should delegate to _fetch_notifications."""
        from naukri_server.tools.notifications import naukri_list_notifications
        with patch("naukri_server.tools.notifications._fetch_notifications", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "count": 0, "notifications": []}
            result = await naukri_list_notifications(limit=10, page=2)
            mock_helper.assert_awaited_once_with(limit=10, page=2, notif_type=None)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_notifications_count_routes(self):
        """naukri_notification_count should call api_get with NOTIFICATION_COUNT_API."""
        from naukri_server.tools.notifications import naukri_notification_count
        with patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"count": 5}
            result = await naukri_notification_count()
            mock_api.assert_awaited_once()
            assert result["status"] == "success"
            assert result["count"] == 5


# =====================================================================
# 2. naukri_settings
# =====================================================================

class TestSettings:
    """Tests for atomic naukri_*_settings tools."""

    @pytest.mark.asyncio
    async def test_settings_get_routes(self):
        """naukri_get_settings should call api_get and return parsed settings."""
        from naukri_server.tools.settings import naukri_get_settings
        with patch("naukri_server.tools.settings.api_client.get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"sections": []}
            result = await naukri_get_settings()
            assert mock_api.await_count >= 1  # formatted + raw settings APIs
            assert result["status"] == "success"
            assert "settings" in result

    @pytest.mark.asyncio
    async def test_settings_update_with_no_fields(self):
        """naukri_update_settings with no settings fields should fail validation."""
        from naukri_server.tools.settings import naukri_update_settings
        result = await naukri_update_settings()
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "No settings" in result["message"]

    @pytest.mark.asyncio
    async def test_settings_update_invalid_job_search_status(self):
        """naukri_update_settings with invalid job_search_status should fail validation."""
        from naukri_server.tools.settings import naukri_update_settings
        result = await naukri_update_settings(job_search_status="retired")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_search_status" in result["message"].lower() or "retired" in result["message"].lower()


# =====================================================================
# 3. naukri_job_alerts
# =====================================================================

class TestAlerts:
    """Tests for atomic naukri alert tools (list/detail/create/update/delete)."""

    @pytest.mark.asyncio
    async def test_alerts_create_requires_params(self):
        """naukri_create_alert without name and keywords should fail validation."""
        from naukri_server.tools.alerts import naukri_create_alert
        result = await naukri_create_alert(name="", keywords="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "name" in result["message"] or "keywords" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_create_requires_keywords(self):
        """naukri_create_alert with name but without keywords should fail validation."""
        from naukri_server.tools.alerts import naukri_create_alert
        result = await naukri_create_alert(name="My Alert", keywords="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keywords" in result["message"] or "name" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_update_requires_alert_id(self):
        """naukri_update_alert without alert_id should fail validation."""
        from naukri_server.tools.alerts import naukri_update_alert
        result = await naukri_update_alert(alert_id="", keywords="python")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "alert_id" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_delete_requires_alert_id(self):
        """naukri_delete_alert without alert_id should fail validation."""
        from naukri_server.tools.alerts import naukri_delete_alert
        result = await naukri_delete_alert(alert_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "alert_id" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_detail_requires_alert_id(self):
        """naukri_alert_detail without alert_id should fail validation."""
        from naukri_server.tools.alerts import naukri_alert_detail
        result = await naukri_alert_detail(alert_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "alert_id" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_list_routes(self):
        """naukri_list_alerts should delegate to _get_alerts_list."""
        from naukri_server.tools.alerts import naukri_list_alerts
        with patch("naukri_server.tools.alerts._get_alerts_list", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "count": 0, "alerts": []}
            result = await naukri_list_alerts()
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"


# =====================================================================
# 4. Helper-level validation
# =====================================================================

class TestNotifSettingsHelperValidation:
    """Validation inside internal helpers that runs before any API call."""

    @pytest.mark.asyncio
    async def test_fetch_notifications_page_clamped_zero(self):
        """page=0 is silently clamped to 1 by validate_page (no error)."""
        from naukri_server.tools.notifications import _fetch_notifications
        with patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"results": []}
            result = await _fetch_notifications(page=0)
            mock_api.assert_awaited()
