"""Tests for Tier 23 changes:
  1. Settings FORMATTED_SETTINGS_API now passes WIDGET_HEADERS (extra_headers with appid:109)
  2. _merge_applications() updates enrichment fields (ars_score, star_rating, company_rating)
  3. _list_applications() applies filter_info locally
  4. daily_brief notification_summary, recommendation clusters, recruiter-search action

Every test is PURE: no network, no browser, no file I/O.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# =====================================================================
# 1. TestSettingsWidgetHeaders
# =====================================================================

class TestSettingsWidgetHeaders:
    """FORMATTED_SETTINGS_API calls now include extra_headers=WIDGET_HEADERS (appid:109)."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_get", new_callable=AsyncMock)
    async def test_get_passes_widget_headers(self, mock_api):
        """naukri_settings(action='get') calls FORMATTED_SETTINGS_API with appid:109 header."""
        # First call → FORMATTED_SETTINGS_API (list of sections)
        # Second call → SETTINGS_API (raw consent fields, no widget headers)
        mock_api.side_effect = [
            [{"sectionName": "Job Search", "settings": [
                {"settingId": "1", "settingLabel": "Status", "settingValue": "Active"}
            ]}],
            {},  # raw settings (consent fields call)
        ]
        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="get")

        assert mock_api.call_count >= 1

        # At least one call must have extra_headers with appid:109
        widget_header_found = False
        for call in mock_api.call_args_list:
            extra = call.kwargs.get("extra_headers", {})
            if extra.get("appid") == "109":
                widget_header_found = True
                break
        assert widget_header_found, (
            "Expected at least one api_get call with extra_headers containing appid='109'. "
            f"Calls: {mock_api.call_args_list}"
        )

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_post", new_callable=AsyncMock)
    @patch("naukri_server.tools.settings.api_get", new_callable=AsyncMock)
    async def test_update_preread_passes_widget_headers(self, mock_api, mock_post):
        """naukri_settings(action='update', recommended_job_frequency='daily') fetches
        FORMATTED_SETTINGS_API with widget headers before posting."""
        # api_get for FORMATTED_SETTINGS_API during merge step
        mock_api.return_value = {"settings": {}}
        mock_post.return_value = {}

        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="update", recommended_job_frequency="daily")

        # api_get must have been called at least once
        assert mock_api.call_count >= 1

        # One of those calls must have extra_headers with appid:109
        widget_header_found = False
        for call in mock_api.call_args_list:
            extra = call.kwargs.get("extra_headers", {})
            if extra.get("appid") == "109":
                widget_header_found = True
                break
        assert widget_header_found, (
            "Expected api_get to be called with extra_headers containing appid='109' "
            "when fetching FORMATTED_SETTINGS_API for merge-before-post. "
            f"Calls: {mock_api.call_args_list}"
        )

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_get", new_callable=AsyncMock)
    async def test_raw_settings_api_no_widget_headers(self, mock_api):
        """The second api_get call (SETTINGS_API for consent fields) must NOT use widget headers."""
        mock_api.side_effect = [
            [{"sectionName": "Job Search", "settings": [
                {"settingId": "1", "settingLabel": "Status", "settingValue": "Active"}
            ]}],
            {
                "naukriAutoApplyConsent": 1,
                "linkedinAutoApplyConsent": 0,
                "applyWhatsAppNotification": 1,
                "profileWhatsAppNotification": 0,
            },
        ]
        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="get")

        assert mock_api.call_count == 2

        # Second call is SETTINGS_API — must NOT have appid:109
        second_call = mock_api.call_args_list[1]
        extra = second_call.kwargs.get("extra_headers", {})
        assert extra.get("appid") != "109", (
            "SETTINGS_API (second api_get call) must NOT include appid='109' widget header. "
            f"Got extra_headers={extra}"
        )


# =====================================================================
# 2. TestSyncMergeFieldRefresh
# =====================================================================

