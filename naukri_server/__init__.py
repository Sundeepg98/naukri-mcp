"""Naukri.com Job Automation MCP Server v5 (68 tools) — package entry point.

Quick Start for AI consumers:
  1. naukri_login / naukri_get_login_status (authenticate first)
  2. naukri_search_jobs / naukri_get_recommendations (find jobs)
  3. naukri_get_job (full details) → naukri_apply (apply)
  4. naukri_sync_applications → naukri_get_applications (track)
  5. naukri_research_company (unified company intel)
  6. naukri_audit_profile → naukri_update_profile (optimize profile)

For analytics: naukri_get_application_insights, naukri_analyze_salary_position
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
from naukri_server.tools import auth, search, jobs, apply, profile, debug, tracking, upload, sync, inbox, notifications, settings, alerts, companies, performance, assessments, subscription, mock_interview, resume_photo, early_access, resume_builder, ambitionbox, health, insights, research  # noqa: E402, F401
