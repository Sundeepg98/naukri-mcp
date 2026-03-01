"""Naukri.com Job Automation MCP Server v9 (86 tools) — package entry point.

Quick Start for AI consumers:
  1. naukri_login / naukri_get_login_status (authenticate first)
  2. naukri_daily_brief (morning dashboard — inbox, notifications, recommendations, dashboard stats, early access)
  3. naukri_auto_hunt (one-call job hunting with fit scoring, filters already-applied)
  4. naukri_smart_apply (fit assessment with configurable threshold) → naukri_apply (apply)
  5. naukri_compare_jobs (side-by-side comparison with fit scores + apply status)
  6. naukri_accept_nvite (respond to recruiter NVites)
  7. naukri_sync_applications → naukri_get_applications (track)
  8. naukri_research_company / naukri_get_interview_experiences (company intel)
  9. naukri_resume_tailor → naukri_update_profile (optimize profile)
 10. naukri_download_resume (download your resume to disk)
 11. naukri_start_mock_interview → naukri_answer_mock_question (AI interview practice)
 12. naukri_share_interest (early access roles)
 13. naukri_purge_applications (clean up old local tracking data)

For automation: naukri_auto_hunt, naukri_skill_gap_analysis, naukri_batch_apply
For analytics: naukri_get_application_insights, naukri_analyze_salary_position
For export: naukri_export_data (JSON/CSV)
For cache management: naukri_review_cached_answers, naukri_delete/update_cached_answer
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
from naukri_server.tools import auth, search, jobs, apply, profile, debug, tracking, upload, sync, inbox, notifications, settings, alerts, companies, performance, assessments, subscription, mock_interview, resume_photo, early_access, resume_builder, ambitionbox, health, insights, research, daily_brief, smart_apply, compare, auto_hunt, skill_gap, export, resume_tailor  # noqa: E402, F401
