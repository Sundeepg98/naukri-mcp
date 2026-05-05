"""Tests for inbox and performance MCP tools — _strip_html, response parsing, routing, validation.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. _strip_html (sync, no mocking needed)
# =====================================================================

class TestStripHtml:
    """Tests for naukri_server.tools.inbox._strip_html."""

    def test_strip_html_removes_tags(self):
        from naukri_server.tools.inbox import _strip_html
        assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_strip_html_collapses_whitespace(self):
        from naukri_server.tools.inbox import _strip_html
        assert _strip_html("<div>  Hello   world  </div>") == "Hello world"

    def test_strip_html_handles_empty(self):
        from naukri_server.tools.inbox import _strip_html
        assert _strip_html("") == ""

    def test_strip_html_plain_text_passthrough(self):
        from naukri_server.tools.inbox import _strip_html
        assert _strip_html("no html here") == "no html here"


# =====================================================================
# 2. _fetch_inbox response parsing
# =====================================================================

class TestFetchInbox:
    """Tests for naukri_server.tools.inbox._fetch_inbox."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_client.post", new_callable=AsyncMock)
    async def test_fetch_inbox_parses_messages(self, mock_post):
        mock_post.return_value = {
            "successResponse": {
                "inbox": [
                    {
                        "messageId": "m1",
                        "subject": "Job offer",
                        "sender": [{"name": "Recruiter A"}],
                        "isRead": False,
                        "dateTime": "2024-01-01",
                        "messageType": "nvite",
                    },
                    {
                        "messageId": "m2",
                        "subject": "Follow up",
                        "sender": "Direct Name",
                        "isRead": True,
                        "dateTime": "2024-01-02",
                    },
                ],
                "total": 2,
                "unread": 1,
                "totalPowerNvite": 0,
                "unreadPowerNvite": 0,
            }
        }
        from naukri_server.tools.inbox import _fetch_inbox
        result = await _fetch_inbox(limit=10)
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["messages"][0]["sender"] == "Recruiter A"
        assert result["messages"][1]["sender"] == "Direct Name"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_client.post", new_callable=AsyncMock)
    async def test_fetch_inbox_unread_only(self, mock_post):
        """unread_only=True should filter out read messages."""
        mock_post.return_value = {
            "successResponse": {
                "inbox": [
                    {"messageId": "m1", "isRead": False, "subject": "New"},
                    {"messageId": "m2", "isRead": True, "subject": "Old"},
                ],
                "total": 2,
                "unread": 1,
                "totalPowerNvite": 0,
                "unreadPowerNvite": 0,
            }
        }
        from naukri_server.tools.inbox import _fetch_inbox
        result = await _fetch_inbox(unread_only=True)
        assert result["count"] == 1  # Only unread message


# =====================================================================
# 3. naukri_inbox routing and validation
# =====================================================================

