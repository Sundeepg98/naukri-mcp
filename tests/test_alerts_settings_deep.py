"""Deep tests for alerts and settings — CTC conversion, API parsing, action routing, config constants.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. Config constant tests (sync — no async needed)
# =====================================================================

class TestConfigConstants:
    """Verify config constants exist with expected types and values."""

    def test_config_constants_exist_and_correct(self):
        from naukri_server.config import (
            DAILY_APPLY_QUOTA, BATCH_APPLY_DEFAULT_DELAY_MS, MAX_BULK_JOBS,
            PROFILE_CACHE_TTL, STALE_THRESHOLD_DAYS, AMBITIONBOX_WAIT_TIMEOUT,
            AMBITIONBOX_FALLBACK_SLEEP, LAKHS_MULTIPLIER,
        )
        assert isinstance(DAILY_APPLY_QUOTA, int) and DAILY_APPLY_QUOTA == 50
        assert isinstance(BATCH_APPLY_DEFAULT_DELAY_MS, int) and BATCH_APPLY_DEFAULT_DELAY_MS == 500
        assert isinstance(MAX_BULK_JOBS, int) and MAX_BULK_JOBS == 50
        assert isinstance(PROFILE_CACHE_TTL, int) and PROFILE_CACHE_TTL == 30
        assert isinstance(STALE_THRESHOLD_DAYS, int) and STALE_THRESHOLD_DAYS == 14
        assert isinstance(AMBITIONBOX_WAIT_TIMEOUT, int) and AMBITIONBOX_WAIT_TIMEOUT == 10000
        assert isinstance(AMBITIONBOX_FALLBACK_SLEEP, int) and AMBITIONBOX_FALLBACK_SLEEP == 2
        assert LAKHS_MULTIPLIER == 100_000

    def test_api_endpoint_strings(self):
        """Key API endpoint constants should be non-empty strings."""
        from naukri_server.config import (
            JOB_ALERT_API, JOB_ALERTS_LIST_API, ALERT_DETAIL_API,
            FORMATTED_SETTINGS_API, SETTINGS_API, BLOCKED_COMPANIES_API,
        )
        for endpoint in (JOB_ALERT_API, JOB_ALERTS_LIST_API, ALERT_DETAIL_API,
                         FORMATTED_SETTINGS_API, SETTINGS_API, BLOCKED_COMPANIES_API):
            assert isinstance(endpoint, str) and len(endpoint) > 0


# =====================================================================
# 2. Alert CTC conversion tests
# =====================================================================

class TestAlertCTCConversion:
    """Test _get_alerts_list CTC float-cast handling."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.alerts.api_client.get", new_callable=AsyncMock)
    async def test_ctc_integer_strings(self, mock_get):
        """Integer string CTC values are converted to lakhs correctly."""
        mock_get.return_value = {
            "list": [
                {
                    "alertId": "a1", "name": "Python Dev", "keywords": "python",
                    "minCTC": "1500000", "maxCTC": "3000000",
                },
            ]
        }
        from naukri_server.tools.alerts import _get_alerts_list
        result = await _get_alerts_list()
        assert result["status"] == "success"
        assert result["count"] == 1
        alert = result["alerts"][0]
        assert alert["min_ctc"] == 15.0   # 1500000 / 100000
        assert alert["max_ctc"] == 30.0   # 3000000 / 100000
        assert alert["ctc_unit"] == "lakhs"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.alerts.api_client.get", new_callable=AsyncMock)
    async def test_ctc_fractional_lakhs(self, mock_get):
        """CTC values that don't divide evenly into lakhs get rounded to 2 decimals."""
        mock_get.return_value = {
            "list": [{"alertId": "a2", "minCTC": "1550000", "maxCTC": "2575000"}]
        }
        from naukri_server.tools.alerts import _get_alerts_list
        result = await _get_alerts_list()
        alert = result["alerts"][0]
        assert alert["min_ctc"] == 15.5    # 1550000 / 100000
        assert alert["max_ctc"] == 25.75   # 2575000 / 100000

    @pytest.mark.asyncio
    @patch("naukri_server.tools.alerts.api_client.get", new_callable=AsyncMock)
    async def test_ctc_zero_string_is_falsy_path(self, mock_get):
        """maxCTC="0" is a truthy string, so float("0")/100000 = 0.0 (not None)."""
        mock_get.return_value = {
            "list": [{"alertId": "a3", "minCTC": "500000", "maxCTC": "0"}]
        }
        from naukri_server.tools.alerts import _get_alerts_list
        result = await _get_alerts_list()
        alert = result["alerts"][0]
        assert alert["min_ctc"] == 5.0
        # "0" is a non-empty string → truthy → enters float branch → 0.0
        assert alert["max_ctc"] == 0.0

    @pytest.mark.asyncio
    @patch("naukri_server.tools.alerts.api_client.get", new_callable=AsyncMock)
    async def test_ctc_missing_fields_are_none(self, mock_get):
        """Alerts without minCTC/maxCTC keys should have None values."""
        mock_get.return_value = {
            "list": [{"alertId": "a4", "name": "React Dev"}]
        }
        from naukri_server.tools.alerts import _get_alerts_list
        result = await _get_alerts_list()
        alert = result["alerts"][0]
        assert alert["min_ctc"] is None
        assert alert["max_ctc"] is None
        assert alert["alert_id"] == "a4"
        assert alert["name"] == "React Dev"