class TestSyncMergeFieldRefresh:
    """_merge_applications() updates enrichment fields on resync."""

    def test_ars_score_updated_on_resync(self):
        """Existing app with ars_score=50 is updated to remote ars_score=80."""
        from naukri_server.tools.sync import _merge_applications

        local = [{"job_id": "J1", "ars_score": 50, "status": "applied"}]
        remote = [{"job_id": "J1", "ars_score": 80, "status": "applied"}]

        stats = _merge_applications(local, remote)

        assert local[0]["ars_score"] == 80, (
            f"Expected ars_score=80 after resync, got {local[0]['ars_score']}"
        )

    def test_star_rating_updated_on_resync(self):
        """Existing app with star_rating=3 is updated to remote star_rating=5."""
        from naukri_server.tools.sync import _merge_applications

        local = [{"job_id": "J1", "star_rating": 3, "status": "applied"}]
        remote = [{"job_id": "J1", "star_rating": 5, "status": "applied"}]

        stats = _merge_applications(local, remote)

        assert local[0]["star_rating"] == 5, (
            f"Expected star_rating=5 after resync, got {local[0]['star_rating']}"
        )

    def test_company_rating_updated_on_resync(self):
        """Existing app with company_rating={'rating': 3.5} is updated to remote value."""
        from naukri_server.tools.sync import _merge_applications

        local = [{"job_id": "J1", "company_rating": {"rating": 3.5}, "status": "applied"}]
        remote = [{"job_id": "J1", "company_rating": {"rating": 4.2, "reviews": 500}, "status": "applied"}]

        stats = _merge_applications(local, remote)

        assert local[0]["company_rating"] == {"rating": 4.2, "reviews": 500}, (
            f"Expected company_rating to match remote after resync, got {local[0]['company_rating']}"
        )


# =====================================================================
# 3. TestFilterInfoLocal
# =====================================================================

class TestFilterInfoLocal:
    """_list_applications() applies filter_info as local source/activity filter."""

    def _make_apps(self):
        """Three sample apps with different source/activity characteristics."""
        return [
            {
                "job_id": "J1",
                "status": "applied",
                "applied_at": "2026-01-01T00:00:00+00:00",
                "recruiter_active": True,
                "source": "naukri_sync",
            },
            {
                "job_id": "J2",
                "status": "applied",
                "applied_at": "2026-01-02T00:00:00+00:00",
                "job_activity": 5,
                "source": "naukri_sync",
            },
            {
                "job_id": "J3",
                "status": "applied",
                "applied_at": "2026-01-03T00:00:00+00:00",
                # no recruiter_active, no job_activity
                "source": "manual",
            },
        ]

    @pytest.mark.asyncio
    async def test_filter_info_1_recruiter_active(self):
        """filter_info=1 returns only apps where recruiter_active or job_activity is set."""
        apps = self._make_apps()
        with patch("naukri_server.tools.tracking._load_json", return_value=apps), \
             patch("naukri_server.tools.tracking._applications_lock", asyncio.Lock()):
            from naukri_server.tools.tracking import _list_applications
            result = await _list_applications(filter_info=1)

        assert result["status"] == "success"
        assert result["count"] == 2, (
            f"filter_info=1 should return 2 apps with recruiter activity, got {result['count']}"
        )
        returned_ids = {a["job_id"] for a in result["applications"]}
        assert "J1" in returned_ids
        assert "J2" in returned_ids
        assert "J3" not in returned_ids

    @pytest.mark.asyncio
    async def test_filter_info_2_naukri_sync(self):
        """filter_info=2 returns only apps with source='naukri_sync'."""
        apps = self._make_apps()
        with patch("naukri_server.tools.tracking._load_json", return_value=apps), \
             patch("naukri_server.tools.tracking._applications_lock", asyncio.Lock()):
            from naukri_server.tools.tracking import _list_applications
            result = await _list_applications(filter_info=2)

        assert result["status"] == "success"
        assert result["count"] == 2, (
            f"filter_info=2 should return 2 naukri_sync apps, got {result['count']}"
        )
        returned_ids = {a["job_id"] for a in result["applications"]}
        assert "J1" in returned_ids
        assert "J2" in returned_ids
        assert "J3" not in returned_ids

    @pytest.mark.asyncio
    async def test_filter_info_3_external(self):
        """filter_info=3 returns only apps without source='naukri_sync' (manual/external)."""
        apps = self._make_apps()
        with patch("naukri_server.tools.tracking._load_json", return_value=apps), \
             patch("naukri_server.tools.tracking._applications_lock", asyncio.Lock()):
            from naukri_server.tools.tracking import _list_applications
            result = await _list_applications(filter_info=3)

        assert result["status"] == "success"
        assert result["count"] == 1, (
            f"filter_info=3 should return 1 external/manual app, got {result['count']}"
        )
        assert result["applications"][0]["job_id"] == "J3"

    @pytest.mark.asyncio
    async def test_filter_info_none_no_filter(self):
        """filter_info=None returns all apps without source/activity filtering."""
        apps = self._make_apps()
        with patch("naukri_server.tools.tracking._load_json", return_value=apps), \
             patch("naukri_server.tools.tracking._applications_lock", asyncio.Lock()):
            from naukri_server.tools.tracking import _list_applications
            result = await _list_applications(filter_info=None)

        assert result["status"] == "success"
        assert result["count"] == 3, (
            f"filter_info=None should return all 3 apps, got {result['count']}"
        )


