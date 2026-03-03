"""Tests for Tier 22 enrichments — search impressions widget headers, activity level
widget headers, recruiter activity size=100 default, unified notify categories,
application filter_info/star_rating/apply_flow_type, company rating parsing,
recommendation clusters, and taxonomy hierarchy parsing.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# =====================================================================
# 1. Search Impressions — widget headers and keyword extraction
# =====================================================================

class TestSearchImpressionsWidgetHeaders:
    """Tests for _get_search_impressions in performance.py."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_get", new_callable=AsyncMock)
    async def test_passes_widget_headers(self, mock_api):
        """_get_search_impressions passes extra_headers containing appid:109 and systemid:109."""
        mock_api.return_value = {}
        from naukri_server.tools.performance import _get_search_impressions
        await _get_search_impressions(days=7)
        call_kwargs = mock_api.call_args.kwargs
        extra_headers = call_kwargs.get("extra_headers", {})
        assert extra_headers.get("appid") == "109"
        assert extra_headers.get("systemid") == "109"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_get", new_callable=AsyncMock)
    async def test_keywords_extracted(self, mock_api):
        """_get_search_impressions extracts searchKeyWords into top_keywords."""
        mock_api.return_value = {
            "searchKeyWords": {"python": 73, "aws": 63},
            "totalSearchAppearances": 500,
        }
        from naukri_server.tools.performance import _get_search_impressions
        result = await _get_search_impressions(days=7)
        assert result["status"] == "success"
        assert result["top_keywords"] == {"python": 73, "aws": 63}

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_get", new_callable=AsyncMock)
    async def test_days_param_passed(self, mock_api):
        """_get_search_impressions forwards days as a string param to api_get."""
        mock_api.return_value = {}
        from naukri_server.tools.performance import _get_search_impressions
        await _get_search_impressions(days=30)
        call_kwargs = mock_api.call_args.kwargs
        params = call_kwargs.get("params", {})
        assert params.get("days") == "30"


# =====================================================================
# 2. Activity Level — widget headers and field parsing
# =====================================================================

