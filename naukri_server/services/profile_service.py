"""Profile service — business logic for profile fetch, caching, dashboard, and audit."""

from naukri_server.api import NaukriAPIError
from naukri_server.domain.profile_completeness import CompletionReport
from naukri_server.interfaces import api_client
from naukri_server.config import (
    DASHBOARD_API, DASHBOARD_PROPERTIES, PROFILE_API,
    PROFILE_CACHE_TTL,
)
from naukri_server.utils import TtlCache
from naukri_server.validation import validate_profile

__all__ = [
    "_get_profile",
    "_fetch_raw_profile",
    "get_cached_profile",
    "_get_dashboard",
    "_fetch_raw_dashboard",
    "get_cached_dashboard",
    "_audit_profile",
    "_profile_ttl_cache",
    "_dashboard_ttl_cache",
    "_TtlCache",
]

# Backward-compat alias — tests import _TtlCache from profile.py (re-exported there)
_TtlCache = TtlCache

# --- Profile & dashboard TTL caches for composite tools ---
_profile_ttl_cache = TtlCache(PROFILE_CACHE_TTL)
_dashboard_ttl_cache = TtlCache(PROFILE_CACHE_TTL)


async def _fetch_raw_profile() -> dict:
    """Fetch profile via API (used by _TtlCache)."""
    return await _get_profile()