# =====================================================================
# 3. Alert list parsing
# =====================================================================

class TestAlertListParsing:
    """Test _get_alerts_list response parsing edge cases."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.alerts.api_client.get", new_callable=AsyncMock)
    async def test_empty_list(self, mock_get):
        mock_get.return_value = {"list": []}
        from naukri_server.tools.alerts import _get_alerts_list
        result = await _get_alerts_list()
        assert result["status"] == "success"
        assert result["count"] == 0
        assert result["alerts"] == []

    @pytest.mark.asyncio
    @patch("naukri_server.tools.alerts.api_client.get", new_callable=AsyncMock)
    async def test_multiple_alerts_preserves_all_fields(self, mock_get):
        """All mapped fields from API response appear in the output."""
        mock_get.return_value = {
            "list": [
                {
                    "alertId": "id1", "name": "Alert One", "keywords": "java",
                    "location": "Mumbai", "experience": "5",
                    "minCTC": "800000", "maxCTC": "2000000",
                    "alertType": "ssa", "functionAreaId": "fa1",
                    "roleId": "r1", "industryTypeId": "it1",
                },
                {
                    "alertId": "id2", "name": "Alert Two", "keywords": "python",
                    "location": "Bangalore", "experience": "3",
                    "alertType": "cja",
                },
            ]
        }
        from naukri_server.tools.alerts import _get_alerts_list
        result = await _get_alerts_list()
        assert result["count"] == 2

        a1 = result["alerts"][0]
        assert a1["alert_id"] == "id1"
        assert a1["name"] == "Alert One"
        assert a1["keywords"] == "java"
        assert a1["location"] == "Mumbai"
        assert a1["experience"] == "5"
        assert a1["min_ctc"] == 8.0
        assert a1["max_ctc"] == 20.0
        assert a1["alert_type"] == "ssa"
        assert a1["function_area_id"] == "fa1"
        assert a1["role_id"] == "r1"
        assert a1["industry_type_id"] == "it1"

        a2 = result["alerts"][1]
        assert a2["alert_id"] == "id2"
        assert a2["alert_type"] == "cja"
        assert a2["min_ctc"] is None
        assert a2["max_ctc"] is None


# =====================================================================
# 4. Settings action routing
# =====================================================================

class TestSettingsRouting:
    """Test atomic settings tools route to the correct private helpers."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_client.get", new_callable=AsyncMock)
    async def test_visibility_routes_to_profile_api(self, mock_get):
        """naukri_visibility fetches PROFILE_API with expand_level=4."""
        mock_get.return_value = {
            "resdexVisibility": {
                "showProfile": True,
                "showCurrentEmployer": True,
                "searchVisible": True,
            }
        }
        from naukri_server.tools.settings import naukri_visibility
        result = await naukri_visibility()
        assert result["status"] == "success"
        assert "visibility" in result
        assert result["visibility"]["show_profile"] is True
        assert result["visibility"]["show_current_employer"] is True
        assert result["visibility"]["search_visible"] is True
        # Verify it called api_get with the profile API and expand_level param
        mock_get.assert_awaited_once()
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["expand_level"] == "4"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_client.get", new_callable=AsyncMock)
    async def test_notification_prefs_routes(self, mock_get):
        """naukri_notification_prefs fetches PROFILE_API and extracts communicationSettings."""
        mock_get.return_value = {
            "communicationSettings": {
                "emailNotifications": True,
                "smsNotifications": True,
                "pushNotifications": True,
                "whatsappNotifications": True,
            }
        }
        from naukri_server.tools.settings import naukri_notification_prefs
        result = await naukri_notification_prefs()
        assert result["status"] == "success"
        prefs = result["notification_prefs"]
        assert prefs["email_notifications"] is True
        assert prefs["sms_notifications"] is True
        assert prefs["push_notifications"] is True
        assert prefs["whatsapp_notifications"] is True

    @pytest.mark.asyncio
    async def test_subscription_delegates(self):
        """naukri_subscription_status delegates to _get_subscription_status from subscription module."""
        mock_sub = AsyncMock(return_value={"status": "success", "is_paid": False})
        # The function does a lazy import: from naukri_server.tools.subscription import _get_subscription_status
        # Patch at the source module where it's defined.
        with patch("naukri_server.tools.subscription._get_subscription_status", mock_sub):
            from naukri_server.tools.settings import naukri_subscription_status
            result = await naukri_subscription_status()
        assert result["status"] == "success"
        mock_sub.assert_awaited_once()


