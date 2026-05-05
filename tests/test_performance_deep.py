"""Deep tests for naukri_server.tools.performance — impressions, recruiter_activity,
activity_level, widget headers, bucket parsing, metadata parsing.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier24_performance.py and tier22.py.
"""

import json

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

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
        await _get_search_impressions(days=30)

        mock_get.assert_awaited_once()
        extra = mock_get.call_args.kwargs.get("extra_headers", {})
        assert extra.get("appid") == "109"
        assert extra.get("systemid") == "109"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.get", new_callable=AsyncMock)
    async def test_days_param_passed(self, mock_get):
        """days is forwarded as a string param to api_get."""
        mock_get.return_value = {}
        from naukri_server.tools.performance import _get_search_impressions
        await _get_search_impressions(days=30)
        params = mock_get.call_args.kwargs.get("params", {})
        assert params.get("days") == "30"


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
    async def test_default_size_100(self, mock_post):
        """Default call sends size=100 in the POST body."""
        mock_post.return_value = {
            "successResponse": {"activityBucketCount": {}, "jobseekerActivityList": [], "count": 0}
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        await _get_recruiter_activity()
        body = mock_post.call_args.kwargs.get("body") or (mock_post.call_args.args[1] if len(mock_post.call_args.args) > 1 else {})
        assert body.get("size") == 100

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_custom_size_respected(self, mock_post):
        """Custom size=50 is forwarded."""
        mock_post.return_value = {
            "successResponse": {"activityBucketCount": {}, "jobseekerActivityList": [], "count": 0}
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        await _get_recruiter_activity(size=50)
        body = mock_post.call_args.kwargs.get("body") or (mock_post.call_args.args[1] if len(mock_post.call_args.args) > 1 else {})
        assert body.get("size") == 50

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
    async def test_has_more_computed(self, mock_post):
        """has_more is True when page*size < total."""
        mock_post.return_value = {
            "successResponse": {"count": 200, "jobseekerActivityList": [], "activityBucketCount": {}}
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity(page=1, size=100)
        assert result["has_more"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_all_95_activities_returned(self, mock_post):
        """All 95 activities from the response are returned."""
        activities_raw = [
            {"recruiterName": f"Recruiter {i}", "companyName": f"Company {i}",
             "activityType": "VIEWED", "activityDate": "2026-01-01"}
            for i in range(95)
        ]
        mock_post.return_value = {
            "successResponse": {"activityBucketCount": {}, "jobseekerActivityList": activities_raw, "count": 95}
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["status"] == "success"
        assert len(result["activities"]) == 95
        assert result["total_actions"] == 95


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
            "rmjStatus": True,
            "updatedStatus": False,
        }
        from naukri_server.tools.performance import _get_activity_level
        result = await _get_activity_level()

        assert result["status"] == "success"
        assert result["level"] == "HIGH"
        assert result["logged_in"] is True
        assert result["resume_updated"] is True
        assert result["profile_updated"] is False

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.get", new_callable=AsyncMock)
    async def test_widget_headers_passed(self, mock_get):
        """WIDGET_HEADERS must be passed for activity_level endpoint."""
        mock_get.return_value = {}
        from naukri_server.tools.performance import _get_activity_level
        await _get_activity_level()

        extra = mock_get.call_args.kwargs.get("extra_headers", {})
        assert extra.get("appid") == "109"
        assert extra.get("systemid") == "109"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.get", new_callable=AsyncMock)
    async def test_missing_level_defaults(self, mock_get):
        """Defaults level to 'UNKNOWN' when missing from response."""
        mock_get.return_value = {}
        from naukri_server.tools.performance import _get_activity_level
        result = await _get_activity_level()
        assert result["status"] == "success"
        assert result["level"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# 4. Performance atomic tools — routing + validation
# ---------------------------------------------------------------------------

class TestPerformanceAtomic:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_search_impressions", new_callable=AsyncMock)
    async def test_search_impressions_routes(self, mock_impressions):
        mock_impressions.return_value = {"status": "success"}
        from naukri_server.tools.performance import naukri_search_impressions
        result = await naukri_search_impressions(days=7)
        assert result["status"] == "success"
        mock_impressions.assert_awaited_once_with(days=7)

    @pytest.mark.asyncio
    async def test_search_impressions_invalid_days(self):
        from naukri_server.tools.performance import naukri_search_impressions
        result = await naukri_search_impressions(days=14)
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_recruiter_activity", new_callable=AsyncMock)
    async def test_recruiter_activity_routes(self, mock_ra):
        mock_ra.return_value = {"status": "success"}
        from naukri_server.tools.performance import naukri_recruiter_activity
        result = await naukri_recruiter_activity(page=1, limit=50)
        assert result["status"] == "success"
        mock_ra.assert_awaited_once_with(page=1, size=50, filter_by=None)

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_activity_level", new_callable=AsyncMock)
    async def test_activity_level_routes(self, mock_al):
        mock_al.return_value = {"status": "success", "level": "MEDIUM"}
        from naukri_server.tools.performance import naukri_activity_level
        result = await naukri_activity_level()
        assert result["status"] == "success"
        mock_al.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_search_impressions", new_callable=AsyncMock)
    async def test_search_impressions_api_error_handled(self, mock_impressions):
        mock_impressions.side_effect = NaukriAPIError(500, "Internal Server Error")
        from naukri_server.tools.performance import naukri_search_impressions
        result = await naukri_search_impressions(days=7)
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 500

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_recruiter_activity", new_callable=AsyncMock)
    async def test_recruiter_activity_exception_handled(self, mock_ra):
        mock_ra.side_effect = RuntimeError("network down")
        from naukri_server.tools.performance import naukri_recruiter_activity
        result = await naukri_recruiter_activity()
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance._get_activity_level", new_callable=AsyncMock)
    async def test_activity_level_exception_handled(self, mock_al):
        mock_al.side_effect = NaukriAPIError(401, "Unauthorized")
        from naukri_server.tools.performance import naukri_activity_level
        result = await naukri_activity_level()
        assert result["status"] == "error"
        assert result["http_status"] == 401


# ---------------------------------------------------------------------------
# 5. Recruiter Activity — bucket parsing edge cases
# ---------------------------------------------------------------------------

class TestRecruiterActivityBuckets:
    """Bucket parsing in _get_recruiter_activity."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_bucket_has_label_and_is_new(self, mock_post):
        """Bucket parsing includes label and is_new fields."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {
                    "VIEWED": {"count": 10, "percentageChange": 5, "label": "Profile Views", "isNew": 1},
                    "DOWNLOADED": {"count": 3, "percentageChange": -2, "label": "Resume Downloads", "isNew": 0},
                },
                "jobseekerActivityList": [],
                "count": 0,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
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


# ---------------------------------------------------------------------------
# 6. Recruiter Activity — item parsing edge cases
# ---------------------------------------------------------------------------

class TestRecruiterActivityItems:
    """Per-activity item parsing in _get_recruiter_activity."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_activity_has_company_master_name_and_is_new(self, mock_post):
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
    async def test_malformed_metadata_does_not_crash(self, mock_post):
        """Malformed metaData JSON doesn't crash."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [{"metaData": "not valid json {{{"}],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["activities"][0]["meta_job_id"] is None

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_empty_metadata_leaves_meta_job_id_none(self, mock_post):
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [{"metaData": ""}],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["activities"][0]["meta_job_id"] is None

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_missing_metadata_key_leaves_meta_job_id_none(self, mock_post):
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [{"recruiterName": "Bob"}],
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
                "jobseekerActivityList": [{"metaData": {"jobId": "77777"}}],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["activities"][0]["meta_job_id"] == "77777"


class TestRecruiterActivityNonDictItemSkipped:
    """Non-dict items in jobseekerActivityList are skipped."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_non_dict_activity_item_skipped(self, mock_post):
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": ["not_a_dict", 42, None, {"recruiterName": "Valid Item"}],
                "count": 1,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert len(result["activities"]) == 1
        assert result["activities"][0]["recruiter_name"] == "Valid Item"


# ---------------------------------------------------------------------------
# 7. Recruiter Activity — extra fields (tier 25)
# ---------------------------------------------------------------------------

class TestRecruiterActivityExtraFields:
    """Verify new recruiter fields (domain_expertise, last_active, etc.)."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_extra_fields_present(self, mock_post):
        mock_post.return_value = {
            "successResponse": {
                "count": 1,
                "jobseekerActivityList": [
                    {
                        "recruiterName": "Alice",
                        "companyName": "BigCo",
                        "activityType": "DOWNLOADED",
                        "activityDate": "2026-03-12",
                        "domainExpertise": "node.js, react, aws",
                        "lastActiveDate": "12 Mar 2026",
                        "isInternational": "1",
                        "followerCount": "150",
                        "isMsgSent": 1,
                        "userFollowing": 0,
                    }
                ],
                "activityBucketCount": {},
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        act = result["activities"][0]
        assert act["domain_expertise"] == "node.js, react, aws"
        assert act["last_active_date"] == "12 Mar 2026"
        assert act["is_international"] is True
        assert act["follower_count"] == 150
        assert act["msg_sent"] is True
        assert act["user_following"] is False

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_extra_fields_missing_graceful(self, mock_post):
        """Missing extra fields default to empty/False/0."""
        mock_post.return_value = {
            "successResponse": {
                "count": 1,
                "jobseekerActivityList": [
                    {"recruiterName": "Bob", "companyName": "SmallCo", "activityType": "VIEWED", "activityDate": "2026-03-10"}
                ],
                "activityBucketCount": {},
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        act = result["activities"][0]
        assert act["domain_expertise"] == ""
        assert act["last_active_date"] == ""
        assert act["is_international"] is False
        assert act["follower_count"] == 0
        assert act["msg_sent"] is False
        assert act["user_following"] is False

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock)
    async def test_international_string_zero(self, mock_post):
        """isInternational='0' maps to False."""
        mock_post.return_value = {
            "successResponse": {
                "count": 1,
                "jobseekerActivityList": [
                    {"recruiterName": "Carol", "companyName": "LocalCo", "activityType": "CONTACTED",
                     "activityDate": "2026-03-12", "isInternational": "0", "followerCount": "0"}
                ],
                "activityBucketCount": {},
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        act = result["activities"][0]
        assert act["is_international"] is False
        assert act["follower_count"] == 0


# ---------------------------------------------------------------------------
# 8. Helper validation — page clamping
# ---------------------------------------------------------------------------

class TestPerformanceHelperValidation:
    @pytest.mark.asyncio
    async def test_recruiter_activity_invalid_filter(self):
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity(filter_by="INVALID_FILTER")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_recruiter_activity_page_clamped(self):
        """page=0 is silently clamped to 1."""
        from naukri_server.tools.performance import _get_recruiter_activity
        with patch("naukri_server.tools.performance.api_client.post", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"successResponse": {"jobseekerActivityList": [], "activityBucketCount": {}, "count": 0}}
            result = await _get_recruiter_activity(page=0)
            mock_api.assert_awaited()
            assert result["status"] == "success"
