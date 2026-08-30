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
# Aliased for the same reason `browser` is, one import above: binding the bare
# name `readiness` here would shadow the SUBMODULE `naukri_server.readiness` on
# the package namespace, so `monkeypatch.setattr(naukri_server.readiness, ...)`
# would reach the instance and fail with AttributeError. Measured, not feared -
# it did exactly that on first run of tests/test_startup_warmup.py.
from naukri_server.readiness import (
    PHASE_BROWSER,
    PHASE_MIGRATION,
    PHASE_PROBES,
    PHASE_SCHEDULER,
    PHASE_WATCHDOG,
    WARMUP_SHUTDOWN_GRACE_SECONDS,
    readiness as _readiness,
)

logger = logging.getLogger(__name__)

# Ref-counted lifespan: browser starts once, stops when last session ends.
# Needed for dual transport where stdio + HTTP sessions share one browser.
_lifespan_refs = 0
_lifespan_lock = asyncio.Lock()

#: The detached background warm-up. Held at module scope so shutdown can wait
#: on it -- a fire-and-forget task with no reference is also collectable
#: mid-flight, which for a Chrome launch means an orphan process.
_warmup_task = None


async def _warm_up():
    """Everything expensive, run OFF the lifespan's critical path.

    THE BUG THIS SPLIT FIXES (measured 2026-08-30). Every statement in this
    function used to run INSIDE ``lifespan`` before it yielded. The MCP lifespan
    is entered by ``mcp.server.lowlevel.server.Server.run`` at the top of the
    session, BEFORE it reads the first message off the transport -- so under
    ``--http``, where the session manager starts one ``Server.run`` per MCP
    session, the first client's ``initialize`` request sat unread in the stream
    for the entire duration of this sequence. On the live server that was
    process start 10:01:32 -> browser launch beginning 10:09 -> profile written
    10:11:22: minutes during which the server was healthy, listening, and
    completely unable to answer the one request a client needs answered first.
    The client gave up and dropped all 120 tools.

    (The socket itself was never the problem and is not touched here: uvicorn
    binds after the STARLETTE lifespan, which for a FastMCP app is only
    ``StreamableHTTPSessionManager.run()`` -- a task-group creation. Measured
    time to accept on a spare port: 8.6s, all of it module import.)

    Each phase names itself in ``readiness`` before it runs, so a tool call that
    lands mid-warm-up can report WHAT is still starting instead of guessing.
    A failure marks readiness failed rather than propagating: this runs as a
    detached task, so an unhandled exception here would be swallowed by asyncio
    and every waiting tool call would block for its full budget on a warm-up
    that had already died.
    """
    try:
        _readiness.enter(PHASE_BROWSER)
        # start() swallows its own launch failures and drops to REST-only mode,
        # so this returns either way. browser_up() is therefore released
        # unconditionally: if the browser genuinely failed, waiting longer
        # cannot help, and the honest answer downstream is the pre-existing
        # BROWSER_ERROR ("browser is not running") -- which by then is true.
        await _browser_instance.start()
        _readiness.browser_up()

        _readiness.enter(PHASE_MIGRATION)
        # One-shot JSON -> SQLite import. migrate_json_to_sqlite() has
        # existed and been unit-tested since the SQLite move, but nothing
        # in production ever called it: on 2026-08-20 the DB reported 0
        # applications while applications.json held 151 real records, and
        # six tracking tools were structurally empty as a result.
        # Guarded by the `migrations` ledger, so it is a no-op on every
        # start after the first and cannot resurrect deleted rows.
        try:
            from naukri_server.database import migrate_json_to_sqlite
            _migration = await migrate_json_to_sqlite()
            if _migration.get("status") == "applied":
                logger.info(
                    "JSON -> SQLite migration applied: apps=%s saved=%s reminders=%s rounds=%s",
                    _migration.get("applications"), _migration.get("saved_jobs"),
                    _migration.get("reminders"), _migration.get("interview_rounds"),
                )
        except Exception as e:
            # Never block startup on a data import.
            logger.error("JSON -> SQLite migration failed (continuing): %s", e)

        _readiness.enter(PHASE_WATCHDOG)
        # Start browser watchdog for self-healing
        _watchdog_module.watchdog = BrowserWatchdog(check_interval=30.0, max_restart_attempts=3)
        await _watchdog_module.watchdog.start()

        _readiness.enter(PHASE_PROBES)
        # Import and start probe scheduler
        import naukri_server.health.probes  # noqa: F401 -- triggers probe registration
        from naukri_server.health import probe_registry, HealthProbeScheduler
        import naukri_server.health as _health_module
        _health_module._scheduler = HealthProbeScheduler(probe_registry, watchdog=_watchdog_module.watchdog)
        await _health_module._scheduler.start()

        _readiness.enter(PHASE_SCHEDULER)
        # Start task scheduler for autonomous background operations
        from naukri_server.scheduler import TaskScheduler
        from naukri_server.scheduler_tasks import register_all as register_scheduler_tasks
        import naukri_server.scheduler as _scheduler_module
        _scheduler_module.scheduler = TaskScheduler()
        register_scheduler_tasks(_scheduler_module.scheduler)
        await _scheduler_module.scheduler.start()

        # THE STARTUP HEALTH CHECK IS GONE, AND NOT MOVED. It ran
        # `naukri_health_check(include_browser=False)` and did nothing with the
        # result but log it. Three reasons it does not belong at startup at all,
        # not even here in the background:
        #   1. Nothing consumed it. No branch, no gate, no readiness signal --
        #      one log line, on a server whose stderr goes to a scheduled task's
        #      discarded console.
        #   2. It is redundant by construction. HealthProbeScheduler started two
        #      statements above runs 21 probes continuously; a one-shot sample at
        #      t=0 tells nobody anything the first probe sweep does not.
        #   3. It is the exact shape of the 2026-08-20 wedge documented below in
        #      this file: a STUCK naukri_health_check took an unrelated tool down
        #      with it. Running that call on the startup path put the one known
        #      wedge-prone handler between the server and its own readiness.
        # `naukri_health_check` remains available as an on-demand tool, which is
        # where a health check a caller actually reads belongs.
        _readiness.ready()
    except asyncio.CancelledError:
        # Shutdown cancelled us mid-phase. Not a warm-up failure -- re-raise so
        # the awaiting shutdown path sees a clean cancellation.
        raise
    except Exception as e:
        _readiness.failed(e)