# =====================================================================
# From test_tier21.py — settings consent fields
# =====================================================================

class TestSettingsConsentFields:
    """Tests for consent fields in naukri_settings(action='get')."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_client.get", new_callable=AsyncMock)
    async def test_get_returns_consent_booleans(self, mock_get):
        """GET returns naukri_auto_apply_consent, linkedin_auto_apply_consent,
        whatsapp_apply_notification, whatsapp_profile_notification as booleans."""
        async def side_effect(url, *args, **kwargs):
            if "formattedsettings" in url:
                return {"sections": []}
            else:
                # Raw settings API
                return {
                    "naukriAutoApplyConsent": 1,
                    "linkedinAutoApplyConsent": 0,
                    "applyWhatsAppNotification": 1,
                    "profileWhatsAppNotification": 0,
                }

        mock_get.side_effect = side_effect
        from naukri_server.tools.settings import naukri_get_settings
        result = await naukri_get_settings()
        assert result["status"] == "success"
        assert result["naukri_auto_apply_consent"] is True
        assert result["linkedin_auto_apply_consent"] is False
        assert result["whatsapp_apply_notification"] is True
        assert result["whatsapp_profile_notification"] is False

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_client.get", new_callable=AsyncMock)
    async def test_consent_fields_are_booleans(self, mock_get):
        """Consent fields are always booleans even when API returns integers."""
        async def side_effect(url, *args, **kwargs):
            if "formattedsettings" in url:
                return {"sections": []}
            else:
                return {
                    "naukriAutoApplyConsent": 42,
                    "linkedinAutoApplyConsent": 0,
                    "applyWhatsAppNotification": 100,
                    "profileWhatsAppNotification": 0,
                }

        mock_get.side_effect = side_effect
        from naukri_server.tools.settings import naukri_get_settings
        result = await naukri_get_settings()
        assert isinstance(result["naukri_auto_apply_consent"], bool)
        assert isinstance(result["linkedin_auto_apply_consent"], bool)
        assert isinstance(result["whatsapp_apply_notification"], bool)
        assert isinstance(result["whatsapp_profile_notification"], bool)

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_client.get", new_callable=AsyncMock)
    async def test_raw_settings_failure_still_returns_main_settings(self, mock_get):
        """If raw settings fetch fails, consent_fields is empty but main settings still returned."""
        call_count = 0

        async def side_effect(url, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if "formattedsettings" in url:
                return {
                    "sections": [
                        {
                            "sectionName": "Job Search",
                            "settings": [
                                {
                                    "settingId": "js_status",
                                    "settingLabel": "Job search status",
                                    "settingValue": "1",
                                    "settingValueLabel": "Active",
                                    "description": "",
                                }
                            ],
                        }
                    ]
                }
            else:
                # Simulate raw settings API failure
                raise Exception("Network error")

        mock_get.side_effect = side_effect
        from naukri_server.tools.settings import naukri_get_settings
        result = await naukri_get_settings()
        assert result["status"] == "success"
        assert result["count"] == 1
        assert len(result["settings"]) == 1
        # Consent fields should NOT be present
        assert "naukri_auto_apply_consent" not in result
        assert "linkedin_auto_apply_consent" not in result
        assert "whatsapp_apply_notification" not in result
        assert "whatsapp_profile_notification" not in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_client.get", new_callable=AsyncMock)
    async def test_formatted_settings_parsing(self, mock_get):
        """Formatted settings are parsed with section, id, label, value etc."""
        async def side_effect(url, *args, **kwargs):
            if "formattedsettings" in url:
                return {
                    "sections": [
                        {
                            "sectionName": "Notifications",
                            "settings": [
                                {
                                    "settingId": "rec_notif",
                                    "settingLabel": "Recruiter notification",
                                    "settingValue": "enabled",
                                    "settingValueLabel": "Enabled",
                                    "description": "Get notified when recruiters view your profile",
                                }
                            ],
                        }
                    ]
                }
            else:
                return {}

        mock_get.side_effect = side_effect
        from naukri_server.tools.settings import naukri_get_settings
        result = await naukri_get_settings()
        s = result["settings"][0]
        assert s["section"] == "Notifications"
        assert s["id"] == "rec_notif"
        assert s["label"] == "Recruiter notification"
        assert s["value"] == "enabled"
        assert s["value_label"] == "Enabled"
        assert s["description"] == "Get notified when recruiters view your profile"
