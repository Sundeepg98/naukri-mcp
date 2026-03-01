"""Naukri.com Job Automation MCP Server v10 (73 tools) — package entry point.

Quick Start for AI consumers:
  1. naukri_login / naukri_get_login_status (authenticate first)
  2. naukri_daily_brief (morning dashboard — 11 checks including stale detection + reminders)
  3. naukri_auto_hunt (one-call job hunting with fit scoring)
  4. naukri_smart_apply (fit assessment) → naukri_apply (apply)
  5. naukri_compare_jobs (side-by-side comparison with fit scores)
  6. naukri_accept_nvite (respond to recruiter NVites)
  7. naukri_sync_applications → naukri_get_stale_applications (track + detect stale)
  8. naukri_set_reminder / naukri_get_reminders (follow-up scheduling)
  9. naukri_research_company / naukri_get_interview_experiences (company intel)
 10. naukri_resume_tailor → naukri_update_profile (optimize profile)
 11. naukri_resume(action="download") (download your resume)

Consolidated tools (use action parameter):
  - naukri_notifications(action="list|count|mark_read|mark_all_read")
  - naukri_job_alerts(action="list|detail|create|update|delete")
  - naukri_cached_answers(action="list|update|delete")
  - naukri_resume(action="info|download|upload")
  - naukri_photo(action="info|upload|delete")
  - naukri_settings(action="get|update|blocked_companies|check_email")

For automation: naukri_auto_hunt, naukri_skill_gap_analysis, naukri_batch_apply
For analytics: naukri_get_application_insights, naukri_analyze_salary_position
For export: naukri_export_data (JSON/CSV)
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