# =====================================================================
# 4. TestDailyBriefEnrichment
# =====================================================================

class TestDailyBriefEnrichment:
    """Tier 23 daily_brief enrichments: notification_summary, recommendation clusters,
    and recruiter-search action generation."""

    def _run_brief_with_patches(self, patches_dict):
        """Apply all patches and return a coroutine runner helper."""
        ctx_managers = [patch(path, new=mock) for path, mock in patches_dict.items()]
        return ctx_managers

    @pytest.mark.asyncio
    async def test_notification_summary_in_brief(self):
        """naukri_daily_brief() includes notification_summary with categories and total_types."""
        from tests.test_smart_tools import TestDailyBrief
        from naukri_server.tools.daily_brief import naukri_daily_brief

        helper = TestDailyBrief()
        patches_dict = helper._build_patches(overrides={
            "naukri_server.tools.notifications._get_unified_notify": {
                "status": "success",
                "source": "unified_notify",
                "categories": {
                    "recoJobs": {"count": 10, "has_new": True},
                    "recruiterSearch": {"count": 42, "has_new": False},
                },
                "total_types": 2,
            },
        })
        ctx_managers = [patch(path, new=mock) for path, mock in patches_dict.items()]
        for cm in ctx_managers:
            cm.__enter__()
        try:
            result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        assert "notification_summary" in result, "notification_summary section missing from daily brief"
        ns = result["notification_summary"]
        assert "categories" in ns, f"notification_summary missing 'categories' key: {ns}"
        assert "total_types" in ns, f"notification_summary missing 'total_types' key: {ns}"
        assert ns["total_types"] == 2

    @pytest.mark.asyncio
    async def test_recommendation_clusters_in_brief(self):
        """naukri_daily_brief() includes clusters and agent_eligible in recommendations."""
        from tests.test_smart_tools import TestDailyBrief
        from naukri_server.tools.daily_brief import naukri_daily_brief

        helper = TestDailyBrief()
        patches_dict = helper._build_patches(overrides={
            "naukri_server.tools.search.naukri_get_recommendations": {
                "status": "success",
                "count": 3,
                "jobs": [{"job_id": "j1"}, {"job_id": "j2"}, {"job_id": "j3"}],
                "clusters": {"Python": 5, "Django": 3},
                "agent_eligible_exists": True,
            },
        })
        ctx_managers = [patch(path, new=mock) for path, mock in patches_dict.items()]
        for cm in ctx_managers:
            cm.__enter__()
        try:
            result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        recs = result.get("recommendations", {})
        assert recs.get("clusters") == {"Python": 5, "Django": 3}, (
            f"recommendations.clusters not populated correctly: {recs}"
        )
        assert recs.get("agent_eligible") is True, (
            f"recommendations.agent_eligible not True: {recs}"
        )

    @pytest.mark.asyncio
    async def test_recruiter_search_action_generated(self):
        """When recruiterSearch count > 0, recommended_actions includes a high-priority action
        mentioning the count and 'recruiter'."""
        from tests.test_smart_tools import TestDailyBrief
        from naukri_server.tools.daily_brief import naukri_daily_brief

        helper = TestDailyBrief()
        patches_dict = helper._build_patches(overrides={
            "naukri_server.tools.notifications._get_unified_notify": {
                "status": "success",
                "source": "unified_notify",
                "categories": {
                    "recruiterSearch": {"count": 1368, "has_new": True},
                },
                "total_types": 1,
            },
        })
        ctx_managers = [patch(path, new=mock) for path, mock in patches_dict.items()]
        for cm in ctx_managers:
            cm.__enter__()
        try:
            result = await naukri_daily_brief()
        finally:
            for cm in ctx_managers:
                cm.__exit__(None, None, None)

        actions = result.get("recommended_actions", [])
        assert actions, "recommended_actions is empty — expected at least one action"

        # Find an action mentioning 1368 and recruiter
        matching = [
            a for a in actions
            if "1368" in a.get("action", "") and "recruiter" in a.get("action", "").lower()
        ]
        assert matching, (
            f"Expected a recommended_action mentioning '1368' and 'recruiter'. "
            f"Got actions: {[a['action'] for a in actions]}"
        )