async def get_cached_profile() -> dict:
    """Return cached profile if fresh, otherwise fetch and cache.

    Used by composite tools (smart_apply, auto_hunt, compare, skill_gap,
    resume_tailor, daily_brief) to avoid redundant profile API calls.
    """
    return await _profile_ttl_cache.get(_fetch_raw_profile)


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _get_profile() -> dict:
    """Fetch full Naukri profile via API (internal helper)."""
    try:
        data = await api_client.get(
            PROFILE_API,
            {"expand_level": "4"},
        )

        profile = data.get("profile", [{}])[0]
        additional = data.get("profileAdditional", {})

        skills = []
        for s in data.get("itskills", []):
            exp_time = s.get("experienceTime", {})
            skills.append({
                "skill": s.get("skill"),
                "experience_years": exp_time.get("year", 0),
                "experience_months": exp_time.get("month", 0),
            })

        employment = []
        for emp in data.get("employments", []):
            employment.append({
                "designation": emp.get("designation"),
                "organization": emp.get("organization"),
                "description": emp.get("description"),
                "start_date": emp.get("startDate"),
                "end_date": emp.get("endDate") or "Present",
            })

        education = []
        for edu in data.get("educations", []):
            education.append({
                "degree": edu.get("course", {}).get("value"),
                "specialization": edu.get("specialisation", {}).get("value"),
                "institute": edu.get("institute"),
                "year": edu.get("yearOfCompletion"),
            })

        exp = profile.get("experience", {})

        result = {
            "status": "success",
            "name": profile.get("name"),
            "resume_headline": profile.get("resumeHeadline"),
            "current_ctc": profile.get("absoluteCtc"),
            "expected_ctc": profile.get("absoluteExpectedCtc"),
            "notice_period": profile.get("noticePeriod", {}).get("value"),
            "total_experience": f"{exp.get('year', 0)} years {exp.get('month', 0)} months",
            "current_location": profile.get("city", {}).get("value"),
            "gender": {"M": "Male", "F": "Female"}.get(profile.get("gender"), profile.get("gender")),
            "key_skills": profile.get("keySkills"),
            "summary": profile.get("summary"),
            "certifications": data.get("certifications", []),
            "projects": data.get("projects", []),
            "languages": data.get("languages", []),
            "online_profiles": data.get("onlineProfiles", []),
            "skills_with_experience": skills,
            "employment": employment,
            "education": education,
            "profile_id": additional.get("profileId"),
        }

        # --- Communication settings (notification preferences) ---
        comm_settings = data.get("communicationSettings") or data.get("communicationPreferences") or profile.get("communicationSettings")
        if comm_settings and isinstance(comm_settings, dict):
            result["communication_settings"] = {
                "email_notifications": comm_settings.get("emailNotifications") or comm_settings.get("emailAlerts"),
                "sms_notifications": comm_settings.get("smsNotifications") or comm_settings.get("smsAlerts"),
                "push_notifications": comm_settings.get("pushNotifications"),
                "job_alerts": comm_settings.get("jobAlerts") or comm_settings.get("jobAlert"),
                "recruiter_messages": comm_settings.get("recruiterMessages") or comm_settings.get("recruiterMail"),
                "promotional": comm_settings.get("promotional") or comm_settings.get("marketingMail"),
                "whatsapp_notifications": comm_settings.get("whatsappNotifications") or comm_settings.get("whatsappAlerts"),
            }
            # Strip None values
            result["communication_settings"] = {k: v for k, v in result["communication_settings"].items() if v is not None}
            if not result["communication_settings"]:
                del result["communication_settings"]

        # --- Resdex visibility (profile visibility flags) ---
        resdex = data.get("resdexVisibility") or data.get("profileVisibility") or additional.get("resdexVisibility") or profile.get("resdexVisibility")
        if resdex and isinstance(resdex, dict):
            result["resdex_visibility"] = {
                "show_profile": resdex.get("showProfile") or resdex.get("profileVisible"),
                "show_current_employer": resdex.get("showCurrentEmployer") or resdex.get("currentEmployerVisible"),
                "show_email": resdex.get("showEmail") or resdex.get("emailVisible"),
                "show_phone": resdex.get("showPhone") or resdex.get("mobileVisible"),
                "show_resume": resdex.get("showResume") or resdex.get("resumeVisible"),
                "hide_from_companies": resdex.get("hideFromCompanies") or resdex.get("blockedCompanies"),
                "search_visible": resdex.get("searchVisible") or resdex.get("isSearchable"),
                "active_job_seeker": resdex.get("activeJobSeeker") or resdex.get("isActivelySearching"),
            }
            # Strip None values
            result["resdex_visibility"] = {k: v for k, v in result["resdex_visibility"].items() if v is not None}
            if not result["resdex_visibility"]:
                del result["resdex_visibility"]
        elif resdex and isinstance(resdex, str):
            # Sometimes returned as a simple string like "visible" / "hidden"
            result["resdex_visibility"] = resdex

        # --- lookupData (resume score, login times, subscription flags) ---
        lookup = data.get("lookupData", {})
        if lookup:
            result["lookup_data"] = {
                "resume_score": lookup.get("resumeScore"),
                "last_login": lookup.get("lastLoginTime"),
                "prev_login": lookup.get("prevLoginTime"),
                "has_current_employment": lookup.get("hasCurrentFullTimeEmployment"),
                "is_paid_user": lookup.get("isPaidUser"),
                "is_agent_eligible": lookup.get("isJobseekerAgentEligible"),
                "int360_expiry": lookup.get("int360RoleExp"),
                "ff_rd_expiry": lookup.get("ffRDSubExp"),
            }

        # --- additionalDetails (AI feature eligibility) ---
        additional_details = data.get("additionalDetails", {})
        if additional_details:
            result["ai_features"] = {
                "is_ai_resume_eligible": additional_details.get("isAIResumeEligible"),
                "employer_verification_eligible": additional_details.get("curEmpVerEligibility"),
            }

        # --- extendedProfile (job search status, career break, stale tags) ---
        ext = data.get("extendedProfile", {})
        if ext:
            jb_status = ext.get("jbSearchStatus", {})
            jb_data = (jb_status.get("data") or [{}])[0] if isinstance(jb_status.get("data"), list) else {}

            career_break = ext.get("careerBreak", {})
            cb_data = (career_break.get("data") or [{}])[0] if isinstance(career_break.get("data"), list) else {}

            tags_data = ext.get("tags", {}).get("data", [])
            stale_tags = [t.get("value") for t in tags_data if isinstance(t, dict) and t.get("meta", {}).get("status") == "inactive"]

            result["extended_profile"] = {
                "job_search_status": jb_data.get("value"),
                "career_break": cb_data.get("comingFromBreak", False),
                "stale_tags": stale_tags,
            }

        # --- schools (10th/12th education) ---
        schools = data.get("schools", [])
        if schools:
            result["schools"] = [
                {
                    "level": (s.get("educationType", {}).get("value") or f"Class {s.get('schoolLevel')}"),
                    "board": (s.get("schoolBoard", {}).get("value") or ""),
                    "year": s.get("schoolCompletionYear"),
                    "percentage_range": (s.get("schoolPercentage", {}).get("value") or ""),
                    "medium": (s.get("schoolMedium", {}).get("value") or ""),
                }
                for s in schools if isinstance(s, dict)
            ]

        warnings = validate_profile(result)
        if warnings:
            result["warnings"] = warnings
        return result
    except ValueError as e:
        return {"status": "error", "message": str(e), "error_code": "API_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Profile API failed: {type(e).__name__}: {e!r}", "error_code": "API_ERROR"}


