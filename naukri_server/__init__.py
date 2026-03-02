"""Naukri.com Job Automation MCP Server — Tier 15 (43 tools, 283 tests)

Quick Start for AI consumers:
  1. naukri_auth(action="login|verify_otp|status") → authenticate first
  2. naukri_daily_brief → morning dashboard (16 parallel sources + early access deltas)
  3. naukri_search_jobs / naukri_get_recommendations → find jobs (REST-first + browser fallback)
  4. naukri_smart_apply(job_id) → fit assessment before applying
  5. naukri_smart_apply(action="bulk_saved") → score all saved jobs against profile
  6. naukri_salary_benchmark(keywords) → market salary analysis + your positioning
  7. naukri_apply / naukri_batch_apply → apply to jobs
  8. naukri_compare_jobs([id1, id2]) → side-by-side comparison
  9. naukri_mock_interview(action="prep", job_id) → interview preparation guide
  10. naukri_applications(action="follow_up") → stale app follow-up suggestions
  11. naukri_inbox(action="list|read|mark_interested|accept_nvite") → recruiter messages
  12. naukri_research_company / naukri_company_intel → company intel
  13. naukri_skill_gap_analysis(include_assessments=True) → skill gap with assessment boosts
  14. naukri_reminders(action="list", include_app_status=True) → reminders + live app status

Consolidated tools (action-parameter pattern):
  - naukri_auth(action="login|verify_otp|status")
  - naukri_profile(action="get|update|audit|boost")
  - naukri_inbox(action="list|read|mark_interested|accept_nvite")
  - naukri_performance(metric="impressions|recruiter_activity|activity_level")
  - naukri_mock_interview(action="topics|history|start|answer|prep")
  - naukri_smart_apply(action="bulk_saved") or naukri_smart_apply(job_id=...)
  - naukri_company_intel(company, intel_type="salary|reviews|interviews")
  - naukri_company(action="search|jobs|slug")
  - naukri_applications(action="list|detail|purge|stale|follow_up")
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
  - naukri_settings(action="get|update|blocked_companies|check_email|visibility|notification_prefs")
  - naukri_job_alerts(action="list|detail|create|update|delete")

Tier 15 additions:
  - CLAUDE.md developer guide, shared file I/O utils (load_json_with_backup, save_json_atomic)
  - REST-first search with browser fallback
  - naukri_salary_benchmark: market salary aggregation + CTC positioning
  - Interview prep action, application follow-up action
  - Data pipelines: assessments→skill_gap, saved_jobs→smart_apply, reminders↔app_status, early_access tracking

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
from naukri_server.tools import auth, search, jobs, apply, profile, debug, tracking, sync, inbox, notifications, settings, alerts, companies, performance, assessments, subscription, mock_interview, resume_photo, early_access, resume_builder, ambitionbox, health, insights, research, daily_brief, smart_apply, compare, auto_hunt, skill_gap, export, resume_tailor, reminders  # noqa: E402, F401