class TestNaukriInbox:
    """Tests for naukri_server.tools.inbox atomic tools."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_client.post", new_callable=AsyncMock)
    async def test_inbox_mark_interested(self, mock_post):
        """naukri_mark_interested with valid params should route to _mark_interested."""
        mock_post.return_value = {}
        from naukri_server.tools.inbox import naukri_mark_interested
        result = await naukri_mark_interested(mail_id="123", conversation_id="c1")
        assert result["status"] == "success"
        assert result["interested"] is True

    @pytest.mark.asyncio
    async def test_inbox_mark_interested_missing_params(self):
        """naukri_mark_interested without conversation_id should fail validation."""
        from naukri_server.tools.inbox import naukri_mark_interested
        result = await naukri_mark_interested(mail_id="123", conversation_id=None)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_inbox_accept_nvite_missing_job_id(self):
        """naukri_accept_nvite with empty nvite_job_id should fail validation."""
        from naukri_server.tools.inbox import naukri_accept_nvite
        result = await naukri_accept_nvite(nvite_job_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"


# =====================================================================
# 4. Performance tools
# =====================================================================

class TestPerformance:
    """Tests for naukri_server.tools.performance helpers and routing."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.get", new_callable=AsyncMock)
    async def test_get_search_impressions(self, mock_get):
        mock_get.return_value = {
            "totalSearchAppearances": 150,
            "recruiterActions": 25,
            "dayWiseSearchAppearance": 21,
            "percentageChange": 15.5,
            "searchAppearanceTimeline": {"2024-01-01": 10},
            "searchKeyWords": {"python": 30},
        }
        from naukri_server.tools.performance import _get_search_impressions
        result = await _get_search_impressions(days=7)
        assert result["status"] == "success"
        assert result["total_appearances"] == 150
        assert result["days"] == 7

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_get_recruiter_activity(self, mock_post):
        mock_post.return_value = {
            "successResponse": {
                "jobseekerActivityList": [
                    {
                        "recruiterName": "John",
                        "companyName": "Google",
                        "activityType": "VIEWED",
                        "activityDate": "2024-01-01",
                    },
                ],
                "activityBucketCount": {
                    "VIEWED": {"count": 10, "percentageChange": 5},
                },
                "count": 1,
                "percentageChange": 5,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["status"] == "success"
        assert len(result["activities"]) == 1
        assert result["activities"][0]["recruiter_name"] == "John"
        assert result["buckets"]["VIEWED"]["count"] == 10

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.get", new_callable=AsyncMock)
    async def test_get_activity_level(self, mock_get):
        mock_get.return_value = {
            "level": "HIGH",
            "loggedInStatus": True,
            "rmjStatus": True,
            "updatedStatus": False,
        }
        from naukri_server.tools.performance import _get_activity_level
        result = await _get_activity_level()
        assert result["status"] == "success"
        assert result["level"] == "HIGH"
        assert result["logged_in"] is True


# =====================================================================
# Inbox atomic tools — validation
# =====================================================================

class TestInboxAtomic:
    """Tests for naukri_server.tools.inbox atomic tools."""

    @pytest.mark.asyncio
    async def test_read_requires_all_ids(self):
        from naukri_server.tools.inbox import naukri_read_message
        result = await naukri_read_message(message_id="m1")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "vcard_id" in result["message"]

    @pytest.mark.asyncio
    async def test_read_missing_message_id(self):
        from naukri_server.tools.inbox import naukri_read_message
        result = await naukri_read_message(vcard_id="v1", unique_id="u1")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_mark_interested_requires_ids(self):
        from naukri_server.tools.inbox import naukri_mark_interested
        result = await naukri_mark_interested(mail_id="m1", conversation_id=None)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "conversation_id" in result["message"]

    @pytest.mark.asyncio
    async def test_accept_nvite_requires_job_id(self):
        from naukri_server.tools.inbox import naukri_accept_nvite
        result = await naukri_accept_nvite(nvite_job_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "nvite_job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_list_routes_to_helper(self):
        from naukri_server.tools.inbox import naukri_list_inbox
        with patch("naukri_server.tools.inbox._fetch_inbox", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "messages": []}
            result = await naukri_list_inbox(limit=10, page=2)
            mock_helper.assert_awaited_once_with(limit=10, unread_only=False, mail_type="", page=2)
            assert result["status"] == "success"


# =====================================================================
# From test_tier21.py — inbox REST API tests
# =====================================================================

class TestInboxRestApi:
    """Tests for REST GET first / POST fallback in _fetch_inbox."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_client.get", new_callable=AsyncMock)
    async def test_rest_get_tried_first(self, mock_get):
        """REST GET is tried first and used when successful."""
        mock_get.return_value = {
            "inbox": [
                {"messageId": "r1", "subject": "REST message", "isRead": False},
            ],
            "totalCount": 1,
            "unreadCount": 1,
            "totalPowerNvite": 0,
            "unreadPowerNvite": 0,
            "relevantCount": 3,
            "hasPowerNvites": True,
        }
        from naukri_server.tools.inbox import _fetch_inbox
        result = await _fetch_inbox(limit=10)
        assert result["status"] == "success"
        assert result["count"] == 1
        assert result["messages"][0]["subject"] == "REST message"
        mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_client.post", new_callable=AsyncMock)
    @patch("naukri_server.tools.inbox.api_client.get", new_callable=AsyncMock)
    async def test_fallback_to_post_when_get_fails(self, mock_get, mock_post):
        """Fallback to POST when GET raises an exception."""
        mock_get.side_effect = Exception("GET not supported")
        mock_post.return_value = {
            "successResponse": {
                "inbox": [
                    {"messageId": "p1", "subject": "POST message", "isRead": False},
                ],
                "total": 1,
                "unread": 1,
                "totalPowerNvite": 0,
                "unreadPowerNvite": 0,
            }
        }
        from naukri_server.tools.inbox import _fetch_inbox
        result = await _fetch_inbox(limit=10)
        assert result["status"] == "success"
        assert result["messages"][0]["subject"] == "POST message"
        mock_get.assert_awaited_once()
        mock_post.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_client.get", new_callable=AsyncMock)
    async def test_relevant_count_and_has_power_nvites(self, mock_get):
        """relevant_count and has_power_nvites are in the return."""
        mock_get.return_value = {
            "inbox": [],
            "totalCount": 50,
            "unreadCount": 10,
            "totalPowerNvite": 5,
            "unreadPowerNvite": 2,
            "relevantCount": 8,
            "hasPowerNvites": True,
        }
        from naukri_server.tools.inbox import _fetch_inbox
        result = await _fetch_inbox()
        assert result["relevant_count"] == 8
        assert result["has_power_nvites"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_client.get", new_callable=AsyncMock)
    async def test_total_count_unread_count_mapping(self, mock_get):
        """totalCount and unreadCount from REST response are mapped correctly."""
        mock_get.return_value = {
            "inbox": [],
            "totalCount": 100,
            "unreadCount": 25,
            "totalPowerNvite": 0,
            "unreadPowerNvite": 0,
        }
        from naukri_server.tools.inbox import _fetch_inbox
        result = await _fetch_inbox()
        assert result["total"] == 100
        assert result["unread"] == 25


class TestInboxRestApiFallbackDetails:
    """Additional tests for inbox REST to POST fallback details."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_client.post", new_callable=AsyncMock)
    @patch("naukri_server.tools.inbox.api_client.get", new_callable=AsyncMock)
    async def test_rest_get_params_passed(self, mock_get, mock_post):
        """REST GET is called with correct query params."""
        mock_get.return_value = {
            "inbox": [],
            "totalCount": 0,
            "unreadCount": 0,
        }
        from naukri_server.tools.inbox import _fetch_inbox
        await _fetch_inbox(limit=15, page=2, mail_type="powerNvite")
        call_kwargs = mock_get.call_args
        params = call_kwargs[1].get("params") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1].get("params")
        assert params["pageSize"] == "15"
        assert params["pageNo"] == "2"
        assert params["mailType"] == "powerNvite"
        # POST should not be called since GET succeeded
        mock_post.assert_not_awaited()