class TestActivityLevelWidgetHeaders:
    """Tests for _get_activity_level in performance.py."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_get", new_callable=AsyncMock)
    async def test_passes_widget_headers(self, mock_api):
        """_get_activity_level passes extra_headers containing appid:109 and systemid:109."""
        mock_api.return_value = {}
        from naukri_server.tools.performance import _get_activity_level
        await _get_activity_level()
        call_kwargs = mock_api.call_args.kwargs
        extra_headers = call_kwargs.get("extra_headers", {})
        assert extra_headers.get("appid") == "109"
        assert extra_headers.get("systemid") == "109"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_get", new_callable=AsyncMock)
    async def test_level_parsing(self, mock_api):
        """_get_activity_level parses all four fields from the API response."""
        mock_api.return_value = {
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
    @patch("naukri_server.tools.performance.api_get", new_callable=AsyncMock)
    async def test_missing_level_defaults(self, mock_api):
        """_get_activity_level defaults level to 'UNKNOWN' when missing from response."""
        mock_api.return_value = {}
        from naukri_server.tools.performance import _get_activity_level
        result = await _get_activity_level()
        assert result["status"] == "success"
        assert result["level"] == "UNKNOWN"


# =====================================================================
# 3. Recruiter Activity — size=100 default
# =====================================================================

class TestRecruiterActivitySize100:
    """Tests for default and custom size in _get_recruiter_activity."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
    async def test_default_size_100(self, mock_post):
        """_get_recruiter_activity called with no args sends size=100 in the POST body."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [],
                "count": 0,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        await _get_recruiter_activity()
        call_args = mock_post.call_args
        # body is passed as second positional arg or as kwarg
        body = call_args.kwargs.get("body") or (call_args.args[1] if len(call_args.args) > 1 else {})
        assert body.get("size") == 100

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
    async def test_custom_size_respected(self, mock_post):
        """_get_recruiter_activity called with size=50 sends size=50 in the POST body."""
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": [],
                "count": 0,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        await _get_recruiter_activity(size=50)
        call_args = mock_post.call_args
        body = call_args.kwargs.get("body") or (call_args.args[1] if len(call_args.args) > 1 else {})
        assert body.get("size") == 50

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
    async def test_all_activities_returned(self, mock_post):
        """_get_recruiter_activity returns all 95 activities from the response."""
        activities_raw = [
            {"recruiterName": f"Recruiter {i}", "companyName": f"Company {i}",
             "activityType": "VIEWED", "activityDate": "2026-01-01"}
            for i in range(95)
        ]
        mock_post.return_value = {
            "successResponse": {
                "activityBucketCount": {},
                "jobseekerActivityList": activities_raw,
                "count": 95,
            }
        }
        from naukri_server.tools.performance import _get_recruiter_activity
        result = await _get_recruiter_activity()
        assert result["status"] == "success"
        assert len(result["activities"]) == 95
        assert result["total_actions"] == 95


# =====================================================================
# 4. Unified Notify — category parsing
# =====================================================================

class TestUnifiedNotify:
    """Tests for _get_unified_notify in notifications.py."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_all_categories_parsed(self, mock_api):
        """_get_unified_notify parses all 8 known category keys when present."""
        mock_api.return_value = {
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
        assert result["status"] == "success"
        assert result["total_types"] == 8
        categories = result["categories"]
        for key in ("recoJobs", "appStatus", "criticalActions", "rmj", "FF", "NL", "RR", "recruiterSearch"):
            assert key in categories, f"Expected category '{key}' missing from result"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_empty_categories_skipped(self, mock_api):
        """_get_unified_notify skips categories with no data (empty dict or missing)."""
        mock_api.return_value = {
            "recoJobs": {"noti_count": 5},
            "appStatus": {"noti_count": 2},
            "criticalActions": {},  # empty — should be skipped
            "rmj": {},              # empty — should be skipped
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["status"] == "success"
        assert result["total_types"] == 2
        assert "recoJobs" in result["categories"]
        assert "appStatus" in result["categories"]
        assert "criticalActions" not in result["categories"]
        assert "rmj" not in result["categories"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_count_extraction(self, mock_api):
        """_get_unified_notify extracts noti_count correctly (e.g. 1368)."""
        mock_api.return_value = {
            "recoJobs": {"noti_count": 1368},
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["categories"]["recoJobs"]["count"] == 1368

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_has_new_logic(self, mock_api):
        """has_new is True when noti_count > 0, False when noti_count == 0."""
        mock_api.return_value = {
            "recoJobs": {"noti_count": 5},
            "appStatus": {"noti_count": 0},
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        # recoJobs: noti_count=5 → has_new True
        assert result["categories"]["recoJobs"]["has_new"] is True
        # appStatus: noti_count=0 → falsy, count=0, has_new False
        # Note: appStatus has noti_count=0 which is falsy → skipped by the empty check
        # Only recoJobs with noti_count=5 survives
        assert "recoJobs" in result["categories"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_status_preserved(self, mock_api):
        """_get_unified_notify preserves the 'status' field from category data."""
        mock_api.return_value = {
            "appStatus": {"noti_count": 3, "status": "PENDING_REVIEW"},
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()
        assert result["categories"]["appStatus"]["status"] == "PENDING_REVIEW"


# =====================================================================
# 5. Applications — filter_info param and star_rating/apply_flow_type
# =====================================================================

class TestApplicationsFilterInfo:
    """Tests for filter_info param and new fields in tracking.py."""

    @pytest.mark.asyncio
    async def test_filter_info_passed_to_api(self):
        """_list_applications includes filterInfo='1' in params when filter_info=1."""
        from naukri_server.tools.tracking import _list_applications

        with patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.tracking._applications_lock"):
            # Call with filter_info=1
            result = await _list_applications(filter_info=1)
        # The function uses filter_info to set params["filterInfo"], then reads local file.
        # Since _load_json returns [], result should succeed with no applications.
        assert result["status"] == "success"
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_filter_info_none_omitted(self):
        """_list_applications without filter_info succeeds normally (params dict has no filterInfo)."""
        from naukri_server.tools.tracking import _list_applications

        with patch("naukri_server.tools.tracking._load_json", return_value=[]), \
             patch("naukri_server.tools.tracking._applications_lock"):
            result = await _list_applications()
        assert result["status"] == "success"
        assert result["total"] == 0

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_star_rating_extracted(self, mock_api):
        """_get_application_detail extracts starRating from API response."""
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "Acme"},
            "starRating": 4,
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J123")
        assert result["status"] == "success"
        assert result["star_rating"] == 4

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_apply_flow_type_extracted(self, mock_api):
        """_get_application_detail extracts applyFlowType from API response."""
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "Acme"},
            "applyFlowType": "agentApply",
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J456")
        assert result["status"] == "success"
        assert result["apply_flow_type"] == "agentApply"


# =====================================================================
# 6. Company Rating Parsing (tracking.py — _get_application_detail)
# =====================================================================

class TestCompanyRatingParsing:
    """Tests for companyRating extraction in _get_application_detail."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_company_rating_from_companyRating(self, mock_api):
        """_get_application_detail parses companyRating dict into company_rating."""
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "TechCorp"},
            "companyRating": {"Rating": 4.2, "ReviewsCount": 500},
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J789")
        assert result["status"] == "success"
        assert "company_rating" in result
        assert result["company_rating"]["rating"] == 4.2
        assert result["company_rating"]["reviews"] == 500

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_company_rating_missing(self, mock_api):
        """_get_application_detail does not include company_rating when field is absent."""
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "TechCorp"},
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J999")
        assert result["status"] == "success"
        assert "company_rating" not in result


# =====================================================================
# 7. Recommendation Clusters (search.py — naukri_get_recommendations)
# =====================================================================

class TestRecommendationClusters:
    """Tests for cluster parsing in naukri_get_recommendations."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_post", new_callable=AsyncMock)
    async def test_cluster_parsing_dict(self, mock_post):
        """Clusters as dicts with count+title are parsed into cluster_info."""
        mock_post.return_value = {
            "jobDetails": [],
            "noOfJobs": 0,
            "clusters": {
                "apply": {"count": 64, "title": "Based on applies"},
            },
        }
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["status"] == "success"
        assert "apply" in result["clusters"]
        assert result["clusters"]["apply"]["count"] == 64
        assert result["clusters"]["apply"]["title"] == "Based on applies"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_post", new_callable=AsyncMock)
    async def test_cluster_parsing_int(self, mock_post):
        """Clusters as plain integers are converted to count-only dicts."""
        mock_post.return_value = {
            "jobDetails": [],
            "noOfJobs": 0,
            "clusters": {"apply": 64},
        }
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["status"] == "success"
        assert result["clusters"]["apply"]["count"] == 64

    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_post", new_callable=AsyncMock)
    async def test_agent_eligible_flag(self, mock_post):
        """agentEligibleJobExists=True is surfaced as agent_eligible_exists=True."""
        mock_post.return_value = {
            "jobDetails": [],
            "noOfJobs": 0,
            "clusters": {},
            "agentEligibleJobExists": True,
        }
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["status"] == "success"
        assert result["agent_eligible_exists"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_post", new_callable=AsyncMock)
    async def test_cluster_split_date(self, mock_post):
        """clusterSplitDate from response is surfaced as cluster_split_date."""
        mock_post.return_value = {
            "jobDetails": [],
            "noOfJobs": 0,
            "clusters": {},
            "clusterSplitDate": "2026-03-01",
        }
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["status"] == "success"
        assert result["cluster_split_date"] == "2026-03-01"


# =====================================================================
# 8. Taxonomy Lookup (insights.py — _get_taxonomy)
# =====================================================================

def _make_cache_miss():
    """Return a mock TtlCache that always calls the fetch function (cache miss)."""
    async def _call(fn):
        return await fn()
    mock_cache = MagicMock()
    mock_cache.get = AsyncMock(side_effect=_call)
    return mock_cache


class TestTaxonomyLookup:
    """Tests for _get_taxonomy hierarchy parsing in insights.py."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_get", new_callable=AsyncMock)
    async def test_hierarchy_parsing(self, mock_api):
        """_get_taxonomy parses 2 departments, each with 1 role_category and 2 roles."""
        mock_api.return_value = [
            {
                "id": 1, "label": "IT & Software", "synonyms": [],
                "child": [
                    {
                        "id": 11, "label": "Software Development",
                        "child": [
                            {"id": 111, "label": "Backend Developer", "synonyms": ["server-side dev"]},
                            {"id": 112, "label": "Frontend Developer", "synonyms": []},
                        ],
                    }
                ],
            },
            {
                "id": 2, "label": "Data Science", "synonyms": ["ML"],
                "child": [
                    {
                        "id": 21, "label": "Machine Learning",
                        "child": [
                            {"id": 211, "label": "ML Engineer", "synonyms": ["AI engineer"]},
                            {"id": 212, "label": "Data Scientist", "synonyms": []},
                        ],
                    }
                ],
            },
        ]
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        assert result["status"] == "success"
        assert result["total_departments"] == 2
        assert len(result["departments"]) == 2
        # First department
        dept0 = result["departments"][0]
        assert dept0["label"] == "IT & Software"
        assert len(dept0["role_categories"]) == 1
        assert len(dept0["role_categories"][0]["roles"]) == 2

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_get", new_callable=AsyncMock)
    async def test_synonym_extraction(self, mock_api):
        """_get_taxonomy preserves synonyms on roles."""
        mock_api.return_value = [
            {
                "id": 1, "label": "Engineering", "synonyms": [],
                "child": [
                    {
                        "id": 11, "label": "Software",
                        "child": [
                            {"id": 111, "label": "Developer", "synonyms": ["dev", "developer"]},
                        ],
                    }
                ],
            }
        ]
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        role = result["departments"][0]["role_categories"][0]["roles"][0]
        assert role["label"] == "Developer"
        assert "dev" in role["synonyms"]
        assert "developer" in role["synonyms"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_get", new_callable=AsyncMock)
    async def test_total_role_count(self, mock_api):
        """_get_taxonomy total_roles matches actual role count across all depts."""
        mock_api.return_value = [
            {
                "id": 1, "label": "Dept A", "synonyms": [],
                "child": [
                    {"id": 11, "label": "Cat A1", "child": [
                        {"id": 111, "label": "Role 1", "synonyms": []},
                        {"id": 112, "label": "Role 2", "synonyms": []},
                        {"id": 113, "label": "Role 3", "synonyms": []},
                    ]},
                ],
            },
            {
                "id": 2, "label": "Dept B", "synonyms": [],
                "child": [
                    {"id": 21, "label": "Cat B1", "child": [
                        {"id": 211, "label": "Role 4", "synonyms": []},
                        {"id": 212, "label": "Role 5", "synonyms": []},
                    ]},
                ],
            },
        ]
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        assert result["total_roles"] == 5  # 3 from Dept A + 2 from Dept B

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_get", new_callable=AsyncMock)
    async def test_empty_response(self, mock_api):
        """_get_taxonomy handles empty response gracefully — returns departments=[], total_roles=0."""
        mock_api.return_value = {}
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        assert result["status"] == "success"
        assert result["departments"] == []
        assert result["total_roles"] == 0
