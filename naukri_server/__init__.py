"""Naukri.com Job Automation MCP Server — Tier 13 (49 tools)

Quick Start for AI consumers:
  1. naukri_login / naukri_get_login_status → authenticate first
  2. naukri_daily_brief → morning dashboard (inbox, notifications, recommendations, activity)
  3. naukri_search_jobs / naukri_get_recommendations → find jobs
  4. naukri_smart_apply(job_id) → fit assessment before applying
  5. naukri_apply / naukri_batch_apply → apply to jobs
  6. naukri_compare_jobs([id1, id2]) → side-by-side comparison
  7. naukri_inbox(action="list|read|mark_interested|accept_nvite") → recruiter messages
  8. naukri_sync(entity="applications|saved_jobs") → sync tracking data
  9. naukri_get_applications / naukri_saved_jobs(action="list|save|unsave") → track
  10. naukri_research_company / naukri_company_intel(intel_type="salary|reviews|interviews") → company intel
  11. naukri_profile(action="get|update|audit|boost") → profile management
  12. naukri_get_dashboard → profile analytics

Consolidated tools (action-parameter pattern):
  - naukri_profile(action="get|update|audit|boost")
  - naukri_inbox(action="list|read|mark_interested|accept_nvite")
  - naukri_performance(metric="impressions|recruiter_activity|activity_level")
  - naukri_mock_interview(action="topics|history|start|answer")
  - naukri_company_intel(company, intel_type="salary|reviews|interviews")
  - naukri_saved_jobs(action="list|save|unsave")
  - naukri_early_access(action="list|share")
  - naukri_profile_media(media_type="resume|photo", action="info|upload|download|delete")
  - naukri_insights(insight_type="applications|salary|cached_answers")
  - naukri_company_follow(action="status|follow|unfollow")
  - naukri_reminders(action="list|set")
  - naukri_sync(entity="applications|saved_jobs")
  - naukri_resume_builder(action="templates|status")
  - naukri_assessments(action="list|completeness")
  - naukri_notifications(action="list|count|mark_read|mark_all_read")
  - naukri_settings(action="get|update|blocked_companies|check_email")
  - naukri_job_alerts(action="list|detail|create|update|delete")

For debugging: naukri_health_check, naukri_debug_browser/api/discovery
"""

from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from naukri_server.browser import browser


@asynccontextmanager
async def lifespan(server):
    await browser.start()
    try:
        yield
    finally:
        await browser.stop()


mcp = FastMCP("naukri", lifespan=lifespan)

# Import tool modules to register @mcp.tool() decorators
from naukri_server.tools import auth, search, jobs, apply, profile, debug, tracking, upload, sync, inbox, notifications, settings, alerts, companies, performance, assessments, subscription, mock_interview, resume_photo, early_access, resume_builder, ambitionbox, health, insights, research, daily_brief, smart_apply, compare, auto_hunt, skill_gap, export, resume_tailor, reminders  # noqa: E402, F401
