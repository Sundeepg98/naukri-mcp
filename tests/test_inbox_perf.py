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
    @patch("naukri_server.tools.inbox.api_post", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.inbox.api_post", new_callable=AsyncMock)
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
    """Tests for naukri_server.tools.inbox.naukri_inbox routing."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_post", new_callable=AsyncMock)
    async def test_inbox_mark_interested(self, mock_post):
        """mark_interested with valid params should route to _mark_interested."""
        mock_post.return_value = {}
        from naukri_server.tools.inbox import naukri_inbox
        result = await naukri_inbox(action="mark_interested", mail_id="123", conversation_id="c1")
        assert result["status"] == "success"
        assert result["interested"] is True

    @pytest.mark.asyncio
    async def test_inbox_mark_interested_missing_params(self):
        """mark_interested without mail_id/conversation_id should fail validation."""
        from naukri_server.tools.inbox import naukri_inbox
        result = await naukri_inbox(action="mark_interested")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_inbox_accept_nvite_missing_job_id(self):
        """accept_nvite without nvite_job_id should fail validation."""
        from naukri_server.tools.inbox import naukri_inbox
        result = await naukri_inbox(action="accept_nvite")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_inbox_unknown_action(self):
        """Unknown action should return error with descriptive message."""
        from naukri_server.tools.inbox import naukri_inbox
        result = await naukri_inbox(action="invalid")
        assert result["status"] == "error"
        assert "Unknown action" in result["message"]


# =====================================================================
# 4. Performance tools
# =====================================================================

class TestPerformance:
    """Tests for naukri_server.tools.performance helpers and routing."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_get", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.performance.api_get", new_callable=AsyncMock)
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

    @pytest.mark.asyncio
    async def test_performance_invalid_metric(self):
        """Unknown metric should return error with descriptive message."""
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="invalid")
        assert result["status"] == "error"
        assert "Unknown metric" in result["message"]
