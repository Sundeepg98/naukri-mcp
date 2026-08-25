"""Deep tests for naukri_server.tools.notifications — fetch, filter, mark read,
unified notify, action routing, mark_all_read cap.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier24_notifications.py, tier22.py, tier22_misc_edge.py.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from naukri_server.api import NaukriAPIError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_notif(nid="N1", title="Test", message="msg", ntype="JA",
                date="2026-01-01", read=False, url="https://x.com"):
    return {
        "id": nid,
        "displayTitle": title,
        "message": message,
        "type": ntype,
        "createdAt": date,
        "readStatus": 1 if read else 0,
        "url": url,
        "metadata": {"key": "val"},
    }


# ---------------------------------------------------------------------------
# 1. _fetch_notifications — list-type response
# ---------------------------------------------------------------------------

class TestFetchNotificationsListType:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_list_type_response(self, mock_get):
        mock_get.return_value = [_make_notif("N1", ntype="JA"), _make_notif("N2", ntype="RA")]
        from naukri_server.tools.notifications import _fetch_notifications
        result = await _fetch_notifications(limit=20, page=1)
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["notifications"][0]["id"] == "N1"
        assert result["notifications"][0]["is_read"] is False

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_dict_type_response_notifications_key(self, mock_get):
        mock_get.return_value = {"totalCount": 5, "notifications": [_make_notif("N3", ntype="SYSTEM")]}
        from naukri_server.tools.notifications import _fetch_notifications
        result = await _fetch_notifications()
        assert result["status"] == "success"
        assert result["total"] == 5
        assert result["count"] == 1
        assert result["notifications"][0]["type"] == "SYSTEM"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_dict_type_response_feed_key(self, mock_get):
        mock_get.return_value = {"feed": [_make_notif("N4", ntype="APPLICATION_UPDATE")]}
        from naukri_server.tools.notifications import _fetch_notifications
        result = await _fetch_notifications()
        assert result["count"] == 1
        assert result["notifications"][0]["id"] == "N4"


# ---------------------------------------------------------------------------
# 2. _fetch_notifications — notif_type filter
# ---------------------------------------------------------------------------

class TestFetchNotificationsTypeFilter:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_notif_type_filter_case_insensitive(self, mock_get):
        mock_get.return_value = [
            _make_notif("N1", ntype="JA"),
            _make_notif("N2", ntype="RA"),
            _make_notif("N3", ntype="SYSTEM"),
        ]
        from naukri_server.tools.notifications import _fetch_notifications
        result = await _fetch_notifications(notif_type="ja")
        assert result["count"] == 1
        assert result["notifications"][0]["id"] == "N1"
        assert result.get("filtered_by") == "ja"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_no_filter_returns_all(self, mock_get):
        mock_get.return_value = [_make_notif("N1", ntype="JA"), _make_notif("N2", ntype="RA")]
        from naukri_server.tools.notifications import _fetch_notifications
        result = await _fetch_notifications()
        assert result["count"] == 2
        assert "filtered_by" not in result


# ---------------------------------------------------------------------------
# 3. _mark_single_read
# ---------------------------------------------------------------------------

class TestMarkSingleRead:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.post", new_callable=AsyncMock)
    async def test_mark_single_read_happy_path(self, mock_post):
        mock_post.return_value = {"status": "ok"}
        from naukri_server.tools.notifications import _mark_single_read
        result = await _mark_single_read("N42", "2026-03-01")
        assert result["status"] == "success"
        assert result["notification_id"] == "N42"
        mock_post.assert_awaited_once()
        call_body = mock_post.call_args.kwargs.get("body") or mock_post.call_args[1].get("body") or mock_post.call_args[0][1]
        assert call_body["notificationId"] == "N42"
        assert call_body["createdAt"] == "2026-03-01"


# ---------------------------------------------------------------------------
# 4. _get_unified_notify — parsing all category keys
# ---------------------------------------------------------------------------

class TestGetUnifiedNotify:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_all_category_keys_parsed(self, mock_get):
        mock_get.return_value = {
            "recoJobs": {"noti_count": 3, "has_new": True},
            "appStatus": {"total_count": 2},
            "criticalActions": {"count": 1},
            "rmj": {"noti_count": 0},
            "FF": {"noti_count": 1},
            "NL": {"count": 0},
            "RR": {"noti_count": 5},
            "recruiterSearch": {"noti_count": 7},
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["status"] == "success"
        assert "recoJobs" in result["categories"]
        assert result["categories"]["recoJobs"]["count"] == 3
        assert "recruiterSearch" in result["categories"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_missing_categories_excluded(self, mock_get):
        mock_get.return_value = {"recoJobs": {"noti_count": 2}}
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert "recoJobs" in result["categories"]
        assert "appStatus" not in result["categories"]
        assert result["total_types"] == 1

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_all_8_categories_when_present(self, mock_get):
        """All 8 known category keys are parsed when present."""
        mock_get.return_value = {
            "recoJobs": {"noti_count": 10},
            "appStatus": {"noti_count": 3},
            "criticalActions": {"noti_count": 1},
            "rmj": {"noti_count": 5},
            "FF": {"noti_count": 2},
            "NL": {"noti_count": 7},
            "RR": {"noti_count": 4},
            "recruiterSearch": {"noti_count": 8},
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["total_types"] == 8
        for key in ("recoJobs", "appStatus", "criticalActions", "rmj", "FF", "NL", "RR", "recruiterSearch"):
            assert key in result["categories"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_empty_categories_skipped(self, mock_get):
        mock_get.return_value = {
            "recoJobs": {"noti_count": 5},
            "appStatus": {"noti_count": 2},
            "criticalActions": {},
            "rmj": {},
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["total_types"] == 2
        assert "criticalActions" not in result["categories"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_count_extraction(self, mock_get):
        mock_get.return_value = {"recoJobs": {"noti_count": 1368}}
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["categories"]["recoJobs"]["count"] == 1368


# ---------------------------------------------------------------------------
# 5. Unified notify — edge cases (count field priority, fallbacks)
# ---------------------------------------------------------------------------

class TestUnifiedNotifyEdgeCases:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_latest_field_preserved(self, mock_get):
        mock_get.return_value = {
            "recoJobs": {"noti_count": 5, "status": "Resume Viewed", "noti_description": "SDE at Google"}
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["categories"]["recoJobs"]["latest_status"] == "Resume Viewed"
        assert result["categories"]["recoJobs"]["latest_description"] == "SDE at Google"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_total_count_fallback(self, mock_get):
        """No noti_count — must fall through to total_count."""
        mock_get.return_value = {"appStatus": {"total_count": 42}}
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["categories"]["appStatus"]["count"] == 42

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_bare_count_fallback(self, mock_get):
        """No noti_count, no total_count — must fall through to count."""
        mock_get.return_value = {"rmj": {"count": 7}}
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["categories"]["rmj"]["count"] == 7


# ---------------------------------------------------------------------------
# 6. Unified notify enrichment (tier 25 — full response)
# ---------------------------------------------------------------------------

class TestUnifiedNotifyEnrichment:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_full_response_parsing(self, mock_get):
        mock_get.return_value = {
            "newCount": 4,
            "totalCount": 2,
            "status": {
                "appStatus": {
                    "label": "Application Status", "type": "appStatus",
                    "total_count": 28, "noti_count": 3,
                    "noti_description": "Node Js Developer",
                    "status": "Resume Viewed", "showOnGnb": True,
                },
                "rmj": {"label": "Job invites", "type": "rmj", "total_count": 46, "noti_count": 1, "showOnGnb": True},
                "recruiterSearch": {"label": "Recruiter Searches", "type": "recruiterSearch", "noti_count": 1368, "showOnGnb": True},
                "FF": {"label": "Promotional Offer", "type": "FF", "noti_description": "FASTJOB20 20% off", "noti_count": 1, "freq": 0, "showOnGnb": True},
            },
            "order": ["appStatus", "rmj", "FF", "recruiterSearch"],
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["status"] == "success"
        assert result["new_count"] == 4
        assert result["display_order"] == ["appStatus", "rmj", "FF", "recruiterSearch"]
        app = result["categories"]["appStatus"]
        assert app["label"] == "Application Status"
        assert app["latest_status"] == "Resume Viewed"
        assert app["latest_description"] == "Node Js Developer"
        rs = result["categories"]["recruiterSearch"]
        assert rs["count"] == 1368

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_empty_categories_in_status(self, mock_get):
        mock_get.return_value = {
            "newCount": 0, "totalCount": 0,
            "status": {"recoJobs": {}, "NL": None},
            "order": ["recoJobs", "NL"],
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert len(result["categories"]) == 0

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_default_order_when_missing(self, mock_get):
        mock_get.return_value = {
            "appStatus": {"label": "Application Status", "type": "appStatus", "noti_count": 5, "showOnGnb": True}
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert "display_order" in result
        assert len(result["display_order"]) == 8


# ---------------------------------------------------------------------------
# 7. naukri_notifications action routing
# ---------------------------------------------------------------------------

class TestNaukriNotificationsAtomic:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications._fetch_notifications", new_callable=AsyncMock)
    async def test_list_atomic(self, mock_fetch):
        mock_fetch.return_value = {"status": "success", "count": 0, "notifications": []}
        from naukri_server.tools.notifications import naukri_list_notifications
        result = await naukri_list_notifications()
        assert result["status"] == "success"
        mock_fetch.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_count_atomic(self, mock_get):
        mock_get.return_value = {"count": 12}
        from naukri_server.tools.notifications import naukri_notification_count
        result = await naukri_notification_count()
        assert result["status"] == "success"
        assert result["count"] == 12

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications._mark_single_read", new_callable=AsyncMock)
    async def test_mark_read_atomic(self, mock_mark):
        mock_mark.return_value = {"status": "success", "notification_id": "N1"}
        from naukri_server.tools.notifications import naukri_mark_notification_read
        result = await naukri_mark_notification_read(notification_id="N1", date="2026-01-01")
        assert result["status"] == "success"
        mock_mark.assert_awaited_once_with("N1", "2026-01-01")

    @pytest.mark.asyncio
    async def test_mark_read_without_id_validation_error(self):
        from naukri_server.tools.notifications import naukri_mark_notification_read
        result = await naukri_mark_notification_read()
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "notification_id" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications._get_unified_notify", new_callable=AsyncMock)
    async def test_summary_atomic(self, mock_unified):
        mock_unified.return_value = {"status": "success", "source": "unified_notify", "categories": {}, "total_types": 0}
        from naukri_server.tools.notifications import naukri_notification_summary
        result = await naukri_notification_summary()
        assert result["status"] == "success"
        assert result["source"] == "unified_notify"


# ---------------------------------------------------------------------------
# 8. mark_all_read — caps at MAX_MARK_ALL_ITERATIONS
# ---------------------------------------------------------------------------

class TestMarkAllRead:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.asyncio.sleep", new_callable=AsyncMock)
    @patch("naukri_server.tools.notifications._mark_single_read", new_callable=AsyncMock)
    @patch("naukri_server.tools.notifications._fetch_notifications", new_callable=AsyncMock)
    async def test_mark_all_read_caps_iterations(self, mock_fetch, mock_mark, mock_sleep):
        from naukri_server.config import MAX_MARK_ALL_ITERATIONS
        page_notifs = [{"id": f"N{i}", "is_read": False, "date": "2026-01-01"} for i in range(50)]
        mock_fetch.return_value = {"status": "success", "notifications": page_notifs}
        mock_mark.return_value = {"status": "success", "notification_id": "Nx"}
        from naukri_server.tools.notifications import naukri_mark_all_notifications_read
        result = await naukri_mark_all_notifications_read()
        assert result["status"] == "success"
        assert mock_fetch.call_count == MAX_MARK_ALL_ITERATIONS

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.asyncio.sleep", new_callable=AsyncMock)
    @patch("naukri_server.tools.notifications._mark_single_read", new_callable=AsyncMock)
    @patch("naukri_server.tools.notifications._fetch_notifications", new_callable=AsyncMock)
    async def test_mark_all_read_already_read(self, mock_fetch, mock_mark, mock_sleep):
        mock_fetch.return_value = {
            "status": "success",
            "notifications": [{"id": "N1", "is_read": True, "date": "2026-01-01"}],
        }
        from naukri_server.tools.notifications import naukri_mark_all_notifications_read
        result = await naukri_mark_all_notifications_read()
        assert result["status"] == "success"
        assert result["marked_count"] == 0
        assert result["already_read"] == 1
        mock_mark.assert_not_awaited()


# ---------------------------------------------------------------------------
# 9. NaukriAPIError handling
# ---------------------------------------------------------------------------

class TestNaukriAPIErrorHandling:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_client.get", new_callable=AsyncMock)
    async def test_count_api_error(self, mock_get):
        mock_get.side_effect = NaukriAPIError(401, "Unauthorized")
        from naukri_server.tools.notifications import naukri_notification_count
        result = await naukri_notification_count()
        assert result["status"] == "error"
        # 401/403 now classify as AUTH_ERROR, not API_ERROR. The same status
        # used to mean different things depending on which dispatch path a
        # tool used; a signed-out caller got no next step. Converted, not
        # deleted -- the case still matters, the expected code changed.
        assert result["error_code"] == "AUTH_ERROR"
        assert result["http_status"] == 401

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications._fetch_notifications", new_callable=AsyncMock)
    async def test_list_api_error(self, mock_fetch):
        mock_fetch.side_effect = NaukriAPIError(503, "Service Unavailable")
        from naukri_server.tools.notifications import naukri_list_notifications
        result = await naukri_list_notifications()
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
