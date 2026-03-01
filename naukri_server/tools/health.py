"""Health check tool — validates API endpoints, browser pool, and AmbitionBox scraping."""

import asyncio
import time

from naukri_server import mcp
from naukri_server.api import api_get, api_post
from naukri_server.browser import browser, page_goto
from naukri_server.config import (
    ACTIVITY_LEVEL_API,
    AMBITIONBOX_BASE,
    DASHBOARD_API,
    NAUKRI_BASE,
    PROFILE_API,
    RECOMMENDED_JOBS_API,
    SEARCH_API,
    logger,
)


# ---------------------------------------------------------------------------
# Individual check functions — each returns a result dict, never raises
# ---------------------------------------------------------------------------


async def _check_login() -> dict:
    """Verify session is valid via the activity-level endpoint."""
    t0 = time.monotonic()
    try:
        data = await api_get(ACTIVITY_LEVEL_API)
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
        data = await api_get(PROFILE_API, {"expand_level": "4"})
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
        data = await api_get(SEARCH_API, {"noOfResults": "1", "keyword": "python", "searchType": "adv"})
        elapsed = int((time.monotonic() - t0) * 1000)
        job_details = data.get("jobDetails", [])
        no_of_jobs = data.get("noOfJobs", 0)
        if job_details and no_of_jobs > 0:
            return {"name": "search_api", "status": "ok", "message": f"Search working, {no_of_jobs} total results", "elapsed_ms": elapsed}
        return {"name": "search_api", "status": "warn", "message": f"Response received but jobDetails empty (noOfJobs={no_of_jobs})", "elapsed_ms": elapsed}
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        msg = str(e)
        # 406 reCAPTCHA is expected — direct API is blocked, browser intercept works
        if "406" in msg or "recaptcha" in msg.lower():
            return {"name": "search_api", "status": "warn", "message": "API returns 406 reCAPTCHA (expected — search uses browser intercept)", "elapsed_ms": elapsed}
        return {"name": "search_api", "status": "fail", "message": f"{type(e).__name__}: {e}", "elapsed_ms": elapsed}


async def _check_recommendations_api() -> dict:
    """Verify recommendations API returns job details."""
    t0 = time.monotonic()
    try:
        data = await api_post(RECOMMENDED_JOBS_API, body={})
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
        data = await api_get(DASHBOARD_API)
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
            await asyncio.sleep(2)
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


# ---------------------------------------------------------------------------
# Main health check tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def naukri_health_check(include_browser: bool = True) -> dict:
    """Run a comprehensive health check on all Naukri MCP integrations.

    Tests API endpoints (login, profile, search, recommendations, dashboard)
    and optionally browser-based checks (page pool, AmbitionBox scraping).

    Args:
        include_browser: If True (default), also run browser checks (~8s extra).
                         Set to False for a fast API-only smoke test (~2s).

    Returns:
        - {status: "success", summary: {ok, warn, fail, total_ms}, checks: [{name, status, message, elapsed_ms}, ...]}
    """
    t_start = time.monotonic()

    # Run all 5 API checks in parallel
    api_tasks = [
        _check_login(),
        _check_profile_api(),
        _check_search_api(),
        _check_recommendations_api(),
        _check_dashboard_api(),
    ]
    api_results = await asyncio.gather(*api_tasks, return_exceptions=True)

    checks = []
    for i, result in enumerate(api_results):
        if isinstance(result, Exception):
            name = ["login", "profile_api", "search_api", "recommendations_api", "dashboard_api"][i]
            checks.append({"name": name, "status": "fail", "message": f"Unexpected error: {type(result).__name__}: {result}", "elapsed_ms": 0})
        else:
            checks.append(result)

    # Run browser checks sequentially (they share the page pool)
    if include_browser:
        browser_result = await _check_browser_alive()
        checks.append(browser_result)

        ambitionbox_result = await _check_ambitionbox()
        checks.append(ambitionbox_result)

    total_ms = int((time.monotonic() - t_start) * 1000)

    ok_count = sum(1 for c in checks if c["status"] == "ok")
    warn_count = sum(1 for c in checks if c["status"] == "warn")
    fail_count = sum(1 for c in checks if c["status"] == "fail")

    return {
        "status": "success",
        "summary": {
            "ok": ok_count,
            "warn": warn_count,
            "fail": fail_count,
            "total_ms": total_ms,
        },
        "checks": checks,
    }
