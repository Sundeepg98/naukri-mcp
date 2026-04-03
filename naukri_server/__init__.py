"""Naukri.com Job Automation MCP Server — Tier 22 (26 tools, 1010+ tests)

Quick Start for AI consumers:
  1. naukri_auth(action="login|verify_otp|status") → authenticate first
  2. naukri_daily_brief → morning dashboard (16 sources + recommended actions)
  3. naukri_search_jobs / naukri_get_recommendations → find jobs
  4. naukri_smart_apply(job_id) → fit assessment before applying
  5. naukri_smart_apply(action="apply_top_fits") → score saved jobs + auto-apply top fits
  6. naukri_applications(action="apply", set_reminder_days=7) → apply with auto-reminder
  7. naukri_applications(action="batch_apply", keywords=...) → bulk apply with reminders
  8. naukri_jobs(action="similar|compare") → similar jobs + side-by-side comparison
  9. naukri_jobs(action="get|bulk|detail_v1|report_fraud") → job details + V1 detail + bulk fetch + fraud reporting
  10. naukri_sync(entity="applications|saved_jobs|export") → sync + export
  11. naukri_insights(insight_type="applications|salary|cached_answers|match_analytics|skill_gap|salary_benchmark")
  12. naukri_inbox(action="list|read|mark_interested|accept_nvite") → recruiter messages (REST API)
  13. naukri_company(action="search|jobs|slug|research|follow_status|follow|unfollow") / naukri_company_intel → company intel
  14. naukri_profile(action="dashboard") → profile dashboard + assessments + feature flags
  15. naukri_settings(action="subscription") → subscription status + consent flags
  16. naukri_resume_builder(action="templates|status|tailor") → resume building + tailoring
  17. naukri_debug(action="browser_*|api_*|discover_*") → debugging tools

Consolidated tools (action-parameter pattern — 21 dispatchers):
  - naukri_auth(action="login|verify_otp|status")
  - naukri_profile(action="get|update|audit|boost|dashboard")
  - naukri_inbox(action="list|read|mark_interested|accept_nvite")
  - naukri_performance(metric="impressions|recruiter_activity|activity_level")
  - naukri_mock_interview(action="topics|history|start|answer|prep")
  - naukri_smart_apply(action="bulk_saved|apply_top_fits") or naukri_smart_apply(job_id=...)
  - naukri_company_intel(company, intel_type="salary|reviews|interviews")
  - naukri_company(action="search|jobs|slug|research|follow_status|follow|unfollow")
  - naukri_applications(action="list|detail|purge|stale|follow_up|apply|batch_apply")
  - naukri_saved_jobs(action="list|save|unsave")
  - naukri_early_access(action="list|share")
  - naukri_profile_media(media_type="resume|photo", action="info|upload|download|delete")
  - naukri_insights(insight_type="applications|salary|cached_answers|match_analytics|skill_gap|salary_benchmark|taxonomy")
  - naukri_reminders(action="list|set")
  - naukri_sync(entity="applications|saved_jobs|export")
  - naukri_resume_builder(action="templates|status|tailor")
  - naukri_notifications(action="list|count|mark_read|mark_all_read|summary")
  - naukri_settings(action="get|update|blocked_companies|check_email|visibility|notification_prefs|subscription")
  - naukri_job_alerts(action="list|detail|create|update|delete")
  - naukri_jobs(action="get|report_fraud|similar|compare")
  - naukri_debug(action="browser_*|api_*|discover_*")

Tier 22 changes (performance widget headers fix):
  - naukri_performance(metric="impressions|activity_level") now use appid:109 widget headers
  - Search keyword frequency API now uses widget headers for faster response times

Tier 17 changes (from 38→30 tools):
  - Consolidated: naukri_get_dashboard→naukri_profile(action="dashboard"),
    naukri_get_subscription_status→naukri_settings(action="subscription"),
    naukri_get_similar_jobs+naukri_compare_jobs→naukri_jobs(action="similar|compare"),
    naukri_research_company→naukri_company(action="research"),
    naukri_resume_tailor→naukri_resume_builder(action="tailor"),
    naukri_skill_gap_analysis+naukri_salary_benchmark→naukri_insights(insight_type="skill_gap|salary_benchmark")
  - Internal callers updated to use private helpers (_get_dashboard, _get_subscription_status)
  - Backward-compat aliases preserved for all renamed functions

Tier 18 changes (from 30→26 tools):
  - Consolidated: naukri_company_follow→naukri_company(action="follow_status|follow|unfollow"),
    naukri_debug_browser+naukri_debug_api+naukri_debug_discovery→naukri_debug(action="browser_*|api_*|discover_*"),
    naukri_assessments removed as MCP tool (still callable via backward-compat alias)
  - Backward-compat aliases preserved for all renamed functions

For debugging: naukri_health_check, naukri_debug
"""

import asyncio
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from naukri_server.api import close_api_session
from naukri_server.browser import browser

# Ref-counted lifespan: browser starts once, stops when last session ends.
# Needed for dual transport where stdio + HTTP sessions share one browser.
_lifespan_refs = 0
_lifespan_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(server):
    global _lifespan_refs
    async with _lifespan_lock:
        _lifespan_refs += 1
        if _lifespan_refs == 1:
            await browser.start()
    try:
        yield
    finally:
        async with _lifespan_lock:
            _lifespan_refs -= 1
            if _lifespan_refs == 0:
                try:
                    await browser.stop()
                except Exception:
                    pass
                await close_api_session()


mcp = FastMCP("naukri", lifespan=lifespan)

# Import tool modules to register @mcp.tool() decorators
from naukri_server.tools import auth, search, jobs, apply, profile, debug, tracking, saved_jobs, analytics, sync, inbox, notifications, settings, alerts, companies, performance, assessments, subscription, mock_interview, resume_photo, early_access, resume_builder, ambitionbox, health, insights, research, daily_brief, smart_apply, compare, auto_hunt, skill_gap, export, resume_tailor, reminders  # noqa: E402, F401
