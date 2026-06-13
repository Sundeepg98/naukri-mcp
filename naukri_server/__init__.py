"""Naukri.com Job Automation MCP Server — atomic single-purpose tool design.

Atomic tools are loaded progressively by Claude Code's Tool Search (default since Jan 2026),
so a large catalog of focused tools is cheaper than a small catalog of multi-purpose dispatchers.

Quick Start for AI consumers:
  1. Auth: naukri_login → naukri_verify_otp (if needed) → naukri_auth_status
  2. naukri_daily_brief → morning dashboard (16 sources + recommended actions)
  3. naukri_search_jobs / naukri_get_recommendations → find jobs
  4. naukri_assess_fit(job_id) → fit assessment before applying
  5. naukri_apply_top_fits() → score saved jobs + auto-apply top fits
  6. naukri_apply(job_id, set_reminder_days=7) → apply with auto-reminder
  7. naukri_batch_apply(keywords=...) → bulk apply with reminders
  8. naukri_similar_jobs / naukri_compare_jobs → similar jobs + side-by-side comparison
  9. naukri_get_job / naukri_bulk_fetch_jobs / naukri_job_detail_v1 / naukri_report_fraud
  10. naukri_sync_applications / naukri_sync_saved / naukri_export_data
  11. naukri_application_insights / naukri_salary_position / naukri_cached_answers /
     naukri_match_analytics / naukri_skill_gap / naukri_salary_benchmark / naukri_taxonomy /
     naukri_profile_prompts / naukri_conversion_funnel / naukri_status_changes
  12. Inbox: naukri_list_inbox / naukri_read_message / naukri_mark_interested / naukri_accept_nvite
  13. Companies: naukri_search_companies / naukri_company_jobs / naukri_company_slug /
     naukri_research_company / naukri_follow_company / naukri_follow_status / naukri_company_intel
  14. Profile: naukri_get_profile / naukri_update_profile / naukri_audit_profile /
     naukri_boost_profile / naukri_dashboard / naukri_profile_targeting
  15. Resume/Photo: naukri_resume_info / naukri_upload_resume / naukri_download_resume /
     naukri_photo_info / naukri_upload_photo / naukri_delete_photo
  16. naukri_settings(action=...) → settings management (action-parameter still used here)
  17. naukri_resume_builder(action="templates|status|tailor") → resume building + tailoring
  18. naukri_debug(action="browser_*|api_*|discover_*") → debugging tools

Remaining action-parameter dispatchers (kept where actions truly differ in shape or are
real workflows): naukri_settings, naukri_resume_builder, naukri_debug, naukri_job_alerts,
naukri_company_intel, naukri_mock_interview, naukri_early_access, naukri_agent, naukri_scheduler.

For debugging: naukri_health_check, naukri_debug
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from naukri_server.api import close_api_session
# Import the browser singleton under an alias rather than binding the bare name
# `browser` on the package. Binding `browser` here shadows the submodule
# `naukri_server.browser` on the package namespace, which breaks
# `mock.patch("naukri_server.browser.browser")` on Python 3.10 (its
# unittest.mock._dot_lookup resolves the package attribute to this instance
# instead of the submodule; 3.11+ resolves the submodule and is unaffected).
from naukri_server.browser import browser as _browser_instance
from naukri_server.database import init_db
import naukri_server.browser_watchdog as _watchdog_module
from naukri_server.browser_watchdog import BrowserWatchdog

logger = logging.getLogger(__name__)

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
            await _browser_instance.start()
            await init_db()
            # Start browser watchdog for self-healing
            _watchdog_module.watchdog = BrowserWatchdog(check_interval=30.0, max_restart_attempts=3)
            await _watchdog_module.watchdog.start()
            # Import and start probe scheduler
            import naukri_server.health.probes  # noqa: F401 — triggers probe registration
            from naukri_server.health import probe_registry, HealthProbeScheduler
            import naukri_server.health as _health_module
            _health_module._scheduler = HealthProbeScheduler(probe_registry, watchdog=_watchdog_module.watchdog)
            await _health_module._scheduler.start()
            # Start task scheduler for autonomous background operations
            from naukri_server.scheduler import TaskScheduler
            from naukri_server.scheduler_tasks import register_all as register_scheduler_tasks
            import naukri_server.scheduler as _scheduler_module
            _scheduler_module.scheduler = TaskScheduler()
            register_scheduler_tasks(_scheduler_module.scheduler)
            await _scheduler_module.scheduler.start()
            # Startup health check — validates core services after browser is ready
            logger.info("Running startup health check...")
            try:
                from naukri_server.tools.health import naukri_health_check
                health = await naukri_health_check(include_browser=False)
                if health.get("summary", {}).get("fail", 0) > 0:
                    logger.warning("Startup health check has failures: %s", health.get("summary"))
                else:
                    logger.info("Startup health check passed: %s", health.get("summary"))
            except Exception as e:
                logger.warning("Startup health check failed: %s (continuing anyway)", e)
    try:
        yield
    finally:
        async with _lifespan_lock:
            _lifespan_refs -= 1
            if _lifespan_refs == 0:
                # Stop task scheduler
                import naukri_server.scheduler as _scheduler_module
                if _scheduler_module.scheduler:
                    await _scheduler_module.scheduler.stop()
                import naukri_server.health as _health_module
                if hasattr(_health_module, '_scheduler') and _health_module._scheduler:
                    await _health_module._scheduler.stop()
                if _watchdog_module.watchdog:
                    await _watchdog_module.watchdog.stop()
                try:
                    await _browser_instance.stop()
                except Exception:
                    pass
                await close_api_session()


# --- Remote auth wiring (opt-in via env vars) ---------------------------------
# Stdio mode and existing local HTTP usage are unaffected: when neither
# MCP_SHARED_SECRET nor MCP_OAUTH_ENABLED is set, FastMCP starts without auth.
#
# Modes:
#   1. Neither env var set     → no auth (current local behavior)
#   2. MCP_SHARED_SECRET only  → bearer-only (Claude Code CLI / Desktop)
#   3. MCP_OAUTH_ENABLED=1     → OAuth provider (also accepts shared secret if
#                                MCP_SHARED_SECRET is set, so single server
#                                handles both Claude.ai web AND Claude Code)
import os as _os
from typing import Any as _Any

from pydantic import AnyHttpUrl as _AnyHttpUrl

from naukri_server.auth.bearer_verifier import build_verifier_from_env as _build_bearer

# Explicit annotation: FastMCP kwargs can be any type (lifespan callable,
# auth provider instance, settings object, ...) — declare as dict[str, Any]
# so mypy/pyright don't infer a narrow type from the first key.
_fastmcp_kwargs: dict[str, _Any] = {"lifespan": lifespan}
_bearer_verifier = _build_bearer()  # may raise ValueError if secret too short
_oauth_enabled = _os.environ.get("MCP_OAUTH_ENABLED", "").strip() == "1"
_oauth_provider = None  # set below if OAuth enabled (referenced for consent route)

if _oauth_enabled:
    # OAuth mode — provider handles BOTH OAuth flow AND shared-secret bearer
    from mcp.server.auth.settings import (
        AuthSettings,
        ClientRegistrationOptions,
        RevocationOptions,
    )
    from naukri_server.auth.oauth_provider import build_oauth_provider_from_env

    _oauth_provider = build_oauth_provider_from_env(bearer_verifier=_bearer_verifier)
    _public_url = _os.environ.get("MCP_PUBLIC_URL", "http://localhost:8321").rstrip("/")
    _public_url_obj = _AnyHttpUrl(_public_url)
    _fastmcp_kwargs["auth_server_provider"] = _oauth_provider
    _fastmcp_kwargs["auth"] = AuthSettings(
        issuer_url=_public_url_obj,
        resource_server_url=_public_url_obj,
        required_scopes=["naukri:full"],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["naukri:full"],
            default_scopes=["naukri:full"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    logger.info("Auth: OAuth provider enabled (issuer=%s, bearer-fallback=%s)",
                _public_url, "yes" if _bearer_verifier else "no")
elif _bearer_verifier is not None:
    # Bearer-only mode — TokenVerifier alone, no OAuth flow
    from mcp.server.auth.settings import AuthSettings

    _public_url = _os.environ.get("MCP_PUBLIC_URL", "http://localhost:8321").rstrip("/")
    _public_url_obj = _AnyHttpUrl(_public_url)
    _fastmcp_kwargs["token_verifier"] = _bearer_verifier
    _fastmcp_kwargs["auth"] = AuthSettings(
        issuer_url=_public_url_obj,
        resource_server_url=_public_url_obj,
        required_scopes=["naukri:full"],
    )
    logger.info("Auth: bearer-only mode (resource=%s)", _public_url)
else:
    logger.info("Auth: disabled (no MCP_SHARED_SECRET / MCP_OAUTH_ENABLED set)")

mcp = FastMCP("naukri", **_fastmcp_kwargs)

# Register OAuth consent UI route only when OAuth is enabled and not in
# auto-approve mode (otherwise the consent endpoint is unreachable).
if _oauth_enabled and _oauth_provider is not None and not _oauth_provider._auto_approve:
    from naukri_server.auth import consent_route as _consent_route
    _consent_route.register(mcp, _oauth_provider)

# Import tool modules to register @mcp.tool() decorators
from naukri_server.tools import auth, search, jobs, apply, profile, profile_update, profile_targeting, debug, tracking, saved_jobs, analytics, sync, inbox, notifications, settings, alerts, companies, performance, assessments, subscription, mock_interview, resume_photo, early_access, resume_builder, ambitionbox, ambitionbox_rest, health, insights, research, daily_brief, smart_apply, compare, auto_hunt, skill_gap, export, resume_tailor, reminders, scheduler_tool, agent_tool  # noqa: E402, F401
from naukri_server import subscribers  # noqa: F401 — registers event handlers
from naukri_server import resources  # noqa: F401 — registers @mcp.resource() handlers
from naukri_server import prompts  # noqa: F401 — registers @mcp.prompt() handlers
from naukri_server import probes  # noqa: F401 — registers api_validator @health_probe
from naukri_server.dashboard import routes  # noqa: F401 — registers @mcp.custom_route() endpoints
