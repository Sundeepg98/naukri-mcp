"""Tests for Tier 25: screening Q retry, recruiter extra fields, unified notify, AB REST bridge.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# 1. Screening Question Retry Bug Fix (apply.py)
# ---------------------------------------------------------------------------

class TestScreeningQuestionRetry:
    """Verify that needs_input jobs can be retried with answers."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_retry_needs_input_with_answers(self, mock_load, mock_apply):
        """When status=needs_input and answers provided, allow retry."""
        mock_load.return_value = [
            {"job_id": "110326018660", "status": "needs_input", "pending_questions": 2}
        ]
        mock_apply.return_value = {"status": "applied", "job_id": "110326018660"}

        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="110326018660", answers={"q1": "yes"})

        assert result["status"] == "applied"
        mock_apply.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_block_needs_input_without_answers(self, mock_load, mock_apply):
        """When status=needs_input but NO answers, still block."""
        mock_load.return_value = [
            {"job_id": "110326018660", "status": "needs_input"}
        ]

        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="110326018660")

        assert result["status"] == "already_applied"
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_block_already_applied_with_answers(self, mock_load, mock_apply):
        """When status=applied, block even with answers."""
        mock_load.return_value = [
            {"job_id": "110326018660", "status": "applied"}
        ]

        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="110326018660", answers={"q1": "yes"})

        assert result["status"] == "already_applied"
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_new_job_no_existing_record(self, mock_load, mock_apply):
        """When no existing record, proceed to apply."""
        mock_load.return_value = []
        mock_apply.return_value = {"status": "applied", "job_id": "990326000001"}

        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="990326000001")

        assert result["status"] == "applied"
        mock_apply.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.apply._apply_single", new_callable=AsyncMock)
    @patch("naukri_server.tools.apply._load_json")
    async def test_block_error_status_without_answers(self, mock_load, mock_apply):
        """When status=error but no answers, block."""
        mock_load.return_value = [
            {"job_id": "110326018660", "status": "error"}
        ]

        from naukri_server.tools.apply import naukri_apply
        result = await naukri_apply(job_id="110326018660")

        assert result["status"] == "already_applied"
        mock_apply.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Recruiter Activity Extra Fields (performance.py)
# ---------------------------------------------------------------------------