@asynccontextmanager
async def lifespan(server):
    """Fast path only. Everything expensive is handed to ``_warm_up``.

    WHAT STAYS ON THE CRITICAL PATH and why: the ref-count (it is the thing
    being counted), and ``init_db()`` -- measured 0.099s, and the ~40 DB-backed
    tools would otherwise race a table that does not exist yet. Nothing else
    here may grow an await that can take more than milliseconds; if it needs
    one, it belongs in ``_warm_up``.

    REF-COUNTING IS UNCHANGED, and gains a property it did not have: session 2
    used to block on ``_lifespan_lock`` for the whole of session 1's browser
    launch, because session 1 held the lock across it. Now the lock is held for
    milliseconds, so ``--dual`` (stdio + HTTP on one browser) no longer
    serialises its sessions behind a Chrome launch.
    """
    global _lifespan_refs, _warmup_task
    async with _lifespan_lock:
        _lifespan_refs += 1
        if _lifespan_refs == 1:
            await init_db()
            _readiness.begin()
            # Detached on purpose: this task must outlive the lifespan's
            # __aenter__, which is the entire point of the change.
            _warmup_task = asyncio.create_task(_warm_up())
    try:
        yield
    finally:
        async with _lifespan_lock:
            _lifespan_refs -= 1
            if _lifespan_refs == 0:
                # Let an in-flight warm-up land before tearing down what it is
                # still building. Cancelling a half-launched Playwright context
                # and then calling stop() on it is the one ordering that can
                # leave an orphan Chrome holding the profile.
                if _warmup_task is not None and not _warmup_task.done():
                    try:
                        await asyncio.wait_for(
                            _warmup_task, timeout=WARMUP_SHUTDOWN_GRACE_SECONDS
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Warm-up still running after %.0fs at shutdown -- cancelled",
                            WARMUP_SHUTDOWN_GRACE_SECONDS,
                        )
                    except Exception as e:  # already recorded by _warm_up
                        logger.debug("Warm-up ended with %s during shutdown", e)
                _warmup_task = None
                _readiness.reset()
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

# --- Server-wide tool watchdog ----------------------------------------------
# The 2026-08-20 wedge did not stop at its own handler: a stuck
# naukri_health_check took an UNRELATED naukri_activity_level down with it, so
# the blast radius was the whole server. Every tool is therefore registered
# behind a last-resort budget, and a permanent stall surfaces as the ordinary
# error envelope {"status": "error", "error_code": "TIMEOUT"} rather than a
# client that waits forever with nothing to show.
#
# This is a BACKSTOP. TOOL_WATCHDOG_TIMEOUT (600s) sits far above any legitimate
# tool - auto_hunt and batch_apply really do run for minutes - because its job
# is to bound "never", not to police "slow". The load-bearing fixes are the
# per-await bounds in naukri_server/browser.py and the per-check budgets in
# naukri_server/tools/health.py; this only guarantees that anything they miss
# still ends.
#
# Wrapping happens BEFORE the tool modules are imported below, so every
# @mcp.tool() lands inside it. functools.wraps preserves __name__/__doc__ and
# sets __wrapped__, so FastMCP's signature introspection (and therefore every
# tool's input schema) is unchanged.
import inspect as _inspect  # noqa: E402

from naukri_server.utils import tool_watchdog as _tool_watchdog  # noqa: E402

_undecorated_tool = mcp.tool


