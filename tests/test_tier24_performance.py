"""Tests for Tier 24: performance module — impressions, recruiter_activity, activity_level.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock, call

from naukri_server.api import NaukriAPIError


# ---------------------------------------------------------------------------
# 1. _get_search_impressions
# ---------------------------------------------------------------------------

class TestGetSearchImpressions:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.get", new_callable=AsyncMock)
    async def test_happy_path(self, mock_get):
        """Returns structured result with all expected keys."""
        mock_get.return_value = {
            "totalSearchAppearances": 150,
            "recruiterActions": 12,
            "dayWiseSearchAppearance": 21.4,
            "percentageChange": 5.2,
            "searchAppearanceTimeline": {"Mon": 20, "Tue": 30},
            "searchKeyWords": {"python": 10, "django": 5},
        }
        from naukri_server.tools.performance import _get_search_impressions
        result = await _get_search_impressions(days=7)

        assert result["status"] == "success"
        assert result["days"] == 7
        assert result["total_appearances"] == 150
        assert result["recruiter_actions"] == 12
        assert result["daily_average"] == 21.4
        assert result["percentage_change"] == 5.2
        assert result["timeline"] == {"Mon": 20, "Tue": 30}
        assert result["top_keywords"] == {"python": 10, "django": 5}

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.get", new_callable=AsyncMock)
    async def test_widget_headers_passed(self, mock_get):
        """WIDGET_HEADERS (appid:109) must be passed as extra_headers."""
        mock_get.return_value = {}
        from naukri_server.tools.performance import _get_search_impressions
        from naukri_server.config import WIDGET_HEADERS
        await _get_search_impressions(days=30)

        mock_get.assert_awaited_once()
        extra = mock_get.call_args.kwargs.get("extra_headers", {})
        assert extra.get("appid") == WIDGET_HEADERS["appid"]


# ---------------------------------------------------------------------------
# 2. _get_recruiter_activity
# ---------------------------------------------------------------------------

class TestGetRecruiterActivity:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_happy_path_with_activities(self, mock_post):
        """Returns activities list and bucket counts."""
        mock_post.return_value = {
            "successResponse": {
                "count": 2,
                "percentageChange": 10,
                "jobseekerActivityList": [
                    {
                        "recruiterName": "John",
                        "companyName": "TechCorp",
                        "activityType": "VIEWED",
                        "activityDate": "2026-03-01",
                        "designation": "HR Manager",
                        "city": "Bangalore",
                        "recruiterId": "R1",
                        "isNew": 1,
                        "previousActionCount": 2,
                        "companyMasterName": "TechCorp Ltd",
                        "activityMap": None,
                        "metaData": '{"jobId": "J99"}',
                    }
                ],
                "activityBucketCount": {
                    "VIEWED": {"count": 5, "percentageChange": 15, "label": "Profile Views", "isNew": 0},
                },
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity(page=1, size=100)

        assert result["status"] == "success"
        assert result["total_actions"] == 2
        assert len(result["activities"]) == 1
        act = result["activities"][0]
        assert act["recruiter_name"] == "John"
        assert act["action"] == "VIEWED"
        assert act["is_new"] is True
        assert act["meta_job_id"] == "J99"
        assert "VIEWED" in result["buckets"]
        assert result["buckets"]["VIEWED"]["count"] == 5

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_bucket_as_scalar(self, mock_post):
        """Bucket entries that are scalars (not dicts) are handled."""
        mock_post.return_value = {
            "successResponse": {
                "count": 0,
                "jobseekerActivityList": [],
                "activityBucketCount": {
                    "DOWNLOADED": 3,
                },
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["buckets"]["DOWNLOADED"]["count"] == 3

    @pytest.mark.asyncio
    async def test_filter_by_invalid(self):
        """Invalid filter_by returns VALIDATION_ERROR without calling API."""
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity(filter_by="INVALID")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Invalid filter_by" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_filter_by_valid_uppercase(self, mock_post):
        """Valid filter_by 'viewed' is uppercased and accepted."""
        mock_post.return_value = {"successResponse": {"count": 0, "jobseekerActivityList": [], "activityBucketCount": {}}}
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity(filter_by="viewed")
        assert result["status"] == "success"
        assert result["filter_by"] == "VIEWED"
        body = mock_post.call_args.kwargs.get("body") or mock_post.call_args[0][1]
        assert body.get("filterBy") == "VIEWED"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_metadata_json_parse(self, mock_post):
        """metaData JSON string is parsed to extract jobId."""
        mock_post.return_value = {
            "successResponse": {
                "count": 1,
                "jobseekerActivityList": [
                    {"activityType": "VIEWED", "metaData": '{"jobId": "J777"}'},
                ],
                "activityBucketCount": {},
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["activities"][0]["meta_job_id"] == "J777"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_metadata_parse_failure_nonfatal(self, mock_post):
        """Malformed metaData JSON does not crash — meta_job_id stays None."""
        mock_post.return_value = {
            "successResponse": {
                "count": 1,
                "jobseekerActivityList": [
                    {"activityType": "VIEWED", "metaData": "not-json"},
                ],
                "activityBucketCount": {},
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["status"] == "success"
        assert result["activities"][0]["meta_job_id"] is None

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_has_more_computed(self, mock_post):
        """has_more is True when page*size < total."""
        mock_post.return_value = {
            "successResponse": {
                "count": 200,
                "jobseekerActivityList": [],
                "activityBucketCount": {},
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity(page=1, size=100)
        assert result["has_more"] is True


# ---------------------------------------------------------------------------
# 3. _get_activity_level
# ---------------------------------------------------------------------------

class TestGetActivityLevel:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.get", new_callable=AsyncMock)
    async def test_happy_path(self, mock_get):
        mock_get.return_value = {
            "level": "HIGH",
            "loggedInStatus": True,
            "rmjStatus": False,
            "updatedStatus": True,
        }
        from naukri_server.tools.performance import _get_activity_level
        result = await _get_activity_level()

        assert result["status"] == "success"
        assert result["level"] == "HIGH"
        assert result["logged_in"] is True
        assert result["resume_updated"] is False
        assert result["profile_updated"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.get", new_callable=AsyncMock)
    async def test_widget_headers_passed(self, mock_get):
        """WIDGET_HEADERS must be passed for activity_level endpoint."""
        mock_get.return_value = {}
        from naukri_server.tools.performance import _get_activity_level
        from naukri_server.config import WIDGET_HEADERS
        await _get_activity_level()

        extra = mock_get.call_args.kwargs.get("extra_headers", {})
        assert extra.get("appid") == WIDGET_HEADERS["appid"]


# ---------------------------------------------------------------------------
# 4. naukri_performance — routing + validation
# ---------------------------------------------------------------------------

class TestNaukriPerformanceRouting:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_search_impressions", new_callable=AsyncMock)
    async def test_impressions_metric_routes(self, mock_impressions):
        mock_impressions.return_value = {"status": "success"}
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="impressions", days=7)
        assert result["status"] == "success"
        mock_impressions.assert_awaited_once_with(days=7)

    @pytest.mark.asyncio
    async def test_impressions_invalid_days(self):
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="impressions", days=14)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_recruiter_activity", new_callable=AsyncMock)
    async def test_recruiter_activity_metric_routes(self, mock_ra):
        mock_ra.return_value = {"status": "success"}
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="recruiter_activity", page=1, limit=50)
        assert result["status"] == "success"
        mock_ra.assert_awaited_once_with(page=1, size=50, filter_by=None)

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_activity_level", new_callable=AsyncMock)
    async def test_activity_level_metric_routes(self, mock_al):
        mock_al.return_value = {"status": "success", "level": "MEDIUM"}
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="activity_level")
        assert result["status"] == "success"
        mock_al.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_metric_validation_error(self):
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="foobar")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown metric" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_search_impressions", new_callable=AsyncMock)
    async def test_impressions_api_error_handled(self, mock_impressions):
        mock_impressions.side_effect = NaukriAPIError(500, "Internal Server Error")
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="impressions", days=7)
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 500

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_recruiter_activity", new_callable=AsyncMock)
    async def test_recruiter_activity_exception_handled(self, mock_ra):
        mock_ra.side_effect = RuntimeError("network down")
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="recruiter_activity")
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_activity_level", new_callable=AsyncMock)
    async def test_activity_level_exception_handled(self, mock_al):
        mock_al.side_effect = NaukriAPIError(401, "Unauthorized")
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="activity_level")
        assert result["status"] == "error"
        assert result["http_status"] == 401