class TestRecruiterActivityExtraFields:
    """Verify new recruiter fields are extracted."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
    async def test_extra_fields_present(self, mock_post):
        """New fields (domain_expertise, last_active, etc.) are extracted."""
        mock_post.return_value = {
            "successResponse": {
                "count": 1,
                "jobseekerActivityList": [
                    {
                        "recruiterName": "Alice",
                        "companyName": "BigCo",
                        "activityType": "DOWNLOADED",
                        "activityDate": "2026-03-12",
                        "designation": "Tech Lead",
                        "city": "Mumbai",
                        "recruiterId": "R42",
                        "isNew": 1,
                        "previousActionCount": 0,
                        "companyMasterName": "BigCo Inc",
                        "activityMap": {"VIEWED": {"date": "2026-03-11"}},
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
    async def test_extra_fields_missing_graceful(self, mock_post):
        """Missing extra fields default to empty/False/0."""
        mock_post.return_value = {
            "successResponse": {
                "count": 1,
                "jobseekerActivityList": [
                    {
                        "recruiterName": "Bob",
                        "companyName": "SmallCo",
                        "activityType": "VIEWED",
                        "activityDate": "2026-03-10",
                    }
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
    async def test_international_string_zero(self, mock_post):
        """isInternational='0' maps to False."""
        mock_post.return_value = {
            "successResponse": {
                "count": 1,
                "jobseekerActivityList": [
                    {
                        "recruiterName": "Carol",
                        "companyName": "LocalCo",
                        "activityType": "CONTACTED",
                        "activityDate": "2026-03-12",
                        "isInternational": "0",
                        "followerCount": "0",
                    }
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
# 3. Unified Notify Enrichment (notifications.py)
# ---------------------------------------------------------------------------

class TestUnifiedNotifyEnrichment:
    """Verify enriched unified notify parsing."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_full_response_parsing(self, mock_get):
        """All category fields are extracted from full response."""
        mock_get.return_value = {
            "newCount": 4,
            "totalCount": 2,
            "status": {
                "appStatus": {
                    "label": "Application Status",
                    "type": "appStatus",
                    "total_count": 28,
                    "noti_count": 3,
                    "noti_description": "Node Js Developer",
                    "status": "Resume Viewed",
                    "showOnGnb": True,
                },
                "rmj": {
                    "label": "Job invites",
                    "type": "rmj",
                    "total_count": 46,
                    "noti_count": 1,
                    "showOnGnb": True,
                },
                "recruiterSearch": {
                    "label": "Recruiter Searches",
                    "type": "recruiterSearch",
                    "noti_count": 1368,
                    "showOnGnb": True,
                },
                "FF": {
                    "label": "Promotional Offer",
                    "type": "FF",
                    "noti_description": "FASTJOB20 20% off",
                    "noti_count": 1,
                    "freq": 0,
                    "showOnGnb": True,
                },
            },
            "order": ["appStatus", "rmj", "FF", "recruiterSearch"],
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()

        assert result["status"] == "success"
        assert result["new_count"] == 4
        assert result["total_count"] == 2
        assert result["display_order"] == ["appStatus", "rmj", "FF", "recruiterSearch"]

        app = result["categories"]["appStatus"]
        assert app["label"] == "Application Status"
        assert app["total_count"] == 28
        assert app["noti_count"] == 3
        assert app["latest_status"] == "Resume Viewed"
        assert app["latest_description"] == "Node Js Developer"
        assert app["has_new"] is True

        ff = result["categories"]["FF"]
        assert ff["latest_description"] == "FASTJOB20 20% off"
        assert ff["frequency_cap"] == 0

        rs = result["categories"]["recruiterSearch"]
        assert rs["count"] == 1368

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_empty_categories_skipped(self, mock_get):
        """Empty or non-dict categories are skipped."""
        mock_get.return_value = {
            "newCount": 0,
            "totalCount": 0,
            "status": {
                "recoJobs": {},
                "NL": None,
            },
            "order": ["recoJobs", "NL"],
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()

        assert len(result["categories"]) == 0
        assert result["total_types"] == 0

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_default_order_when_missing(self, mock_get):
        """Uses default order when response has no order field."""
        mock_get.return_value = {
            "appStatus": {
                "label": "Application Status",
                "type": "appStatus",
                "noti_count": 5,
                "showOnGnb": True,
            },
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()

        assert "display_order" in result
        assert len(result["display_order"]) == 8  # default 8 categories

    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_show_on_gnb_filtering(self, mock_get):
        """total_types only counts categories with showOnGnb=True."""
        mock_get.return_value = {
            "newCount": 1,
            "totalCount": 1,
            "status": {
                "appStatus": {
                    "label": "Apps",
                    "type": "appStatus",
                    "noti_count": 3,
                    "showOnGnb": True,
                },
                "criticalActions": {
                    "label": "Actions",
                    "type": "criticalActions",
                    "noti_count": 1,
                    "showOnGnb": False,
                },
            },
            "order": ["appStatus", "criticalActions"],
        }
        from naukri_server.tools.notifications import _get_unified_notify
        result = await _get_unified_notify()

        assert len(result["categories"]) == 2
        assert result["total_types"] == 1  # only appStatus has showOnGnb=True


# ---------------------------------------------------------------------------
# 4. AmbitionBox REST Bridge (ambitionbox.py)
# ---------------------------------------------------------------------------

class TestABRestBridge:
    """Tests for AmbitionBox REST bridge functions."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._ab_rest_get", new_callable=AsyncMock)
    async def test_get_benefits(self, mock_get):
        """ab_get_benefits returns structured benefits data."""
        mock_get.return_value = {
            "data": {
                "totalBenefits": 30,
                "benefits": [
                    {"name": "Health Insurance", "percentage": 92, "count": 4500},
                    {"name": "Cafeteria", "percentage": 78, "count": 3800},
                ],
            }
        }
        from naukri_server.tools.ambitionbox import ab_get_benefits
        result = await ab_get_benefits("41")

        assert result["status"] == "success"
        assert result["total_benefits"] == 30
        assert len(result["benefits"]) == 2
        assert result["benefits"][0]["name"] == "Health Insurance"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._ab_rest_get", new_callable=AsyncMock)
    async def test_get_work_culture(self, mock_get):
        """ab_get_work_culture returns timing, travel, work days, shifts."""
        mock_get.return_value = {
            "data": {
                "reviewsCount": 47802,
                "workMonitorLabels": ["Flexible timing", "Strict timing"],
                "workMonitorSeries": [73, 27],
                "workMonitorInsight": "73% report flexible timing",
                "travelTagsLabels": ["No travel", "Occasional"],
                "travelTagsSeries": [67, 33],
                "workDaysLabels": ["Mon-Fri", "Mon-Sat"],
                "workDaysSeries": [89, 11],
                "shiftsLabels": ["Day", "Night"],
                "shiftsSeries": [95, 5],
            }
        }
        from naukri_server.tools.ambitionbox import ab_get_work_culture
        result = await ab_get_work_culture("41")

        assert result["status"] == "success"
        assert result["reviews_count"] == 47802
        assert result["work_timing"]["labels"] == ["Flexible timing", "Strict timing"]
        assert result["work_timing"]["values"] == [73, 27]
        assert result["travel"]["labels"] == ["No travel", "Occasional"]
        assert result["work_days"]["values"] == [89, 11]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._ab_rest_get", new_callable=AsyncMock)
    async def test_get_interview_questions(self, mock_get):
        """ab_get_interview_questions returns questions list."""
        mock_get.return_value = {
            "data": [
                {"question": "What is Node.js?", "difficulty": "Easy"},
                {"question": "Explain event loop", "difficulty": "Medium"},
            ],
            "meta": {"count": 10, "currentPage": 1},
            "totalInterviewExperiencesLive": 8364,
        }
        from naukri_server.tools.ambitionbox import ab_get_interview_questions
        result = await ab_get_interview_questions("41", designation_id="1027")

        assert result["status"] == "success"
        assert result["total_interviews"] == 8364
        assert result["count"] == 10
        assert len(result["questions"]) == 2

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._ab_rest_get", new_callable=AsyncMock)
    async def test_get_competitors(self, mock_get):
        """ab_get_competitors returns competitor list."""
        mock_get.return_value = [
            {"CompanyId": 42, "CompanyName": "TCS", "Rating": 3.3, "ReviewCount": 112022},
            {"CompanyId": 43, "CompanyName": "Wipro", "Rating": 3.5, "ReviewCount": 50000},
        ]
        from naukri_server.tools.ambitionbox import ab_get_competitors
        result = await ab_get_competitors("41")

        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["competitors"][0]["CompanyName"] == "TCS"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._ab_rest_get", new_callable=AsyncMock)
    async def test_get_locations(self, mock_get):
        """ab_get_locations returns office locations."""
        mock_get.return_value = {
            "companyLocations": [
                {"city": "Bengaluru", "address": "Electronics City"},
                {"city": "Pune", "address": "Hinjewadi"},
            ]
        }
        from naukri_server.tools.ambitionbox import ab_get_locations
        result = await ab_get_locations("41")

        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["locations"][0]["city"] == "Bengaluru"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._ab_rest_get", new_callable=AsyncMock)
    async def test_get_applied_jobs_insights(self, mock_get):
        """ab_get_applied_jobs_insights returns salary insights for your applications."""
        mock_get.return_value = [
            {
                "companyName": "Meesho",
                "minCtc": 2500000,
                "maxCtc": 4000000,
                "jobProfileName": "Backend Developer",
                "timeLapse": "3 days ago",
            }
        ]
        from naukri_server.tools.ambitionbox import ab_get_applied_jobs_insights
        result = await ab_get_applied_jobs_insights()

        assert result["status"] == "success"
        assert result["count"] == 1
        assert result["insights"][0]["companyName"] == "Meesho"
        assert result["insights"][0]["minCtc"] == 2500000

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._ab_rest_get", new_callable=AsyncMock)
    async def test_get_salary_rest(self, mock_get):
        """ab_get_salary_rest returns detailed salary breakdown."""
        mock_get.return_value = {
            "data": {
                "profileInfo": {"name": "Backend Developer"},
                "summaryData": {"avgSalary": 1500000, "minSalary": 800000, "maxSalary": 2500000},
                "confidence": "VeryHigh",
                "experienceLevels": [{"level": "0-2 yrs", "avg": 600000}],
                "ctcComparison": {"marketAvg": 1400000},
                "takeHomeSalary": {"monthly": 95000},
            }
        }
        from naukri_server.tools.ambitionbox import ab_get_salary_rest
        result = await ab_get_salary_rest("41", "1027")

        assert result["status"] == "success"
        assert result["summary"]["avgSalary"] == 1500000
        assert result["confidence"] == "VeryHigh"
        assert len(result["experience_levels"]) == 1

    @pytest.mark.asyncio
    @patch("naukri_server.tools.ambitionbox._ab_rest_get", new_callable=AsyncMock)
    async def test_empty_response_handling(self, mock_get):
        """REST bridge handles empty responses gracefully."""
        mock_get.return_value = {}
        from naukri_server.tools.ambitionbox import ab_get_benefits
        result = await ab_get_benefits("999")

        assert result["status"] == "success"
        assert result["total_benefits"] == 0
        assert result["benefits"] == []


# ---------------------------------------------------------------------------
# 5. DFP Profile Targeting (profile.py action="targeting")
# ---------------------------------------------------------------------------

class TestDFPTargeting:
    """Verify DFP targeting action parses ad-system profile fields and identifies gaps."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_targeting_returns_profile_fields(self, mock_get):
        """Targeting action returns parsed profile fields."""
        mock_get.return_value = {
            "params": {
                "Profile-CTC": 17.0,
                "Profile-Experience": 5.0,
                "Profile-Age": 27,
                "Profile-Gender": "M",
                "Profile-Location": "Bengaluru",
                "Profile-Company": "Acme Technology",
                "Profile-Designation": "Software Engineer (Backend)",
                "Profile-KeySkills": "Node.js, TypeScript",
                "Profile-Institute": "Example Institute of Technology",
                "Profile-UG-Course": "B.Tech",
                "Profile-UG-spl": "CSE",
                "Profile-UG-yearpass": 2021,
                "Profile-UG-percent": 6.47,
                "Profile-Pref-Loc": "",
                "Profile-PG-Course": "",
                "Profile-Registerdays": 1510,
                "Profile-Activeness": 0,
            },
            "slots": [{"adUnitPath": "/test"}],
        }
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="targeting")
        assert result["status"] == "success"
        assert result["profile"]["ctc_lpa"] == 17.0
        assert result["profile"]["experience_years"] == 5.0
        assert result["profile"]["location"] == "Bengaluru"
        assert result["ad_slots"] == 1

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_targeting_identifies_gaps(self, mock_get):
        """Empty Profile-* fields are listed as completeness gaps."""
        mock_get.return_value = {
            "params": {
                "Profile-CTC": 17.0,
                "Profile-Pref-Loc": "",
                "Profile-PG-Course": "",
                "Profile-PG-Spl": "",
            },
            "slots": [],
        }
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="targeting")
        assert result["gap_count"] == 3
        assert "pref loc" in result["completeness_gaps"]
        assert "pg course" in result["completeness_gaps"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_targeting_empty_response(self, mock_get):
        """Empty DFP response returns zero fields."""
        mock_get.return_value = {}
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="targeting")
        assert result["status"] == "success"
        assert result["targeting_fields"] == 0
        assert result["gap_count"] == 0

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_targeting_api_error(self, mock_get):
        """API error returns error status."""
        from naukri_server.api import NaukriAPIError
        mock_get.side_effect = NaukriAPIError(401, "Unauthorized")
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="targeting")
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_targeting_zero_values_not_gaps(self, mock_get):
        """Fields with value 0 are NOT treated as gaps."""
        mock_get.return_value = {
            "params": {
                "Profile-Activeness": 0,
                "Profile-CTC": 0,
                "Profile-Pref-Loc": "",
            },
            "slots": [],
        }
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="targeting")
        # Only Pref-Loc is a gap (empty string), not Activeness/CTC (value 0)
        assert result["gap_count"] == 1
        assert "pref loc" in result["completeness_gaps"]


