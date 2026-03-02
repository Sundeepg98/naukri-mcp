"""Tests for Tier 21 enrichments — recruiter activity, settings consent,
profile enrichment, dashboard enrichment, inbox REST API, bulk job fetch,
V1 job detail, and job list parser enrichment.

Every test is PURE: no network, no browser, no file I/O.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. Recruiter Activity enrichment (performance.py)
# =====================================================================

class TestRecruiterActivityBuckets:
    """Tests for bucket parsing in _get_recruiter_activity."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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


# =====================================================================
# 2. Settings consent fields (settings.py)
# =====================================================================

class TestSettingsConsentFields:
    """Tests for consent fields in naukri_settings(action='get')."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_get", new_callable=AsyncMock)
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
        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="get")
        assert result["status"] == "success"
        assert result["naukri_auto_apply_consent"] is True
        assert result["linkedin_auto_apply_consent"] is False
        assert result["whatsapp_apply_notification"] is True
        assert result["whatsapp_profile_notification"] is False

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_get", new_callable=AsyncMock)
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
        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="get")
        assert isinstance(result["naukri_auto_apply_consent"], bool)
        assert isinstance(result["linkedin_auto_apply_consent"], bool)
        assert isinstance(result["whatsapp_apply_notification"], bool)
        assert isinstance(result["whatsapp_profile_notification"], bool)

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_get", new_callable=AsyncMock)
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
        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="get")
        assert result["status"] == "success"
        assert result["count"] == 1
        assert len(result["settings"]) == 1
        # Consent fields should NOT be present
        assert "naukri_auto_apply_consent" not in result
        assert "linkedin_auto_apply_consent" not in result
        assert "whatsapp_apply_notification" not in result
        assert "whatsapp_profile_notification" not in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.settings.api_get", new_callable=AsyncMock)
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
        from naukri_server.tools.settings import naukri_settings
        result = await naukri_settings(action="get")
        s = result["settings"][0]
        assert s["section"] == "Notifications"
        assert s["id"] == "rec_notif"
        assert s["label"] == "Recruiter notification"
        assert s["value"] == "enabled"
        assert s["value_label"] == "Enabled"
        assert s["description"] == "Get notified when recruiters view your profile"


# =====================================================================
# 3. Profile enrichment (profile.py — _get_profile)
# =====================================================================

class TestProfileLookupData:
    """Tests for lookup_data section extracted from lookupData."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_lookup_data_extracted(self, mock_get):
        mock_get.return_value = {
            "profile": [{"name": "Test User", "experience": {}}],
            "profileAdditional": {},
            "lookupData": {
                "resumeScore": 85,
                "lastLoginTime": "2026-03-01T10:00:00",
                "prevLoginTime": "2026-02-28T10:00:00",
                "hasCurrentFullTimeEmployment": True,
                "isPaidUser": False,
                "isJobseekerAgentEligible": True,
                "int360RoleExp": "2027-01-01",
                "ffRDSubExp": "2026-12-01",
            },
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["status"] == "success"
        ld = result["lookup_data"]
        assert ld["resume_score"] == 85
        assert ld["last_login"] == "2026-03-01T10:00:00"
        assert ld["prev_login"] == "2026-02-28T10:00:00"
        assert ld["has_current_employment"] is True
        assert ld["is_paid_user"] is False
        assert ld["is_agent_eligible"] is True
        assert ld["int360_expiry"] == "2027-01-01"
        assert ld["ff_rd_expiry"] == "2026-12-01"


class TestProfileAiFeatures:
    """Tests for ai_features section extracted from additionalDetails."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_ai_features_extracted(self, mock_get):
        mock_get.return_value = {
            "profile": [{"name": "Test User", "experience": {}}],
            "profileAdditional": {},
            "additionalDetails": {
                "isAIResumeEligible": True,
                "curEmpVerEligibility": False,
            },
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["ai_features"]["is_ai_resume_eligible"] is True
        assert result["ai_features"]["employer_verification_eligible"] is False


class TestProfileExtendedProfile:
    """Tests for extended_profile section (job_search_status, career_break, stale_tags)."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_extended_profile_extracted(self, mock_get):
        mock_get.return_value = {
            "profile": [{"name": "Test", "experience": {}}],
            "profileAdditional": {},
            "extendedProfile": {
                "jbSearchStatus": {
                    "data": [{"value": "actively_searching"}],
                },
                "careerBreak": {
                    "data": [{"comingFromBreak": True}],
                },
                "tags": {
                    "data": [
                        {"value": "Python", "meta": {"status": "active"}},
                        {"value": "Java", "meta": {"status": "inactive"}},
                        {"value": "COBOL", "meta": {"status": "inactive"}},
                    ],
                },
            },
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        ep = result["extended_profile"]
        assert ep["job_search_status"] == "actively_searching"
        assert ep["career_break"] is True
        assert ep["stale_tags"] == ["Java", "COBOL"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_extended_profile_no_stale_tags(self, mock_get):
        """When no tags are inactive, stale_tags is an empty list."""
        mock_get.return_value = {
            "profile": [{"name": "Test", "experience": {}}],
            "profileAdditional": {},
            "extendedProfile": {
                "jbSearchStatus": {"data": [{"value": "open"}]},
                "careerBreak": {"data": [{"comingFromBreak": False}]},
                "tags": {
                    "data": [
                        {"value": "React", "meta": {"status": "active"}},
                    ],
                },
            },
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["extended_profile"]["stale_tags"] == []


class TestProfileSchools:
    """Tests for schools section parsing."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_schools_parsed(self, mock_get):
        mock_get.return_value = {
            "profile": [{"name": "Test", "experience": {}}],
            "profileAdditional": {},
            "schools": [
                {
                    "educationType": {"value": "XII"},
                    "schoolBoard": {"value": "CBSE"},
                    "schoolCompletionYear": 2016,
                    "schoolPercentage": {"value": "80-90%"},
                    "schoolMedium": {"value": "English"},
                },
                {
                    "educationType": {"value": "X"},
                    "schoolBoard": {"value": "ICSE"},
                    "schoolCompletionYear": 2014,
                    "schoolPercentage": {"value": "90-100%"},
                    "schoolMedium": {"value": "English"},
                    "schoolLevel": "10",
                },
            ],
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert len(result["schools"]) == 2
        assert result["schools"][0]["level"] == "XII"
        assert result["schools"][0]["board"] == "CBSE"
        assert result["schools"][0]["year"] == 2016
        assert result["schools"][0]["percentage_range"] == "80-90%"
        assert result["schools"][0]["medium"] == "English"
        assert result["schools"][1]["level"] == "X"
        assert result["schools"][1]["board"] == "ICSE"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_schools_fallback_to_school_level(self, mock_get):
        """When educationType is missing, falls back to schoolLevel."""
        mock_get.return_value = {
            "profile": [{"name": "Test", "experience": {}}],
            "profileAdditional": {},
            "schools": [
                {
                    "educationType": {},
                    "schoolLevel": "12",
                    "schoolBoard": {},
                    "schoolCompletionYear": 2018,
                    "schoolPercentage": {},
                    "schoolMedium": {},
                },
            ],
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["schools"][0]["level"] == "Class 12"


class TestProfileMissingSections:
    """Tests that missing sections are gracefully omitted from result."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_missing_sections_not_in_result(self, mock_get):
        """When lookupData, additionalDetails, extendedProfile, schools are absent,
        their corresponding keys are not present in the result."""
        mock_get.return_value = {
            "profile": [{"name": "Minimal User", "experience": {}}],
            "profileAdditional": {},
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["status"] == "success"
        assert "lookup_data" not in result
        assert "ai_features" not in result
        assert "extended_profile" not in result
        assert "schools" not in result


# =====================================================================
# 4. Dashboard enrichment (profile.py — _get_dashboard)
# =====================================================================

class TestDashboardAssessments:
    """Tests for assessments parsing in _get_dashboard."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_assessments_with_results(self, mock_get):
        mock_get.return_value = {
            "dashBoard": {
                "assessments": [
                    {
                        "skill": "Python",
                        "level": {"name": "Advanced"},
                        "questionCount": 20,
                        "duration": 30,
                        "maxAttempts": 3,
                        "testId": "T123",
                        "results": {
                            "scorePercent": 85,
                            "rank": 1200,
                            "status": "passed",
                        },
                    }
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["status"] == "success"
        a = result["assessments"][0]
        assert a["skill"] == "Python"
        assert a["level"] == "Advanced"
        assert a["question_count"] == 20
        assert a["duration_mins"] == 30
        assert a["max_attempts"] == 3
        assert a["test_id"] == "T123"
        assert a["results"]["score_percent"] == 85
        assert a["results"]["rank"] == 1200
        assert a["results"]["status"] == "passed"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_assessments_without_results(self, mock_get):
        """Assessment with no results has results=None."""
        mock_get.return_value = {
            "dashBoard": {
                "assessments": [
                    {
                        "skill": "JavaScript",
                        "level": {"name": "Beginner"},
                        "questionCount": 15,
                        "duration": 20,
                        "maxAttempts": 5,
                        "testId": "T456",
                        "results": None,
                    }
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        a = result["assessments"][0]
        assert a["results"] is None


class TestDashboardExpectedCtcStructured:
    """Tests for expected_ctc_structured calculation."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_expected_ctc_structured_calculation(self, mock_get):
        """lacs * 100000 + thousands * 1000 = total_annual."""
        mock_get.return_value = {
            "dashBoard": {
                "expectedCtc": {
                    "lacs": {"value": 15},
                    "thousands": {"value": 50},
                },
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        ctc = result["expected_ctc_structured"]
        assert ctc["lacs"] == 15
        assert ctc["thousands"] == 50
        assert ctc["total_annual"] == 15 * 100000 + 50 * 1000  # 1,550,000

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_expected_ctc_structured_zero_thousands(self, mock_get):
        """Zero thousands still computes correctly."""
        mock_get.return_value = {
            "dashBoard": {
                "expectedCtc": {
                    "lacs": {"value": 10},
                    "thousands": {"value": 0},
                },
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        ctc = result["expected_ctc_structured"]
        assert ctc["total_annual"] == 1000000

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_expected_ctc_structured_missing_value(self, mock_get):
        """Missing value defaults to 0."""
        mock_get.return_value = {
            "dashBoard": {
                "expectedCtc": {
                    "lacs": {},
                    "thousands": {},
                },
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        ctc = result["expected_ctc_structured"]
        assert ctc["lacs"] == 0
        assert ctc["thousands"] == 0
        assert ctc["total_annual"] == 0

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_expected_ctc_not_present_when_not_dict(self, mock_get):
        """If expectedCtc is not a dict, expected_ctc_structured is not in result."""
        mock_get.return_value = {
            "dashBoard": {
                "expectedCtc": "15 LPA",
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert "expected_ctc_structured" not in result


class TestDashboardRecommendedCompanies:
    """Tests for recommended_companies from similarCompToFollow."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_recommended_companies_caps_at_5(self, mock_get):
        """similarCompToFollow is capped at 5 entries."""
        companies = [
            {"name": f"Company {i}", "rating": 4.0 + i * 0.1, "reviews": {"count": 100 * i}}
            for i in range(10)
        ]
        mock_get.return_value = {"dashBoard": {"similarCompToFollow": companies}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert len(result["recommended_companies"]) == 5
        assert result["recommended_companies"][0]["name"] == "Company 0"
        assert result["recommended_companies"][4]["name"] == "Company 4"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_recommended_companies_with_reviews_count(self, mock_get):
        mock_get.return_value = {
            "dashBoard": {
                "similarCompToFollow": [
                    {"name": "TCS", "rating": 3.8, "reviews": {"count": 50000}},
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["recommended_companies"][0]["reviews_count"] == 50000

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_recommended_companies_empty_list(self, mock_get):
        """Empty similarCompToFollow means recommended_companies is not in result."""
        mock_get.return_value = {"dashBoard": {"similarCompToFollow": []}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert "recommended_companies" not in result


class TestDashboardFeatureFlags:
    """Tests for feature_flags extraction."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_feature_flags_extracted(self, mock_get):
        mock_get.return_value = {
            "dashBoard": {
                "eligibleFlagForAIMockInterview": True,
                "isAIResumeEligible": False,
                "jbSearchStatus": {
                    "data": [{"value": "open_to_opportunities"}],
                },
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        ff = result["feature_flags"]
        assert ff["ai_mock_interview"] is True
        assert ff["ai_resume"] is False
        assert ff["job_search_status"] == "open_to_opportunities"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_feature_flags_missing_jb_search(self, mock_get):
        """When jbSearchStatus is missing, job_search_status is None."""
        mock_get.return_value = {"dashBoard": {}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["feature_flags"]["job_search_status"] is None


class TestDashboardEmptyFields:
    """Tests for graceful handling of empty/missing dashboard fields."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_empty_dashboard(self, mock_get):
        """Empty dashBoard returns success with status and feature_flags."""
        mock_get.return_value = {"dashBoard": {}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["status"] == "success"
        # None values are stripped
        assert "profile_views" not in result
        assert "assessments" not in result
        assert "expected_ctc_structured" not in result
        assert "recommended_companies" not in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_missing_dashboard_key(self, mock_get):
        """Missing dashBoard key returns success with all None."""
        mock_get.return_value = {}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["status"] == "success"


# =====================================================================
# 5. Inbox REST API (inbox.py)
# =====================================================================

class TestInboxRestApi:
    """Tests for REST GET first / POST fallback in _fetch_inbox."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_get", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.inbox.api_post", new_callable=AsyncMock)
    @patch("naukri_server.tools.inbox.api_get", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.inbox.api_get", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.inbox.api_get", new_callable=AsyncMock)
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


# =====================================================================
# 6. Bulk job fetch (jobs.py — _bulk_fetch_jobs)
# =====================================================================

class TestBulkFetchJobs:
    """Tests for _bulk_fetch_jobs."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_post", new_callable=AsyncMock)
    async def test_successful_bulk_fetch(self, mock_post):
        """Successful bulk fetch with multiple job IDs."""
        mock_post.return_value = {
            "jobDetails": [
                {
                    "jobId": "111",
                    "title": "Python Dev",
                    "companyName": "Acme",
                    "salaryDetail": {"minimumSalary": 500000, "maximumSalary": 1000000, "label": "5-10 LPA"},
                    "placeholders": [{"type": "location", "label": "Bangalore"}],
                },
                {
                    "jobId": "222",
                    "title": "Java Dev",
                    "companyName": "Globex",
                    "salaryDetail": {"minimumSalary": 600000, "maximumSalary": 1200000, "label": "6-12 LPA"},
                    "placeholders": [{"type": "location", "label": "Mumbai"}],
                },
            ]
        }
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        result = await _bulk_fetch_jobs(job_ids=["111", "222"])
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["jobs"][0]["job_id"] == "111"
        assert result["jobs"][1]["job_id"] == "222"

    @pytest.mark.asyncio
    async def test_empty_job_ids_returns_validation_error(self):
        """Empty job_ids returns validation error."""
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        result = await _bulk_fetch_jobs(job_ids=[])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_post", new_callable=AsyncMock)
    async def test_caps_at_20_job_ids(self, mock_post):
        """More than 20 job IDs are capped at 20."""
        mock_post.return_value = {"jobDetails": []}
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        ids = [str(i) for i in range(30)]
        result = await _bulk_fetch_jobs(job_ids=ids)
        # Check that api_post was called with only 20 IDs
        call_body = mock_post.call_args[0][1]
        assert len(call_body["jobIds"]) == 20

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_post", new_callable=AsyncMock)
    async def test_invalid_ids_empty_job_details(self, mock_post):
        """Invalid IDs silently dropped — empty jobDetails yields count=0."""
        mock_post.return_value = {"jobDetails": []}
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        result = await _bulk_fetch_jobs(job_ids=["invalid_id"])
        assert result["status"] == "success"
        assert result["count"] == 0
        assert result["jobs"] == []


# =====================================================================
# 7. V1 job detail (jobs.py — _get_job_v1)
# =====================================================================

class TestGetJobV1:
    """Tests for _get_job_v1."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock)
    async def test_walk_in_fields_extracted(self, mock_get):
        """Walk-in fields are extracted: is_walk_in, walkin_time, walkin_venue."""
        mock_get.return_value = {
            "job": {
                "jobId": "55555",
                "post": "Marketing Manager",
                "companyName": "BigCorp",
                "isWalkIn": True,
                "walkinTime": "10:00 AM - 4:00 PM",
                "walkinVenue": "Building A, Floor 3, Bangalore",
                "walkingDateFrom": "2026-03-10",
                "walkingDateTo": "2026-03-12",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="55555")
        assert result["status"] == "success"
        assert result["is_walk_in"] is True
        assert result["walkin_time"] == "10:00 AM - 4:00 PM"
        assert result["walkin_venue"] == "Building A, Floor 3, Bangalore"
        assert result["walkin_date_from"] == "2026-03-10"
        assert result["walkin_date_to"] == "2026-03-12"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock)
    async def test_contact_fields_extracted(self, mock_get):
        """Contact fields are extracted: contact_name, contact_email, contact_phone."""
        mock_get.return_value = {
            "job": {
                "jobId": "66666",
                "post": "Recruiter",
                "companyName": "HireCo",
                "contactName": "Jane Smith",
                "email": "jane@hireco.com",
                "tel": "+91-9876543210",
                "CONTDESIG": "HR Manager",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="66666")
        assert result["contact_name"] == "Jane Smith"
        assert result["contact_email"] == "jane@hireco.com"
        assert result["contact_phone"] == "+91-9876543210"
        assert result["contact_designation"] == "HR Manager"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock)
    async def test_metrics_extracted(self, mock_get):
        """Metrics are extracted: jd_views, jd_applies, vacancy."""
        mock_get.return_value = {
            "job": {
                "jobId": "77777",
                "post": "Data Scientist",
                "companyName": "DataCo",
                "jdViews": 1500,
                "jdApplies": 200,
                "noOfVacancy": 5,
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="77777")
        assert result["jd_views"] == 1500
        assert result["jd_applies"] == 200
        assert result["vacancy"] == 5

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock)
    async def test_none_values_stripped(self, mock_get):
        """None values are stripped from the result."""
        mock_get.return_value = {
            "job": {
                "jobId": "88888",
                "post": "QA Engineer",
                "companyName": "TestCo",
                # Many fields missing = None
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="88888")
        assert result["status"] == "success"
        # Falsy None fields should be absent (stripped)
        assert "contact_email" not in result
        assert "contact_phone" not in result
        assert "walkin_time" not in result
        assert "walkin_venue" not in result
        assert "vacancy" not in result

    @pytest.mark.asyncio
    async def test_missing_job_id_returns_validation_error(self):
        """Missing job_id returns validation error."""
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock)
    async def test_salary_hidden_field(self, mock_get):
        """salary_hidden is True when showSal == 'n'."""
        mock_get.return_value = {
            "job": {
                "jobId": "99999",
                "post": "Secret Agent",
                "companyName": "MI6",
                "showSal": "n",
                "minSal": 0,
                "maxSal": 0,
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="99999")
        assert result["salary_hidden"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock)
    async def test_work_mode_mapping(self, mock_get):
        """wfhType maps to human-readable work_mode."""
        mock_get.return_value = {
            "job": {
                "jobId": "11111",
                "post": "Remote Dev",
                "companyName": "RemoteCo",
                "wfhType": "2",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="11111")
        assert result["work_mode"] == "remote"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock)
    async def test_hiring_for_field(self, mock_get):
        """hiringFor field is extracted when present."""
        mock_get.return_value = {
            "job": {
                "jobId": "12121",
                "post": "Consultant",
                "companyName": "ConsultCo",
                "hiringFor": "Google India",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="12121")
        assert result["hiring_for"] == "Google India"


# =====================================================================
# 8. Job list parser enrichment (job_parsing.py)
# =====================================================================

class TestJobListParserEnrichment:
    """Tests for enrichment fields in _parse_job_list."""

    def test_hiring_for_field(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1", "hiringFor": "Microsoft"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["hiring_for"] == "Microsoft"

    def test_hiring_for_none_when_missing(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["hiring_for"] is None

    def test_diversity_tag(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1", "diversityTagText": "Women in Tech"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["diversity_tag"] == "Women in Tech"

    def test_diversity_tag_none_when_missing(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["diversity_tag"] is None

    def test_experience_text(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1", "experienceText": "3-5 Yrs"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["experience_text"] == "3-5 Yrs"

    def test_experience_text_none_when_missing(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["experience_text"] is None

    def test_salary_min_raw_from_salary_detail(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "1",
            "salaryDetail": {
                "minimumSalary": 500000,
                "maximumSalary": 1200000,
                "label": "5-12 LPA",
                "hideSalary": False,
            },
        }]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["salary_min_raw"] == 500000
        assert result[0]["salary_max_raw"] == 1200000
        assert result[0]["salary_hidden"] is False

    def test_salary_hidden_true(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "1",
            "salaryDetail": {
                "minimumSalary": 0,
                "maximumSalary": 0,
                "hideSalary": True,
            },
        }]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["salary_hidden"] is True

    def test_salary_hidden_defaults_to_false(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "1",
            "salaryDetail": {
                "minimumSalary": 300000,
                "maximumSalary": 600000,
            },
        }]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["salary_hidden"] is False

    def test_all_enrichment_fields_together(self):
        """All Tier 21 enrichment fields present in a single job item."""
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "42",
            "title": "Full Stack Dev",
            "companyName": "TechCorp",
            "hiringFor": "Amazon",
            "diversityTagText": "LGBTQ+ Friendly",
            "experienceText": "2-4 Yrs",
            "salaryDetail": {
                "minimumSalary": 800000,
                "maximumSalary": 1600000,
                "label": "8-16 LPA",
                "hideSalary": False,
            },
            "placeholders": [{"type": "location", "label": "Hyderabad"}],
        }]
        result = _parse_job_list(jobs_raw, 10)
        j = result[0]
        assert j["hiring_for"] == "Amazon"
        assert j["diversity_tag"] == "LGBTQ+ Friendly"
        assert j["experience_text"] == "2-4 Yrs"
        assert j["salary_min_raw"] == 800000
        assert j["salary_max_raw"] == 1600000
        assert j["salary_hidden"] is False

    def test_empty_salary_detail_still_adds_raw_fields(self):
        """Even an empty salaryDetail dict adds the raw fields."""
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "1",
            "salaryDetail": {},
        }]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["salary_min_raw"] is None
        assert result[0]["salary_max_raw"] is None
        assert result[0]["salary_hidden"] is False


# =====================================================================
# 9. Additional edge cases
# =====================================================================

class TestRecruiterActivityNonDictItemSkipped:
    """Ensure non-dict items in jobseekerActivityList are skipped."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.performance.api_post", new_callable=AsyncMock)
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


class TestDashboardAssessmentEdgeCases:
    """Edge cases for dashboard assessments."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_assessment_with_empty_results_dict(self, mock_get):
        """Assessment with empty results dict {} is falsy in Python, so results=None."""
        mock_get.return_value = {
            "dashBoard": {
                "assessments": [
                    {
                        "skill": "SQL",
                        "level": {"name": "Intermediate"},
                        "questionCount": 10,
                        "duration": 15,
                        "maxAttempts": 2,
                        "testId": "T789",
                        "results": {},
                    }
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        a = result["assessments"][0]
        # Empty dict {} is falsy, so `if a.get("results")` is False -> results=None
        assert a["results"] is None

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_assessment_with_populated_results(self, mock_get):
        """Assessment with a non-empty results dict parses scorePercent, rank, status."""
        mock_get.return_value = {
            "dashBoard": {
                "assessments": [
                    {
                        "skill": "SQL",
                        "level": {"name": "Intermediate"},
                        "questionCount": 10,
                        "duration": 15,
                        "maxAttempts": 2,
                        "testId": "T789",
                        "results": {
                            "scorePercent": 72,
                            "rank": 500,
                            "status": "passed",
                        },
                    }
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        a = result["assessments"][0]
        assert a["results"]["score_percent"] == 72
        assert a["results"]["rank"] == 500
        assert a["results"]["status"] == "passed"


class TestInboxRestApiFallbackDetails:
    """Additional tests for inbox REST to POST fallback details."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.inbox.api_post", new_callable=AsyncMock)
    @patch("naukri_server.tools.inbox.api_get", new_callable=AsyncMock)
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


class TestBulkFetchJobsViaUnifiedTool:
    """Tests for bulk action routed through naukri_jobs."""

    @pytest.mark.asyncio
    async def test_bulk_without_job_ids_returns_error(self):
        """bulk action without job_ids returns validation error."""
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="bulk")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_detail_v1_without_job_id_returns_error(self):
        """detail_v1 action without job_id returns validation error."""
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="detail_v1")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"


class TestJobV1IsExpired:
    """Test additional V1-unique fields."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock)
    async def test_is_expired_and_closing_date(self, mock_get):
        mock_get.return_value = {
            "job": {
                "jobId": "33333",
                "post": "Expired Role",
                "companyName": "OldCo",
                "isExpiredJob": True,
                "closingDate": "2025-12-31",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="33333")
        assert result["is_expired"] is True
        assert result["closing_date"] == "2025-12-31"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_get", new_callable=AsyncMock)
    async def test_is_consultant_field(self, mock_get):
        mock_get.return_value = {
            "job": {
                "jobId": "44444",
                "post": "Contractor",
                "companyName": "StaffCo",
                "cons": "y",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="44444")
        assert result["is_consultant"] is True
