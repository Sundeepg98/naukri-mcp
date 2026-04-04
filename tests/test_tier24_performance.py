"""Tests for Tier 24: performance module — impressions, recruiter_activity, activity_level.

Every test is PURE: no network, no browser, no file I/O.
"""

import json

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


# =====================================================================
# From test_consolidation.py — performance action routing & validation
# =====================================================================

class TestPerformanceConsolidation:
    """Tests for naukri_server.tools.performance.naukri_performance."""

    @pytest.mark.asyncio
    async def test_invalid_metric(self):
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="invalid")
        assert result["status"] == "error"
        assert "Unknown metric" in result["message"]

    @pytest.mark.asyncio
    async def test_impressions_invalid_days(self):
        from naukri_server.tools.performance import naukri_performance
        result = await naukri_performance(metric="impressions", days=15)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "7, 30, 90" in result["message"]

    @pytest.mark.asyncio
    async def test_impressions_valid_days_routes(self):
        from naukri_server.tools.performance import naukri_performance
        with patch("naukri_server.tools.performance._get_search_impressions", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "days": 30}
            result = await naukri_performance(metric="impressions", days=30)
            mock_helper.assert_awaited_once_with(days=30)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_recruiter_activity_routes(self):
        from naukri_server.tools.performance import naukri_performance
        with patch("naukri_server.tools.performance._get_recruiter_activity", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "activities": []}
            result = await naukri_performance(metric="recruiter_activity", page=2, limit=10, filter_by="VIEWED")
            mock_helper.assert_awaited_once_with(page=2, size=10, filter_by="VIEWED")

    @pytest.mark.asyncio
    async def test_activity_level_routes(self):
        from naukri_server.tools.performance import naukri_performance
        with patch("naukri_server.tools.performance._get_activity_level", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "level": "HIGH"}
            result = await naukri_performance(metric="activity_level")
            mock_helper.assert_awaited_once()


# =====================================================================
# From test_consolidation.py — helper-level validation (performance)
# =====================================================================

class TestPerformanceHelperValidation:
    """Validation inside performance helpers."""

    @pytest.mark.asyncio
    async def test_recruiter_activity_invalid_filter(self):
        """_get_recruiter_activity rejects invalid filter_by values."""
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity(filter_by="INVALID_FILTER")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "INVALID_FILTER" in result["message"]

    @pytest.mark.asyncio
    async def test_recruiter_activity_page_clamped(self):
        """page=0 is silently clamped to 1 by validate_page."""
        from naukri_server.tools.performance import _get_recruiter_activity
        with patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"successResponse": {"jobseekerActivityList": [], "activityBucketCount": {}, "count": 0}}
            result = await _get_recruiter_activity(page=0)
            mock_api.assert_awaited()
            assert result["status"] == "success"


# =====================================================================
# From test_tier21.py — recruiter activity bucket & item parsing
# =====================================================================

class TestRecruiterActivityBuckets:
    """Tests for bucket parsing in _get_recruiter_activity."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_bucket_has_label_and_is_new(self, mock_post):
        """Bucket parsing includes label and is_new fields."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {
                    "VIEWED": {
                        "count": 10,
                        "percentageChange": 5,
                        "label": "Profile Views",
                        "isNew": 1,
                    },
                    "DOWNLOADED": {
                        "count": 3,
                        "percentageChange": -2,
                        "label": "Resume Downloads",
                        "isNew": 0,
                    },
                },
                "jobseekerActivityList": [],
                "count": 0,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["status"] == "success"
        viewed = result["buckets"]["VIEWED"]
        assert viewed["label"] == "Profile Views"
        assert viewed["is_new"] is True
        assert viewed["count"] == 10
        downloaded = result["buckets"]["DOWNLOADED"]
        assert downloaded["label"] == "Resume Downloads"
        assert downloaded["is_new"] is False

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_bucket_plain_int_fallback(self, mock_post):
        """Bucket with plain int value (not dict) falls back to count-only."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {"CONTACTED": 7},
                "jobseekerActivityList": [],
                "count": 0,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["buckets"]["CONTACTED"] == {"count": 7}


class TestRecruiterActivityItems:
    """Tests for per-activity item parsing in _get_recruiter_activity."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_activity_has_company_master_name_and_is_new(self, mock_post):
        """Per-activity items have company_master_name, is_new, activity_map, meta_job_id."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [
                    {
                        "recruiterName": "Jane Doe",
                        "companyName": "Acme Corp",
                        "activityType": "VIEWED",
                        "activityDate": "2025-12-01",
                        "companyMasterName": "Acme Corporation Pvt Ltd",
                        "isNew": 1,
                        "activityMap": {"VIEWED": 2, "DOWNLOADED": 1},
                        "metaData": json.dumps({"jobId": "99887766"}),
                    }
                ],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        act = result["activities"][0]
        assert act["company_master_name"] == "Acme Corporation Pvt Ltd"
        assert act["is_new"] is True
        assert act["activity_map"] == {"VIEWED": 2, "DOWNLOADED": 1}
        assert act["meta_job_id"] == "99887766"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_metadata_json_parsing_extracts_jobid(self, mock_post):
        """metaData JSON string parsing extracts jobId."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [
                    {
                        "metaData": '{"jobId": "12345", "source": "search"}',
                    }
                ],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["activities"][0]["meta_job_id"] == "12345"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_malformed_metadata_does_not_crash(self, mock_post):
        """Malformed metaData JSON doesn't crash — returns None for meta_job_id."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [
                    {"metaData": "not valid json {{{"},
                ],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["activities"][0]["meta_job_id"] is None

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_empty_metadata_leaves_meta_job_id_none(self, mock_post):
        """Empty metaData leaves meta_job_id as None."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [
                    {"metaData": ""},
                ],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["activities"][0]["meta_job_id"] is None

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_missing_metadata_key_leaves_meta_job_id_none(self, mock_post):
        """Activity with no metaData key at all leaves meta_job_id as None."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [
                    {"recruiterName": "Bob"},
                ],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["activities"][0]["meta_job_id"] is None

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_metadata_dict_instead_of_string(self, mock_post):
        """metaData that is already a dict (not JSON string) is handled."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [
                    {"metaData": {"jobId": "77777"}},
                ],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["activities"][0]["meta_job_id"] == "77777"


class TestRecruiterActivityNonDictItemSkipped:
    """Ensure non-dict items in jobseekerActivityList are skipped."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_non_dict_activity_item_skipped(self, mock_post):
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [
                    "not_a_dict",
                    42,
                    None,
                    {"recruiterName": "Valid Item"},
                ],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert len(result["activities"]) == 1
        assert result["activities"][0]["recruiter_name"] == "Valid Item"
