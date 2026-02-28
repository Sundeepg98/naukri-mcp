"""Naukri.com Job Automation MCP Server v5 (58 tools) — package entry point."""

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
from naukri_server.tools import auth, search, jobs, apply, profile, debug, tracking, upload, sync, inbox, notifications, settings, alerts, companies, performance, assessments, subscription, mock_interview, extras, resume_photo, early_access, resume_builder, ambitionbox, health  # noqa: E402, F401