def _watchdogged_tool(*t_args, **t_kwargs):
    register = _undecorated_tool(*t_args, **t_kwargs)

    def apply(fn):
        if _inspect.iscoroutinefunction(fn):
            fn = _tool_watchdog(name=getattr(fn, "__name__", None))(fn)
        return register(fn)

    return apply


mcp.tool = _watchdogged_tool

# --- The same scrub guarantee for RESOURCES and PROMPTS ---------------------
# WHY THE TOOL-ONLY VERSION WAS INCOMPLETE. The wrapper above applies
# `utils.scrub_result` to every tool result, and the path-leak census
# (tests/test_path_leaks.py) certified the property from it. But it patched
# `mcp.tool` and nothing else, so the guarantee stopped exactly where the
# `@mcp.tool()` decorator did.
#
# MEASURED 2026-08-21: 120 of 120 tools scrubbed, 5 resources and 7 prompts NOT.
# That is a live hole of the very class the census named, not a hypothetical
# one: `resources/handlers.py` formats every failure as
# `f"Failed to load profile: {exc}"`, and OSError and Playwright errors embed
# the absolute filename they failed on. No new code was needed for a leak to
# appear -- only a disk error.
#
# TWO DIFFERENCES FROM THE TOOL WRAPPER, both deliberate:
#
# 1. NO WATCHDOG. A resource read has no browser session or apply path behind
#    it and a prompt handler is pure, so there is no "never returns" to bound.
#    Scrubbing is the whole job here.
# 2. SYNC HANDLERS TOO. `_watchdogged_tool` wraps only coroutines, because a
#    watchdog needs something to await. All 7 prompt handlers are SYNC, so
#    reusing that check would leave every prompt unscrubbed while reporting
#    itself as applied. Both branches are wrapped below.
#
# It is the SAME `utils.scrub_result`, not a second scrubber -- one boundary
# rule, one implementation, so a fix to the rule reaches every surface.
#
# TYPED RETURNS SURVIVE UNTOUCHED. `scrub_result` handles str/dict/list/tuple
# and passes everything else through unchanged, so a FastMCP `Message` object
# comes back as itself and every prompt's declared return type keeps
# validating. Confirmed by running the full suite, not by reading the code.
#
# `functools.wraps` keeps `__name__`/`__doc__`/`__wrapped__`, which is what
# FastMCP's signature introspection reads -- so a prompt's argument schema and
# a resource's URI-parameter validation are both unchanged.
import functools as _functools  # noqa: E402

from naukri_server.utils import scrub_result as _scrub_result  # noqa: E402


def _scrubbed_handler(fn):
    """Route a resource/prompt handler's result through `scrub_result`."""
    if _inspect.iscoroutinefunction(fn):
        @_functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return _scrub_result(await fn(*args, **kwargs))
    else:
        @_functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return _scrub_result(fn(*args, **kwargs))

    # The census in tests/test_resource_prompt_scrubbing.py walks the live
    # registries looking for this marker, so a handler added later cannot opt
    # out silently.
    wrapper.__scrubbed_result__ = True
    return wrapper


_undecorated_resource = mcp.resource
_undecorated_prompt = mcp.prompt


def _scrubbed_resource(*r_args, **r_kwargs):
    register = _undecorated_resource(*r_args, **r_kwargs)

    def apply(fn):
        return register(_scrubbed_handler(fn))

    return apply


def _scrubbed_prompt(*p_args, **p_kwargs):
    register = _undecorated_prompt(*p_args, **p_kwargs)

    def apply(fn):
        return register(_scrubbed_handler(fn))

    return apply


mcp.resource = _scrubbed_resource
mcp.prompt = _scrubbed_prompt

# Register OAuth consent UI route only when OAuth is enabled and not in
# auto-approve mode (otherwise the consent endpoint is unreachable).
if _oauth_enabled and _oauth_provider is not None and not _oauth_provider._auto_approve:
    from naukri_server.auth import consent_route as _consent_route
    _consent_route.register(mcp, _oauth_provider)

# Import tool modules to register @mcp.tool() decorators
from naukri_server.tools import auth, search, jobs, apply, profile, profile_update, profile_targeting, debug, tracking, saved_jobs, analytics, sync, inbox, notifications, settings, alerts, companies, performance, assessments, subscription, mock_interview, resume_photo, early_access, resume_builder, ambitionbox, ambitionbox_rest, health, insights, research, daily_brief, smart_apply, compare, auto_hunt, skill_gap, export, resume_tailor, reminders, scheduler_tool, agent_tool, kill_switch_tool, config_tool, server_info  # noqa: E402, F401
from naukri_server import subscribers  # noqa: F401 — registers event handlers
from naukri_server import resources  # noqa: F401 — registers @mcp.resource() handlers
from naukri_server import prompts  # noqa: F401 — registers @mcp.prompt() handlers
from naukri_server import probes  # noqa: F401 — registers api_validator @health_probe
from naukri_server.dashboard import routes  # noqa: F401 — registers @mcp.custom_route() endpoints