# ---------------------------------------------------------------------------
# Dashboard Selective Properties Optimization
# ---------------------------------------------------------------------------

class TestDashboardSelectiveProperties:
    """Verify dashboard API uses selective properties param to reduce response size."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_dashboard_passes_properties_param(self, mock_get):
        """_get_dashboard passes properties param to api_get."""
        mock_get.return_value = {"dashBoard": {"pc": 100, "ca": 0}}
        from naukri_server.tools.profile import _get_dashboard
        await _get_dashboard()
        # Verify properties param was passed
        call_args = mock_get.call_args
        assert call_args is not None
        params = call_args.kwargs.get("params") or (call_args.args[1] if len(call_args.args) > 1 else None)
        assert params is not None
        assert "properties" in params

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_dashboard_properties_contains_expected_keys(self, mock_get):
        """Properties string includes the validated property names."""
        mock_get.return_value = {"dashBoard": {}}
        from naukri_server.tools.profile import _get_dashboard
        await _get_dashboard()
        props = mock_get.call_args.kwargs.get("params", {}).get("properties", "")
        for key in ("userDetails", "profilePerformance", "isPaidUser", "photoInfo"):
            assert key in props, f"Missing property: {key}"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_dashboard_still_parses_with_selective_response(self, mock_get):
        """Selective response (fewer fields) still parses without errors."""
        mock_get.return_value = {"dashBoard": {"profileViewCount": 42}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["status"] == "success"
        assert result["profile_views"] == 42


# ---------------------------------------------------------------------------
# 7. CCS Profile Completion Prompts (insights.py insight_type="profile_prompts")
# ---------------------------------------------------------------------------

class TestProfilePrompts:
    """Verify CCS profile completion prompts feature."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights._get_profile_prompts", new_callable=AsyncMock)
    async def test_profile_prompts_dispatches(self, mock_prompts):
        """Dispatch routes profile_prompts to _get_profile_prompts."""
        mock_prompts.return_value = {"status": "success", "pending_count": 2}
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="profile_prompts")
        assert result["status"] == "success"
        assert result["pending_count"] == 2
        mock_prompts.assert_called_once()

    @pytest.mark.asyncio
    async def test_profile_prompts_pending_states(self):
        """State values of 0 are treated as pending prompts."""
        from naukri_server.tools.insights import _get_profile_prompts

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={
            "states": {
                "t2536_click_add_salary_breakup": 0,
                "t2906_click_add_locality": 0,
                "t2772_click_complete": 0,
                "t4170_click_submit": 1,
                "t2683_view": 0,
                "t9999_unknown_key": 0,
            },
            "ttl": 27429,
            "sections": [1, 2, 3],
        })
        mock_browser = MagicMock()
        mock_browser.page_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_page)
        mock_browser.page_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("naukri_server.tools.insights._get_browser", return_value=mock_browser):
            result = await _get_profile_prompts()

        assert result["status"] == "success"
        assert result["pending_count"] == 4  # salary_breakup, locality, complete, iti_trades
        assert result["completed_count"] == 1  # t4170 has value 1
        # Pending sorted by impact: high first
        assert result["pending_prompts"][0]["impact"] == "high"
        assert result["cache_ttl_seconds"] == 27429
        assert result["widget_sections_count"] == 3
        # All state keys preserved
        assert len(result["all_state_keys"]) == 6

    @pytest.mark.asyncio
    async def test_profile_prompts_all_completed(self):
        """All known states with value > 0 appear as completed."""
        from naukri_server.tools.insights import _get_profile_prompts

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={
            "states": {
                "t2536_click_add_salary_breakup": 1,
                "t2906_click_add_locality": 1,
                "t2772_click_complete": 2,
                "t4170_click_submit": 1,
                "t2683_view": 3,
            },
            "ttl": 10000,
            "sections": [],
        })
        mock_browser = MagicMock()
        mock_browser.page_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_page)
        mock_browser.page_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("naukri_server.tools.insights._get_browser", return_value=mock_browser):
            result = await _get_profile_prompts()

        assert result["status"] == "success"
        assert result["pending_count"] == 0
        assert result["completed_count"] == 5

    @pytest.mark.asyncio
    async def test_profile_prompts_empty_states(self):
        """Empty states dict returns zero pending and completed."""
        from naukri_server.tools.insights import _get_profile_prompts

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={
            "states": {},
            "ttl": 5000,
            "sections": [1],
        })
        mock_browser = MagicMock()
        mock_browser.page_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_page)
        mock_browser.page_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("naukri_server.tools.insights._get_browser", return_value=mock_browser):
            result = await _get_profile_prompts()

        assert result["status"] == "success"
        assert result["pending_count"] == 0
        assert result["completed_count"] == 0
        assert result["all_state_keys"] == {}

    @pytest.mark.asyncio
    async def test_profile_prompts_ccs_error(self):
        """CCS fetch error returns error status with BROWSER_ERROR code."""
        from naukri_server.tools.insights import _get_profile_prompts

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"error": "Failed to fetch"})
        mock_browser = MagicMock()
        mock_browser.page_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_page)
        mock_browser.page_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("naukri_server.tools.insights._get_browser", return_value=mock_browser):
            result = await _get_profile_prompts()

        assert result["status"] == "error"
        assert result["error_code"] == "BROWSER_ERROR"
        assert "Failed to fetch" in result["message"]
