"""Profile service — business logic for profile fetch, caching, dashboard, and audit."""

from typing import Optional

from naukri_server.api import NaukriAPIError
from naukri_server.domain import safe_get
from naukri_server.domain.profile import ProfileParser
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
    "do_update",
    "do_boost",
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

        profile = safe_get(data, "profile", default=[{}])[0]
        additional = safe_get(data, "profileAdditional", default={})

        skills = ProfileParser.extract_skills(data)

        employment = ProfileParser.extract_employment(data)

        education = ProfileParser.extract_education(data)

        exp = safe_get(profile, "experience", default={})

        ctc = ProfileParser.extract_ctc(profile)
        result = {
            "status": "success",
            "name": safe_get(profile, "name", field_name="name", warn=True, context="profile"),
            "resume_headline": ProfileParser.extract_resume_headline(profile),
            "current_ctc": ctc["current_ctc"],
            "expected_ctc": ctc["expected_ctc"],
            "notice_period": ProfileParser.extract_notice_period(profile),
            "total_experience": f"{safe_get(exp, 'year', default=0)} years {safe_get(exp, 'month', default=0)} months",
            "current_location": safe_get(safe_get(profile, "city", default={}), "value", field_name="current_location", warn=True, context="profile"),
            "gender": {"M": "Male", "F": "Female"}.get(safe_get(profile, "gender", default=""), safe_get(profile, "gender")),
            "key_skills": safe_get(profile, "keySkills", field_name="key_skills", warn=True, context="profile"),
            "summary": safe_get(profile, "summary"),
            "certifications": safe_get(data, "certifications", default=[]),
            "projects": safe_get(data, "projects", default=[]),
            "languages": safe_get(data, "languages", default=[]),
            "online_profiles": safe_get(data, "onlineProfiles", default=[]),
            "skills_with_experience": skills,
            "employment": employment,
            "education": education,
            "profile_id": safe_get(additional, "profileId", field_name="profile_id", warn=True, context="profile"),
        }

        # --- Communication settings (notification preferences) ---
        comm_parsed = ProfileParser.extract_communication_settings(data, profile)
        if comm_parsed:
            result["communication_settings"] = comm_parsed

        # --- Resdex visibility (profile visibility flags) ---
        resdex_parsed = ProfileParser.extract_resdex_visibility(data, additional, profile)
        if resdex_parsed is not None:
            result["resdex_visibility"] = resdex_parsed

        # --- lookupData (resume score, login times, subscription flags) ---
        lookup = safe_get(data, "lookupData", default={})
        if lookup:
            result["lookup_data"] = {
                "resume_score": safe_get(lookup, "resumeScore", field_name="resume_score", warn=True, context="lookupData"),
                "last_login": safe_get(lookup, "lastLoginTime"),
                "prev_login": safe_get(lookup, "prevLoginTime"),
                "has_current_employment": safe_get(lookup, "hasCurrentFullTimeEmployment"),
                "is_paid_user": safe_get(lookup, "isPaidUser"),
                "is_agent_eligible": safe_get(lookup, "isJobseekerAgentEligible"),
                "int360_expiry": safe_get(lookup, "int360RoleExp"),
                "ff_rd_expiry": safe_get(lookup, "ffRDSubExp"),
            }

        # --- additionalDetails (AI feature eligibility) ---
        additional_details = safe_get(data, "additionalDetails", default={})
        if additional_details:
            result["ai_features"] = {
                "is_ai_resume_eligible": safe_get(additional_details, "isAIResumeEligible"),
                "employer_verification_eligible": safe_get(additional_details, "curEmpVerEligibility"),
            }

        # --- extendedProfile (job search status, career break, stale tags) ---
        ext = safe_get(data, "extendedProfile", default={})
        if ext:
            jb_status = safe_get(ext, "jbSearchStatus", default={})
            jb_status_data = safe_get(jb_status, "data")
            jb_data = (jb_status_data or [{}])[0] if isinstance(jb_status_data, list) else {}

            career_break = safe_get(ext, "careerBreak", default={})
            cb_raw = safe_get(career_break, "data")
            cb_data = (cb_raw or [{}])[0] if isinstance(cb_raw, list) else {}

            tags_data = safe_get(safe_get(ext, "tags", default={}), "data", default=[])
            stale_tags = [safe_get(t, "value") for t in tags_data if isinstance(t, dict) and safe_get(safe_get(t, "meta", default={}), "status") == "inactive"]

            result["extended_profile"] = {
                "job_search_status": safe_get(jb_data, "value"),
                "career_break": safe_get(cb_data, "comingFromBreak", default=False),
                "stale_tags": stale_tags,
            }

        # --- schools (10th/12th education) ---
        schools = safe_get(data, "schools", default=[])
        if schools:
            result["schools"] = [
                {
                    "level": (safe_get(safe_get(s, "educationType", default={}), "value") or f"Class {safe_get(s, 'schoolLevel')}"),
                    "board": (safe_get(safe_get(s, "schoolBoard", default={}), "value") or ""),
                    "year": safe_get(s, "schoolCompletionYear"),
                    "percentage_range": (safe_get(safe_get(s, "schoolPercentage", default={}), "value") or ""),
                    "medium": (safe_get(safe_get(s, "schoolMedium", default={}), "value") or ""),
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

        # --- Emit ProfileScoreChanged if we have a completeness score ---
        try:
            from naukri_server.events import event_bus, ProfileScoreChanged
            if report.completeness_pct is not None:
                await event_bus.emit(ProfileScoreChanged(old_score=0, new_score=report.completeness_pct))
        except Exception:
            pass

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
        db = safe_get(data, "dashBoard", default={})

        # --- Core fields (existing) ---
        result = {
            "status": "success",
            "profile_views": safe_get(db, "profileViewCount", field_name="profile_views", warn=True, context="dashboard"),
            "recruiter_activity_date": safe_get(db, "recruiterActionsLatestDate", field_name="recruiter_activity_date", warn=True, context="dashboard"),
            "ctc_lpa": safe_get(db, "rawCtc"),
            "experience_years": safe_get(db, "rawTotalExperience"),
            "unread_invites": safe_get(db, "unreadPowerNvite"),
            "total_invites": safe_get(db, "totalPowerNvite"),
            "unread_relevant_mail": safe_get(db, "unreadMostRelevantMail"),
            "has_inbox": safe_get(db, "hasInboxFlag") == "Y",
            "total_matches": safe_get(db, "mrt"),
        }

        # --- Application stats ---
        result["applied_count"] = safe_get(db, "appliedCount", "totalApplied")
        result["applied_today"] = safe_get(db, "appliedToday")
        result["applied_this_week"] = safe_get(db, "appliedThisWeek")

        # --- Profile completeness and score ---
        result["profile_completion"] = ProfileParser.extract_dashboard_profile_completeness(db)
        result["profile_score"] = safe_get(db, "profileScore", "profileRank", field_name="profile_score", warn=True, context="dashboard")
        result["profile_quality"] = safe_get(db, "profileQuality")
        result["profile_strength"] = safe_get(db, "profileStrength")

        # --- Saved jobs ---
        result["saved_jobs_count"] = safe_get(db, "savedJobsCount", "totalSavedJobs")

        # --- Resume details ---
        resume_details = ProfileParser.extract_dashboard_resume(db)
        result["resume_name"] = resume_details["resume_name"]
        result["resume_last_updated"] = resume_details["resume_last_updated"]
        result["resume_headline"] = resume_details["resume_headline"]
        result["resume_id"] = resume_details["resume_id"]

        # --- User identity ---
        result["profile_title"] = safe_get(db, "profileTitle", "title")
        result["profile_image_url"] = safe_get(db, "profileImageUrl", "imageUrl", "photoUrl")
        result["name"] = safe_get(db, "name", "fullName")
        result["email"] = safe_get(db, "email", "emailId")
        result["phone"] = safe_get(db, "phone", "mobileNo")
        result["user_id"] = safe_get(db, "userId", "resId")

        # --- Current employment context ---
        result["designation"] = safe_get(db, "designation", "currentDesignation")
        result["company"] = safe_get(db, "company", "currentCompany")
        result["location"] = safe_get(db, "location", "city")
        result["notice_period"] = safe_get(db, "noticePeriod")
        result["preferred_locations"] = safe_get(db, "preferredLocations", "locationPref")

        # --- Mail / messaging ---
        result["total_unread_mail"] = safe_get(db, "totalUnreadMail", "unreadMailCount")
        result["unread_direct_mail"] = safe_get(db, "unreadDirectMail")
        result["unread_forwarded_mail"] = safe_get(db, "unreadForwardedMail")

        # --- Search and recruiter analytics ---
        result["search_impressions"] = ProfileParser.extract_dashboard_search_impressions(db)
        result["recruiter_actions_count"] = safe_get(db, "recruiterActionsCount", "totalRecruiterActions")
        result["profile_views_trend"] = safe_get(db, "profileViewsTrend", "viewsTrend")
        result["profile_views_last_week"] = safe_get(db, "profileViewsLastWeek")
        result["profile_views_last_month"] = safe_get(db, "profileViewsLastMonth")

        # --- Verification flags ---
        result["is_profile_verified"] = safe_get(db, "isProfileVerified", "profileVerified")
        result["is_email_verified"] = safe_get(db, "isEmailVerified", "emailVerified")
        result["is_phone_verified"] = safe_get(db, "isPhoneVerified", "mobileVerified")

        # --- Subscription ---
        result["subscription_type"] = safe_get(db, "subscriptionType", "productName")
        result["subscription_expiry"] = safe_get(db, "subscriptionExpiry", "subscriptionEndDate")
        result["is_premium"] = safe_get(db, "isPremium", "premiumUser")

        # --- Job alerts ---
        result["job_alerts_count"] = safe_get(db, "jobAlertsCount", "totalJobAlerts")

        # --- Invite breakdown ---
        result["pending_invites"] = safe_get(db, "pendingNvite", "pendingInvites")
        result["accepted_invites"] = safe_get(db, "acceptedNvite", "acceptedInvites")
        result["declined_invites"] = safe_get(db, "declinedNvite", "declinedInvites")

        # --- Resdex / visibility ---
        result["resdex_visible"] = safe_get(db, "resdexVisible", "showProfile")
        result["active_status"] = safe_get(db, "activeStatus", "activityStatus")
        result["last_active"] = safe_get(db, "lastActive", "lastLoginDate")

        # --- Recommendations ---
        result["recommended_jobs_count"] = safe_get(db, "recommendedJobsCount", "totalRecommendedJobs")

        # --- Assessments ---
        assessments_raw = safe_get(db, "assessments", default=[])
        if assessments_raw:
            result["assessments"] = [
                {
                    "skill": safe_get(a, "skill"),
                    "level": (safe_get(safe_get(a, "level", default={}), "name") if isinstance(safe_get(a, "level"), dict) else None),
                    "question_count": safe_get(a, "questionCount"),
                    "duration_mins": safe_get(a, "duration"),
                    "max_attempts": safe_get(a, "maxAttempts"),
                    "test_id": safe_get(a, "testId"),
                    "results": {
                        "score_percent": safe_get(safe_get(a, "results", default={}) or {}, "scorePercent"),
                        "rank": safe_get(safe_get(a, "results", default={}) or {}, "rank"),
                        "status": safe_get(safe_get(a, "results", default={}) or {}, "status"),
                    } if safe_get(a, "results") else None,
                }
                for a in assessments_raw if isinstance(a, dict)
            ]

        # --- Expected CTC structured ---
        expected_ctc = safe_get(db, "expectedCtc")
        if isinstance(expected_ctc, dict):
            lacs = safe_get(expected_ctc, "lacs", default={})
            thousands = safe_get(expected_ctc, "thousands", default={})
            lacs_val = int(safe_get(lacs, "value", default=0) or 0)
            thousands_val = int(safe_get(thousands, "value", default=0) or 0)
            result["expected_ctc_structured"] = {
                "lacs": lacs_val,
                "thousands": thousands_val,
                "total_annual": lacs_val * 100000 + thousands_val * 1000,
            }

        # --- Similar companies to follow ---
        sim_companies = safe_get(db, "similarCompToFollow", default=[])
        if sim_companies:
            result["recommended_companies"] = [
                {
                    "name": safe_get(c, "name"),
                    "rating": safe_get(c, "rating"),
                    "reviews_count": safe_get(safe_get(c, "reviews", default={}) or {}, "count"),
                }
                for c in sim_companies[:5] if isinstance(c, dict)
            ]

        # --- Feature flags ---
        jb_search = safe_get(db, "jbSearchStatus")
        jb_search_data = (safe_get(jb_search, "data", default=[{}]) or [{}])[0] if isinstance(jb_search, dict) else {}
        result["feature_flags"] = {
            "ai_mock_interview": safe_get(db, "eligibleFlagForAIMockInterview"),
            "ai_resume": safe_get(db, "isAIResumeEligible"),
            "job_search_status": safe_get(jb_search_data, "value"),
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


# ---------------------------------------------------------------------------
# Browser-orchestration helpers (delegate to profile_update.py via lazy
# imports so this service module keeps a clean import graph).
# ---------------------------------------------------------------------------

async def do_update(
    fields: Optional[dict] = None,
    notice_period: Optional[str] = None,
    expected_ctc: Optional[float] = None,
    current_ctc: Optional[float] = None,
) -> dict:
    """Orchestrate browser-based profile update with retry + timeout.

    The ``_update_profile`` helper is looked up via the ``tools.profile``
    module so test patches at that path are honored.
    """
    import asyncio
    from naukri_server.browser import browser_retry
    from naukri_server.config import BROWSER_OPERATION_TIMEOUT
    from naukri_server.tools import profile as _profile_tool

    return await asyncio.wait_for(
        browser_retry(
            lambda: _profile_tool._update_profile(
                fields=fields or {},
                notice_period=notice_period,
                expected_ctc=expected_ctc,
                current_ctc=current_ctc,
            ),
            description="profile update",
        ),
        timeout=BROWSER_OPERATION_TIMEOUT,
    )


async def do_boost(randomize: bool = False) -> dict:
    """Orchestrate browser-based profile-boost (re-save headline) with retry + timeout.

    The ``_boost_visibility`` helper is looked up via the ``tools.profile``
    module so test patches at that path are honored.
    """
    import asyncio
    from naukri_server.browser import browser_retry
    from naukri_server.config import BROWSER_OPERATION_TIMEOUT
    from naukri_server.tools import profile as _profile_tool

    return await asyncio.wait_for(
        browser_retry(
            lambda: _profile_tool._boost_visibility(randomize=randomize),
            description="profile boost",
        ),
        timeout=BROWSER_OPERATION_TIMEOUT,
    )