async def _audit_profile() -> dict:
    """Audit profile and return actionable improvement suggestions (internal helper).

    I/O: fetches profile + completeness percentage.
    Pure computation delegated to CompletionReport.from_profile().
    """
    try:
        # --- I/O: fetch profile data ---
        profile_result = await _get_profile()
        if profile_result.get("status") != "success":
            return profile_result

        # --- I/O: fetch completeness percentage ---
        from naukri_server.tools.assessments import _get_profile_completeness
        completeness_result = await _get_profile_completeness()
        completeness_pct = None
        if completeness_result.get("status") == "success":
            completeness_pct = completeness_result.get("completeness_percent")

        # --- Pure computation (domain object) ---
        report = CompletionReport.from_profile(profile_result, completeness_pct)

        return {"status": "success", **report.to_dict()}
    except Exception as e:
        return {"status": "error", "message": f"Profile audit failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


async def _fetch_raw_dashboard() -> dict:
    """Fetch dashboard via API (used by _TtlCache)."""
    return await _get_dashboard()


async def get_cached_dashboard() -> dict:
    """Dashboard data with TTL cache."""
    return await _dashboard_ttl_cache.get(_fetch_raw_dashboard)


# ---------------------------------------------------------------------------
# Dashboard helper (merged into naukri_profile action="dashboard")
# ---------------------------------------------------------------------------

async def _get_dashboard() -> dict:
    """Get your Naukri dashboard summary via API.

    Returns profile views, recruiter activity, CTC, experience,
    recruiter invites, unread mail counts, application stats, profile
    completeness, resume details, saved jobs count, and more — a
    comprehensive health check of your Naukri presence.

    Returns:
        - {status: "success", profile_views, recruiter_activity_date, ctc_lpa, experience_years,
           unread_invites, total_invites, unread_relevant_mail, has_inbox, total_matches,
           applied_count, profile_completion, profile_score, saved_jobs_count,
           resume_name, resume_last_updated, profile_title, profile_image_url,
           designation, company, location, notice_period, preferred_locations,
           total_unread_mail, search_impressions, recruiter_actions_count,
           is_profile_verified, is_email_verified, is_phone_verified,
           subscription_type, subscription_expiry, job_alerts_count,
           pending_invites, accepted_invites, ...}
        - {status: "error", message}
    """
    try:
        data = await api_client.get(DASHBOARD_API, params={"properties": DASHBOARD_PROPERTIES})
        db = data.get("dashBoard", {})

        # --- Core fields (existing) ---
        result = {
            "status": "success",
            "profile_views": db.get("profileViewCount"),
            "recruiter_activity_date": db.get("recruiterActionsLatestDate"),
            "ctc_lpa": db.get("rawCtc"),
            "experience_years": db.get("rawTotalExperience"),
            "unread_invites": db.get("unreadPowerNvite"),
            "total_invites": db.get("totalPowerNvite"),
            "unread_relevant_mail": db.get("unreadMostRelevantMail"),
            "has_inbox": db.get("hasInboxFlag") == "Y",
            "total_matches": db.get("mrt"),
        }

        # --- Application stats ---
        result["applied_count"] = db.get("appliedCount") or db.get("totalApplied")
        result["applied_today"] = db.get("appliedToday")
        result["applied_this_week"] = db.get("appliedThisWeek")

        # --- Profile completeness and score ---
        result["profile_completion"] = db.get("profileCompletion") or db.get("completeness") or db.get("profileCompleteness")
        result["profile_score"] = db.get("profileScore") or db.get("profileRank")
        result["profile_quality"] = db.get("profileQuality")
        result["profile_strength"] = db.get("profileStrength")

        # --- Saved jobs ---
        result["saved_jobs_count"] = db.get("savedJobsCount") or db.get("totalSavedJobs")

        # --- Resume details ---
        resume = db.get("resumeDetails") or db.get("resume") or {}
        if isinstance(resume, dict):
            result["resume_name"] = resume.get("name") or resume.get("resumeName") or db.get("resumeName")
            result["resume_last_updated"] = resume.get("lastUpdated") or resume.get("modifiedDate") or db.get("resumeLastUpdated")
            result["resume_headline"] = resume.get("resumeHeadline") or db.get("resumeHeadline")
            result["resume_id"] = resume.get("resumeId") or resume.get("id")
        else:
            result["resume_name"] = db.get("resumeName")
            result["resume_last_updated"] = db.get("resumeLastUpdated")
            result["resume_headline"] = db.get("resumeHeadline")
            result["resume_id"] = None

        # --- User identity ---
        result["profile_title"] = db.get("profileTitle") or db.get("title")
        result["profile_image_url"] = db.get("profileImageUrl") or db.get("imageUrl") or db.get("photoUrl")
        result["name"] = db.get("name") or db.get("fullName")
        result["email"] = db.get("email") or db.get("emailId")
        result["phone"] = db.get("phone") or db.get("mobileNo")
        result["user_id"] = db.get("userId") or db.get("resId")

        # --- Current employment context ---
        result["designation"] = db.get("designation") or db.get("currentDesignation")
        result["company"] = db.get("company") or db.get("currentCompany")
        result["location"] = db.get("location") or db.get("city")
        result["notice_period"] = db.get("noticePeriod")
        result["preferred_locations"] = db.get("preferredLocations") or db.get("locationPref")

        # --- Mail / messaging ---
        result["total_unread_mail"] = db.get("totalUnreadMail") or db.get("unreadMailCount")
        result["unread_direct_mail"] = db.get("unreadDirectMail")
        result["unread_forwarded_mail"] = db.get("unreadForwardedMail")

        # --- Search and recruiter analytics ---
        result["search_impressions"] = db.get("searchImpressions") or db.get("searchAppearances") or db.get("searchCount")
        result["recruiter_actions_count"] = db.get("recruiterActionsCount") or db.get("totalRecruiterActions")
        result["profile_views_trend"] = db.get("profileViewsTrend") or db.get("viewsTrend")
        result["profile_views_last_week"] = db.get("profileViewsLastWeek")
        result["profile_views_last_month"] = db.get("profileViewsLastMonth")

        # --- Verification flags ---
        result["is_profile_verified"] = db.get("isProfileVerified") or db.get("profileVerified")
        result["is_email_verified"] = db.get("isEmailVerified") or db.get("emailVerified")
        result["is_phone_verified"] = db.get("isPhoneVerified") or db.get("mobileVerified")

        # --- Subscription ---
        result["subscription_type"] = db.get("subscriptionType") or db.get("productName")
        result["subscription_expiry"] = db.get("subscriptionExpiry") or db.get("subscriptionEndDate")
        result["is_premium"] = db.get("isPremium") or db.get("premiumUser")

        # --- Job alerts ---
        result["job_alerts_count"] = db.get("jobAlertsCount") or db.get("totalJobAlerts")

        # --- Invite breakdown ---
        result["pending_invites"] = db.get("pendingNvite") or db.get("pendingInvites")
        result["accepted_invites"] = db.get("acceptedNvite") or db.get("acceptedInvites")
        result["declined_invites"] = db.get("declinedNvite") or db.get("declinedInvites")

        # --- Resdex / visibility ---
        result["resdex_visible"] = db.get("resdexVisible") or db.get("showProfile")
        result["active_status"] = db.get("activeStatus") or db.get("activityStatus")
        result["last_active"] = db.get("lastActive") or db.get("lastLoginDate")

        # --- Recommendations ---
        result["recommended_jobs_count"] = db.get("recommendedJobsCount") or db.get("totalRecommendedJobs")

        # --- Assessments ---
        assessments_raw = db.get("assessments", [])
        if assessments_raw:
            result["assessments"] = [
                {
                    "skill": a.get("skill"),
                    "level": (a.get("level", {}).get("name") if isinstance(a.get("level"), dict) else None),
                    "question_count": a.get("questionCount"),
                    "duration_mins": a.get("duration"),
                    "max_attempts": a.get("maxAttempts"),
                    "test_id": a.get("testId"),
                    "results": {
                        "score_percent": (a.get("results", {}) or {}).get("scorePercent"),
                        "rank": (a.get("results", {}) or {}).get("rank"),
                        "status": (a.get("results", {}) or {}).get("status"),
                    } if a.get("results") else None,
                }
                for a in assessments_raw if isinstance(a, dict)
            ]

        # --- Expected CTC structured ---
        expected_ctc = db.get("expectedCtc")
        if isinstance(expected_ctc, dict):
            lacs = expected_ctc.get("lacs", {})
            thousands = expected_ctc.get("thousands", {})
            result["expected_ctc_structured"] = {
                "lacs": int(lacs.get("value", 0) or 0),
                "thousands": int(thousands.get("value", 0) or 0),
                "total_annual": int(lacs.get("value", 0) or 0) * 100000 + int(thousands.get("value", 0) or 0) * 1000,
            }

        # --- Similar companies to follow ---
        sim_companies = db.get("similarCompToFollow", [])
        if sim_companies:
            result["recommended_companies"] = [
                {
                    "name": c.get("name"),
                    "rating": c.get("rating"),
                    "reviews_count": (c.get("reviews", {}) or {}).get("count"),
                }
                for c in sim_companies[:5] if isinstance(c, dict)
            ]

        # --- Feature flags ---
        result["feature_flags"] = {
            "ai_mock_interview": db.get("eligibleFlagForAIMockInterview"),
            "ai_resume": db.get("isAIResumeEligible"),
            "job_search_status": (db.get("jbSearchStatus", {}).get("data", [{}]) or [{}])[0].get("value") if isinstance(db.get("jbSearchStatus"), dict) else None,
        }

        # --- Strip None values to keep response clean ---
        result = {k: v for k, v in result.items() if v is not None}
        # Always include status
        result["status"] = "success"

        return result
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e), "error_code": "API_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get dashboard: {type(e).__name__}: {e}", "error_code": "API_ERROR"}
