"""Health check tool — validates API endpoints, browser pool, and AmbitionBox scraping."""

import asyncio
import os
import time

from naukri_server import mcp
from naukri_server.api import api_metrics, NaukriAPIError
from naukri_server.block_state import BlockState, classify_block_state
from naukri_server.interfaces import api_client, browser_provider
from naukri_server.browser import browser, page_goto
from naukri_server.config import (
    ACTIVITY_LEVEL_API,
    AMBITIONBOX_BASE,
    BROWSER_MODAL_APPEAR,
    CHROME_PROFILE,
    DASHBOARD_API,
    NAUKRI_BASE,
    PROFILE_API,
    RECOMMENDED_JOBS_API,
    SEARCH_API,
    HEALTH_CHECK_TIMEOUT,
    HEALTH_BROWSER_CHECK_TIMEOUT,
    logger,
)


def _classify_health_error(exc: Exception) -> BlockState:
    """Map a raised health-check error to a BlockState.

    Prefers the structured ``block_kind`` that api.py tags onto NaukriAPIError /
    NaukriBotCheckError. Falls back to running the pure classifier over the
    error message (which embeds status + body snippet) so older/untagged errors
    still classify. Returns BlockState.HEALTHY when nothing block-like is found
    (the caller then treats it as a hard failure).
    """
    block_kind = getattr(exc, "block_kind", None)
    if block_kind:
        try:
            return BlockState(block_kind)
        except ValueError:
            pass
    status = getattr(exc, "status", 0) or 0
    return classify_block_state(status=status, content_type="", body=str(exc)).state


# ---------------------------------------------------------------------------
# Individual check functions — each returns a result dict, never raises
# ---------------------------------------------------------------------------


