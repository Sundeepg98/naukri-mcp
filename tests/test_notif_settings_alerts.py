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
    """Tests for naukri_server.tools.notifications.naukri_notifications."""

    @pytest.mark.asyncio
    async def test_notifications_invalid_action(self):
        from naukri_server.tools.notifications import naukri_notifications
        result = await naukri_notifications(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_notifications_mark_read_requires_id(self):
        """mark_read without notification_id should fail validation."""
        from naukri_server.tools.notifications import naukri_notifications
        result = await naukri_notifications(action="mark_read")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "notification_id" in result["message"]

    @pytest.mark.asyncio
    async def test_notifications_mark_read_requires_date(self):
        """mark_read with notification_id but without date should fail validation."""
        from naukri_server.tools.notifications import naukri_notifications
        result = await naukri_notifications(action="mark_read", notification_id="n123")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "date" in result["message"]

    @pytest.mark.asyncio
    async def test_notifications_list_routes(self):
        """list action should delegate to _fetch_notifications."""
        from naukri_server.tools.notifications import naukri_notifications
        with patch("naukri_server.tools.notifications._fetch_notifications", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "count": 0, "notifications": []}
            result = await naukri_notifications(action="list", limit=10, page=2)
            mock_helper.assert_awaited_once_with(limit=10, page=2, notif_type=None)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_notifications_count_routes(self):
        """count action should call api_get with NOTIFICATION_COUNT_API."""
        from naukri_server.tools.notifications import naukri_notifications
        with patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"count": 5}
            result = await naukri_notifications(action="count")
            mock_api.assert_awaited_once()
            assert result["status"] == "success"
            assert result["count"] == 5


# =====================================================================
# 2. naukri_settings
# =====================================================================

class TestSettings:
    """Tests for naukri_server.tools.settings.naukri_settings."""

    @pytest.mark.asyncio
    async def test_settings_invalid_action(self):
        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_settings_get_routes(self):
        """get action should call api_get and return parsed settings."""
        from naukri_server.tools.settings import naukri_settings
        with patch("naukri_server.tools.settings.api_get", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"sections": []}
            result = await naukri_settings(action="get")
            mock_api.assert_awaited_once()
            assert result["status"] == "success"
            assert "settings" in result

    @pytest.mark.asyncio
    async def test_settings_update_with_no_fields(self):
        """update with no settings fields should fail validation."""
        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="update")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "No settings" in result["message"]

    @pytest.mark.asyncio
    async def test_settings_update_invalid_job_search_status(self):
        """update with invalid job_search_status should fail validation."""
        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="update", job_search_status="retired")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_search_status" in result["message"].lower() or "retired" in result["message"].lower()


# =====================================================================
# 3. naukri_job_alerts
# =====================================================================

class TestAlerts:
    """Tests for naukri_server.tools.alerts.naukri_job_alerts."""

    @pytest.mark.asyncio
    async def test_alerts_invalid_action(self):
        from naukri_server.tools.alerts import naukri_job_alerts
        result = await naukri_job_alerts(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_create_requires_params(self):
        """create without name and keywords should fail validation."""
        from naukri_server.tools.alerts import naukri_job_alerts
        result = await naukri_job_alerts(action="create")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "name" in result["message"] or "keywords" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_create_requires_keywords(self):
        """create with name but without keywords should fail validation."""
        from naukri_server.tools.alerts import naukri_job_alerts
        result = await naukri_job_alerts(action="create", name="My Alert")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keywords" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_update_requires_alert_id(self):
        """update without alert_id should fail validation."""
        from naukri_server.tools.alerts import naukri_job_alerts
        result = await naukri_job_alerts(action="update", keywords="python")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "alert_id" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_delete_requires_alert_id(self):
        """delete without alert_id should fail validation."""
        from naukri_server.tools.alerts import naukri_job_alerts
        result = await naukri_job_alerts(action="delete")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "alert_id" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_detail_requires_alert_id(self):
        """detail without alert_id should fail validation."""
        from naukri_server.tools.alerts import naukri_job_alerts
        result = await naukri_job_alerts(action="detail")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "alert_id" in result["message"]

    @pytest.mark.asyncio
    async def test_alerts_list_routes(self):
        """list action should delegate to _get_alerts_list."""
        from naukri_server.tools.alerts import naukri_job_alerts
        with patch("naukri_server.tools.alerts._get_alerts_list", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "count": 0, "alerts": []}
            result = await naukri_job_alerts(action="list")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"


# =====================================================================
# 4. Helper-level validation
# =====================================================================

class TestHelperValidation:
    """Validation inside internal helpers that runs before any API call."""

    @pytest.mark.asyncio
    async def test_fetch_notifications_page_validation(self):
        """_fetch_notifications rejects page < 1."""
        from naukri_server.tools.notifications import _fetch_notifications
        result = await _fetch_notifications(page=0)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "page" in result["message"]