async def _check_login() -> dict:
    """Verify session is valid via the activity-level endpoint."""
    t0 = time.monotonic()
    try:
        data = await api_client.get(ACTIVITY_LEVEL_API)
        elapsed = int((time.monotonic() - t0) * 1000)
        if isinstance(data, dict):
            return {"name": "login", "status": "ok", "message": "Logged in, session active", "elapsed_ms": elapsed}
        return {"name": "login", "status": "warn", "message": f"Unexpected response type: {type(data).__name__}", "elapsed_ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return {"name": "login", "status": "fail", "message": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed}


async def _check_profile_api() -> dict:
    """Verify profile API returns data with a name."""
    t0 = time.monotonic()
    try:
        data = await api_client.get(PROFILE_API, {"expand_level": "4"})
        elapsed = int((time.monotonic() - t0) * 1000)
        profiles = data.get("profile", [])
        if profiles and isinstance(profiles[0], dict) and profiles[0].get("name"):
            name = profiles[0]["name"]
            return {"name": "profile_api", "status": "ok", "message": f"Profile loaded: {name}", "elapsed_ms": elapsed}
        return {"name": "profile_api", "status": "warn", "message": "Response received but name not found — possible schema change", "elapsed_ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return {"name": "profile_api", "status": "fail", "message": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed}


async def _check_search_api() -> dict:
    """Verify search API is reachable. Note: direct API calls often return 406
    (reCAPTCHA) — the real naukri_search_jobs tool uses browser intercept.
    A 406 is treated as 'warn' (API exists but requires browser), not 'fail'."""
    t0 = time.monotonic()
    try:
        data = await api_client.get(SEARCH_API, {"noOfResults": "1", "keyword": "python", "searchType": "adv"})
        elapsed = int((time.monotonic() - t0) * 1000)
        job_details = data.get("jobDetails", [])
        no_of_jobs = data.get("noOfJobs", 0)
        if job_details and no_of_jobs > 0:
            return {"name": "search_api", "status": "ok", "message": f"Search working, {no_of_jobs} total results", "elapsed_ms": elapsed}
        return {"name": "search_api", "status": "warn", "message": f"Response received but jobDetails empty (noOfJobs={no_of_jobs})", "elapsed_ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        # Classify the failure via the block-state classifier instead of the old
        # hardcoded `if "406" in msg` string check. A direct search API call
        # being bot-blocked (soft_block / captcha) is EXPECTED — the real
        # naukri_search_jobs tool uses browser intercept — so those are "warn",
        # not "fail". Other block kinds (login_wall) and non-block errors are
        # surfaced distinctly. Prefer the structured block_kind tagged on the
        # error by api.py; fall back to marker matching on the message.
        assessment = _classify_health_error(e)
        if assessment is BlockState.SOFT_BLOCK or assessment is BlockState.CAPTCHA:
            return {"name": "search_api", "status": "warn",
                    "message": f"API bot-checked ({assessment.value}) — expected; search uses browser intercept",
                    "elapsed_ms": elapsed, "block_kind": assessment.value}
        if assessment is BlockState.RATE_LIMITED:
            return {"name": "search_api", "status": "warn",
                    "message": "API rate-limited (429) — expected under load; search uses browser intercept",
                    "elapsed_ms": elapsed, "block_kind": assessment.value}
        if assessment is BlockState.LOGIN_WALL:
            return {"name": "search_api", "status": "fail",
                    "message": "API redirected to login — session expired (re-auth needed)",
                    "elapsed_ms": elapsed, "block_kind": assessment.value}
        return {"name": "search_api", "status": "fail", "message": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed}


async def _check_recommendations_api() -> dict:
    """Verify recommendations API returns job details."""
    t0 = time.monotonic()
    try:
        data = await api_client.post(RECOMMENDED_JOBS_API, body={})
        elapsed = int((time.monotonic() - t0) * 1000)
        job_details = data.get("jobDetails", [])
        if job_details:
            return {"name": "recommendations_api", "status": "ok", "message": f"Recommendations working, {len(job_details)} jobs returned", "elapsed_ms": elapsed}
        return {"name": "recommendations_api", "status": "warn", "message": "Response received but jobDetails empty", "elapsed_ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return {"name": "recommendations_api", "status": "fail", "message": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed}


async def _check_dashboard_api() -> dict:
    """Verify dashboard API returns profile view count."""
    t0 = time.monotonic()
    try:
        data = await api_client.get(DASHBOARD_API)
        elapsed = int((time.monotonic() - t0) * 1000)
        db = data.get("dashBoard", {})
        if "profileViewCount" in db:
            views = db["profileViewCount"]
            return {"name": "dashboard_api", "status": "ok", "message": f"Dashboard working, {views} profile views", "elapsed_ms": elapsed}
        return {"name": "dashboard_api", "status": "warn", "message": "Response received but profileViewCount missing — possible schema change", "elapsed_ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return {"name": "dashboard_api", "status": "fail", "message": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed}


async def _check_browser_alive() -> dict:
    """Verify page pool works and session is not redirected to login."""
    t0 = time.monotonic()
    try:
        async with browser.page_pool.acquire() as page:
            await page_goto(page, f"{NAUKRI_BASE}/mnjuser/homepage")
            elapsed = int((time.monotonic() - t0) * 1000)
            if "/nlogin" in page.url:
                return {"name": "browser_alive", "status": "fail", "message": "Redirected to login page — session expired", "elapsed_ms": elapsed}
            return {"name": "browser_alive", "status": "ok", "message": f"Browser page pool working, navigated to {page.url}", "elapsed_ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return {"name": "browser_alive", "status": "fail", "message": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed}


async def _check_ambitionbox() -> dict:
    """Verify AmbitionBox scraping works by extracting __NEXT_DATA__."""
    t0 = time.monotonic()
    try:
        async with browser.page_pool.acquire() as page:
            await page_goto(page, f"{AMBITIONBOX_BASE}/salaries/google-salaries", wait="networkidle")
            await asyncio.sleep(BROWSER_MODAL_APPEAR)
            next_data = await page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    if (el) return JSON.parse(el.textContent);
                    for (const s of document.querySelectorAll('script[type="application/json"]')) {
                        try {
                            const d = JSON.parse(s.textContent);
                            if (d && d.props && d.props.pageProps) return d;
                        } catch(e) {}
                    }
                    return null;
                }
            """)
            elapsed = int((time.monotonic() - t0) * 1000)
            if next_data and next_data.get("props", {}).get("pageProps"):
                return {"name": "ambitionbox", "status": "ok", "message": "AmbitionBox __NEXT_DATA__ extractable", "elapsed_ms": elapsed}
            return {"name": "ambitionbox", "status": "warn", "message": "Page loaded but __NEXT_DATA__ not found — scraping may be broken", "elapsed_ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return {"name": "ambitionbox", "status": "fail", "message": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed}


async def _check_browser_interface() -> dict:
    """Verify browser_provider abstraction works."""
    try:
        # Quick test of the interface — confirm it's importable and the singleton exists
        if browser_provider is not None:
            return {"name": "browser_interface", "status": "ok", "message": "Browser provider interface available"}
        return {"name": "browser_interface", "status": "warn", "message": "browser_provider is None"}
    except Exception as e:
        return {"name": "browser_interface", "status": "warn", "message": str(e)}


async def _run_check(name: str, coro, budget: float) -> dict:
    """Run one check under its own budget and ALWAYS return a row.

    Why this exists: on 2026-08-20 a single check that never returned took the
    whole tool down, and the reviewer's client sat on it for four minutes. The
    response schema already carried a per-check {name, status, elapsed_ms}, so a
    blown budget has a natural home - a degraded ROW - rather than a dead tool.
    The other checks' results are still worth having; losing them because one
    probe stalled is a strictly worse answer than reporting the stall.
    """
    t0 = time.monotonic()
    try:
        return await asyncio.wait_for(coro, timeout=budget)
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.warning("Health check %r exceeded its %.0fs budget", name, budget)
        return {
            "name": name,
            "status": "timeout",
            "message": (
                f"Check did not finish within its {budget:.0f}s budget - reported "
                "as degraded so the rest of the health check could still return"
            ),
            "elapsed_ms": elapsed,
        }
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        return {"name": name, "status": "fail",
                "message": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed}


async def _bounded(coro, budget: float, default=None):
    """Await an optional observability extra, giving up rather than hanging."""
    try:
        return await asyncio.wait_for(coro, timeout=budget)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Main health check tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def naukri_health_check(include_browser: bool = True, source: str = "checks") -> dict:
    """Run a comprehensive health check on all Naukri MCP integrations.

    Tests API endpoints (login, profile, search, recommendations, dashboard)
    and optionally browser-based checks (page pool, AmbitionBox scraping).

    Args:
        include_browser: If True (default), also run browser checks (~8s extra).
                         Set to False for a fast API-only smoke test (~2s).
        source: "checks" (default) for traditional health checks, "probes" for
                probe-based health check using the health probe framework.

    Returns:
        - {status: "success", summary: {ok, warn, fail, total_ms}, checks: [{name, status, message, elapsed_ms}, ...]}
    """
    # Probe-based health check
    if source == "probes":
        try:
            from naukri_server.health import probe_registry
            return {
                "status": "success",
                "source": "probes",
                "summary": probe_registry.summary(),
                "probes": probe_registry.all_results(),
            }
        except Exception as e:
            return {"status": "error", "message": f"Probe registry error: {e}", "error_code": "INTERNAL_ERROR"}

    t_start = time.monotonic()

    # Run all 5 API checks in parallel, each under its own budget so one stalled
    # endpoint degrades to a "timeout" row instead of hanging the whole tool.
    api_tasks = [
        _run_check("login", _check_login(), HEALTH_CHECK_TIMEOUT),
        _run_check("profile_api", _check_profile_api(), HEALTH_CHECK_TIMEOUT),
        _run_check("search_api", _check_search_api(), HEALTH_CHECK_TIMEOUT),
        _run_check("recommendations_api", _check_recommendations_api(), HEALTH_CHECK_TIMEOUT),
        _run_check("dashboard_api", _check_dashboard_api(), HEALTH_CHECK_TIMEOUT),
    ]
    api_results = await asyncio.gather(*api_tasks, return_exceptions=True)

    checks = []
    for i, result in enumerate(api_results):
        if isinstance(result, BaseException):
            name = ["login", "profile_api", "search_api", "recommendations_api", "dashboard_api"][i]
            checks.append({"name": name, "status": "fail", "message": f"Unexpected error: {type(result).__name__}: {result}", "elapsed_ms": 0})
        else:
            checks.append(result)

    # Run browser checks sequentially (they share the page pool)
    if include_browser:
        # Sequential on purpose (they share the page pool), each budgeted - the
        # browser checks are the ones most able to stall, and a serial chain of
        # unbounded awaits is how a stall becomes an outage.
        checks.append(await _run_check(
            "browser_alive", _check_browser_alive(), HEALTH_BROWSER_CHECK_TIMEOUT))
        checks.append(await _run_check(
            "ambitionbox", _check_ambitionbox(), HEALTH_BROWSER_CHECK_TIMEOUT))
        checks.append(await _run_check(
            "browser_interface", _check_browser_interface(), HEALTH_CHECK_TIMEOUT))

    total_ms = int((time.monotonic() - t_start) * 1000)

    ok_count = sum(1 for c in checks if c["status"] == "ok")
    warn_count = sum(1 for c in checks if c["status"] == "warn")
    fail_count = sum(1 for c in checks if c["status"] == "fail")
    timeout_count = sum(1 for c in checks if c["status"] == "timeout")

    # Startup validation: Chrome profile directory
    warnings = []
    if not os.path.isdir(CHROME_PROFILE):
        warnings.append(f"Chrome profile directory missing: {CHROME_PROFILE}")

    # PagePool observability stats
    pool_stats = browser.page_pool.get_stats() if browser.page_pool else None

    result = {
        "status": "success",
        "summary": {
            "ok": ok_count,
            "warn": warn_count,
            "fail": fail_count,
            "timeout": timeout_count,
            "total_ms": total_ms,
        },
        "checks": checks,
        "pool_stats": pool_stats,
    }
    # Watchdog stats
    try:
        from naukri_server.browser_watchdog import watchdog
        if watchdog:
            result["watchdog"] = watchdog.stats
            result["watchdog"]["circuit_breaker"] = browser.page_pool.circuit_state if browser.page_pool else "N/A"
    except Exception:
        pass

    result["api_metrics"] = api_metrics.get_stats()
    if warnings:
        result["warnings"] = warnings

    # Event bus observability
    try:
        from naukri_server.events import event_bus
        result["subscriber_count"] = event_bus.subscriber_count_by_type
    except Exception:
        pass

    # Event persistence stats
    try:
        from naukri_server.database import get_event_stats, count_undelivered_notifications
        # Observability extras must never be the reason the tool does not return.
        event_stats = await _bounded(get_event_stats(hours=24), 5.0)
        notif_count = await _bounded(count_undelivered_notifications(), 5.0)
        result["event_stats_24h"] = event_stats
        result["pending_notifications"] = notif_count
    except Exception:
        pass

    try:
        from naukri_server.health import probe_registry
        result["probe_summary"] = probe_registry.summary()
    except Exception:
        pass

    return result
