"""Naukri data sync — pull applied jobs and saved jobs from Naukri's backend."""

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

from mcp.server.fastmcp import Context

from naukri_server import mcp
from naukri_server.api import NaukriAPIError
from naukri_server.interfaces import api_client
from naukri_server.browser import browser, page_goto, page_intercept_json
from naukri_server.config import (
    logger, APPLIED_JOBS_PAGE, SAVED_JOBS_PAGE,
    APPLIED_JOBS_API, SAVED_JOBS_API,
    BROWSER_MODAL_APPEAR,
    SYNC_STATE_FILE,
    AUTO_PURGE_DAYS,
)
from naukri_server.events import event_bus, ApplicationStatusChanged

# Re-export pure business logic from service layer for backward compatibility.
# Tests import these names from naukri_server.tools.sync — keep them importable.
from naukri_server.services.sync_service import (  # noqa: F401
    _parse_applied_jobs,
    _parse_saved_jobs,
    _map_naukri_status,
    merge_applications as _merge_applications,
    merge_saved_jobs as _merge_saved_jobs,
)


# ---------------------------------------------------------------------------
# Sync state persistence — path comes from config.SYNC_STATE_FILE
# ---------------------------------------------------------------------------

def _load_sync_state() -> dict:
    if SYNC_STATE_FILE.exists():
        try:
            return json.loads(SYNC_STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug("Corrupted sync state file, resetting: %s", e)
            return {}
    return {}


def _save_sync_state(state: dict):
    text = json.dumps(state, indent=2)
    tmp = SYNC_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(SYNC_STATE_FILE))


# Async wrappers — avoid blocking the event loop with sync I/O
_sync_state_lock = asyncio.Lock()


async def _load_sync_state_async() -> dict:
    return await asyncio.to_thread(_load_sync_state)


async def _save_sync_state_async(state: dict):
    await asyncio.to_thread(_save_sync_state, state)


# Merge helpers moved to services/sync_service.py — re-exported above as
# _merge_applications / _merge_saved_jobs to preserve test import paths.

# URL patterns for browser interception (discovered)
_APPLIED_JOBS_URL_PATTERN = "applyapi/v5/history"  # matches /cloudgateway-apply/.../applyapi/v5/history
_SAVED_JOBS_URL_PATTERN = "savedJobs/detail"  # matches /jobapi/v3/user/savedJobs/detail


# ---------------------------------------------------------------------------
# HTML scrape: adaptive JS for extracting applied jobs from server-rendered page
# ---------------------------------------------------------------------------

_SCRAPE_APPLIED_JOBS_JS = """() => {
    // Adaptive applied-jobs page scraper — tries multiple CSS selector strategies

    const STRATEGIES = [
        { name: 'tuple',       cardSelector: '[class*="tuple"]' },
        { name: 'jobCard',     cardSelector: '[class*="jobCard"], [class*="job-card"], [class*="JobCard"]' },
        { name: 'data-job-id', cardSelector: '[data-job-id]' },
        { name: 'appliedJob',  cardSelector: '[class*="applied"], [class*="application"]' },
        { name: 'list-item',   cardSelector: '[class*="list"] > [class*="row"], [class*="list"] > li, [class*="list"] > article' },
    ];

    function extractField(card, patterns) {
        for (const p of patterns) {
            try {
                const el = card.querySelector(p);
                if (el) {
                    const t = (el.textContent || '').trim();
                    if (t && t.length > 0 && t.length < 500) return t;
                }
            } catch(e) {}
        }
        return null;
    }

    function extractLink(card, patterns) {
        for (const p of patterns) {
            try {
                const el = card.querySelector(p);
                if (el && el.href) return el.href;
            } catch(e) {}
        }
        const anchors = card.querySelectorAll('a[href]');
        for (const a of anchors) {
            if (a.href && (a.href.includes('/job') || a.href.includes('jobId'))) return a.href;
        }
        return null;
    }

    function extractJobId(card, url) {
        const dataId = card.getAttribute('data-job-id') || card.getAttribute('data-jobid');
        if (dataId) return dataId;
        if (url) { const m = url.match(/([0-9]{6,})/); if (m) return m[1]; }
        const idEl = card.querySelector('[data-job-id], [data-jobid], [data-id]');
        if (idEl) return idEl.getAttribute('data-job-id') || idEl.getAttribute('data-jobid') || idEl.getAttribute('data-id');
        const h = card.outerHTML.slice(0, 500);
        const im = h.match(/[jJ]ob[iI]d['":\\s=]+['"]*([0-9]{6,})/);
        if (im) return im[1];
        return null;
    }

    const TITLE = ['[class*="title"] a', '[class*="Title"] a', '[class*="desig"]', 'a[class*="title"]', 'h2 a', 'h3 a', 'h4 a', 'a:first-of-type'];
    const COMPANY = ['[class*="comp"]', '[class*="Comp"]', '[class*="company"]', '[class*="org"]', '[class*="subTitle"]'];
    const STATUS = ['[class*="status"]', '[class*="Status"]', '[class*="appStatus"]', '[class*="state"]', '[class*="badge"]'];
    const DATE = ['[class*="date"]', '[class*="Date"]', '[class*="applied"]', '[class*="time"]', 'time', '[datetime]'];
    const LOCATION = ['[class*="loc"]', '[class*="Loc"]', '[class*="location"]', '[class*="city"]'];
    const SALARY = ['[class*="sal"]', '[class*="Sal"]', '[class*="salary"]', '[class*="ctc"]', '[class*="package"]'];

    for (const strategy of STRATEGIES) {
        let cards;
        try { cards = document.querySelectorAll(strategy.cardSelector); } catch(e) { continue; }
        if (cards.length === 0 || cards.length >= 200) continue;

        const jobs = [];
        for (const card of cards) {
            const url = extractLink(card, TITLE);
            const job_id = extractJobId(card, url);
            const title = extractField(card, TITLE);
            if (!title && !job_id) continue;
            jobs.push({
                job_id: job_id, title: title,
                company: extractField(card, COMPANY),
                status: extractField(card, STATUS),
                applied_date: extractField(card, DATE),
                location: extractField(card, LOCATION),
                salary: extractField(card, SALARY),
                url: url,
            });
        }
        if (jobs.length > 0) {
            const pagination = { has_next: false, current_page: 1, total_pages: null };
            try {
                const nextBtn = document.querySelector(
                    'a[class*="next" i], a[class*="Next"], [class*="paginat"] a:last-child, ' +
                    'a[aria-label*="next" i], button[class*="next" i], [class*="fright"] a'
                );
                if (nextBtn && !nextBtn.classList.toString().toLowerCase().includes('disabled') &&
                    !nextBtn.hasAttribute('disabled')) pagination.has_next = true;
                const activePage = document.querySelector('[class*="paginat"] [class*="active"], [class*="paginat"] .selected');
                if (activePage) { const n = parseInt(activePage.textContent.trim()); if (!isNaN(n)) pagination.current_page = n; }
                let maxPage = 0;
                document.querySelectorAll('[class*="paginat"] a').forEach(a => {
                    const n = parseInt(a.textContent.trim()); if (!isNaN(n) && n > maxPage) maxPage = n;
                });
                if (maxPage > 0) pagination.total_pages = maxPage;
            } catch(e) {}
            return { strategy: strategy.name, jobs: jobs, pagination: pagination, total_on_page: jobs.length };
        }
    }

    // All strategies failed — return diagnostics
    const diag = {};
    for (const s of STRATEGIES) {
        try { diag[s.name] = document.querySelectorAll(s.cardSelector).length; } catch(e) { diag[s.name] = 'error'; }
    }
    return { error: 'no_matching_strategy', diagnostics: diag, page_title: document.title,
             body_length: (document.body.textContent || '').length, url: window.location.href };
}"""


# ---------------------------------------------------------------------------
# Fetch helpers (REST, browser intercept, HTML scrape)
# ---------------------------------------------------------------------------

async def _fetch_via_rest(api_path: Optional[str], params: Optional[dict] = None) -> Optional[dict]:
    """Try fetching data via direct REST API call.
    Returns None if endpoint is not configured or returns an error."""
    if not api_path:
        return None
    try:
        return await api_client.get(api_path, params)
    except NaukriAPIError as e:
        logger.warning("REST fetch failed for %s: %s", api_path, e)
        return None
    except Exception as e:
        logger.warning("REST fetch exception for %s: %s: %s", api_path, type(e).__name__, e)
        return None


async def _fetch_applied_jobs_rest(max_pages: int = 10, days_back: int = 365) -> Optional[list]:
    """Fetch all applied jobs via the history REST API with pagination.

    GET /cloudgateway-apply/whtma-services/v0/applyapi/v5/history
        ?pageSize=20&days=365&pageNumber=1&filterInfo=2

    Returns a flat list of applyDetails entries, or None on failure.
    """
    if not APPLIED_JOBS_API:
        return None

    all_details = []
    page = 1

    while page <= max_pages:
        params = {"pageSize": "20", "days": str(days_back), "pageNumber": str(page), "filterInfo": "2"}
        try:
            data = await api_client.get(APPLIED_JOBS_API, params)
        except Exception as e:
            logger.warning("Applied jobs REST page %d failed: %s", page, e)
            if page == 1:
                return None
            break

        details = data.get("applyDetails", [])
        if not details:
            break

        all_details.extend(details)

        total = int(data.get("matchingRowsCount", 0))
        if len(all_details) >= total:
            break
        page += 1

    if not all_details:
        return None

    logger.info("REST: fetched %d applied jobs across %d page(s)", len(all_details), page)
    return all_details


async def _fetch_via_browser(page_url: str, url_pattern: Optional[str] = None,
                              timeout: float = 10) -> Optional[dict]:
    """Fetch data by navigating in browser and intercepting JSON responses."""
    async with browser.page_pool.acquire() as page:
        # Try targeted capture first
        if url_pattern:
            data = await page_intercept_json(page, page_url, url_pattern=url_pattern, timeout=timeout)
            if data:
                return data

        # Fallback: capture all JSON responses and return the largest
        captured_responses = []
        response_event = asyncio.Event()

        async def on_response(response):
            content_type = response.headers.get("content-type", "")
            if response.status == 200 and "json" in content_type:
                try:
                    body = await response.json()
                    captured_responses.append({"url": response.url, "body": body})
                except Exception as e:
                    logger.debug("Failed to parse JSON response from %s: %s", response.url, e)
                response_event.set()

        page.on("response", on_response)
        try:
            await page_goto(page, page_url)
            try:
                await asyncio.wait_for(response_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Browser fetch timed out after %ss for: %s", timeout, page_url)
            await asyncio.sleep(BROWSER_MODAL_APPEAR)
        finally:
            page.remove_listener("response", on_response)

        if not captured_responses:
            return None

        # Return the largest captured response
        return max(captured_responses, key=lambda r: len(str(r["body"])))["body"]


async def _fetch_via_html_scrape(page_url: str, max_pages: int = 10) -> Optional[list]:
    """Fetch applied jobs by scraping server-rendered HTML via page.evaluate().

    Navigates to the applied-jobs page, extracts job cards from the DOM
    using adaptive CSS selectors, and handles pagination.

    Returns a list of normalized job dicts, or None if scraping failed.
    """
    async with browser.page_pool.acquire() as page:
        all_jobs = []
        current_page = 1

        while current_page <= max_pages:
            url = page_url if current_page == 1 else f"{page_url}?pageNo={current_page}"
            await page_goto(page, url)
            await asyncio.sleep(BROWSER_MODAL_APPEAR)  # let server-rendered content settle

            # Detect login redirect
            current_url = page.url
            if "login" in current_url.lower() or "nologin" in current_url.lower():
                logger.warning("HTML scrape: redirected to login (%s)", current_url)
                return None

            result = await page.evaluate(_SCRAPE_APPLIED_JOBS_JS)

            if result is None or result.get("error"):
                logger.warning("HTML scrape page %d: %s", current_page,
                               result.get("error") if result else "null")
                if current_page == 1:
                    # Log diagnostics for first-page failure
                    if result and result.get("diagnostics"):
                        logger.info("HTML scrape diagnostics: %s", result)
                    return None
                break  # partial results from earlier pages

            page_jobs = result.get("jobs", [])
            if not page_jobs:
                break

            all_jobs.extend(page_jobs)

            has_next = result.get("pagination", {}).get("has_next", False)
            if not has_next:
                break
            current_page += 1

        if not all_jobs:
            return None

        logger.info("HTML scrape: %d applied jobs across %d page(s)", len(all_jobs), current_page)
        return all_jobs


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _sync_applications(
    force_browser: bool = False,
    days_back: int = 365,
    ctx: Context | None = None,
) -> dict:
    """Sync applied jobs from Naukri.com into local tracking.

    Pulls application history from Naukri's backend and merges
    with local applications.json. Preserves local-only fields. New jobs
    from Naukri are added with source="naukri_sync".
    """
    remote_data = None
    remote_jobs = None
    method = "unknown"

    # Strategy 1: Paginated REST API (fetches all pages, returns raw entries)
    if not force_browser:
        if ctx:
            try:
                await ctx.info("Trying REST API for applied jobs...")
            except Exception:
                pass
        rest_entries = await _fetch_applied_jobs_rest(days_back=days_back)
        if rest_entries is not None:
            remote_data = {"applyDetails": rest_entries}
            method = "rest_api"

    # Strategy 2: Browser JSON intercept (only if URL pattern known)
    if remote_data is None and _APPLIED_JOBS_URL_PATTERN:
        if ctx:
            try:
                await ctx.info("REST failed, trying browser intercept...")
            except Exception:
                pass
        remote_data = await _fetch_via_browser(
            APPLIED_JOBS_PAGE,
            url_pattern=_APPLIED_JOBS_URL_PATTERN,
        )
        if remote_data is not None:
            method = "browser_intercept"

    # Strategy 3: HTML scraping (server-rendered page)
    if remote_data is None and remote_jobs is None:
        if ctx:
            try:
                await ctx.info("Browser intercept failed, trying HTML scrape...")
            except Exception:
                pass
        scraped = await _fetch_via_html_scrape(APPLIED_JOBS_PAGE)
        if scraped is not None:
            remote_jobs = scraped  # already normalized, skip _parse_applied_jobs
            method = "html_scrape"

    # Parse JSON strategies through the parser
    if remote_data is not None and remote_jobs is None:
        remote_jobs = _parse_applied_jobs(remote_data)

    if remote_jobs is None:
        return {
            "status": "error",
            "message": ("Could not fetch applied jobs. "
                        "All strategies failed (REST, browser intercept, HTML scrape). "
                        "You may need to log in first via naukri_login."),
            "error_code": "API_ERROR",
        }

    # Normalize status labels
    for job in remote_jobs:
        if job.get("status"):
            job["status"] = _map_naukri_status(job["status"])
        else:
            job["status"] = "applied"

    from naukri_server.database import list_all_applications, upsert_application, delete_applications_before
    from naukri_server.sagas import SagaExecutor

    local_apps = await list_all_applications()
    # Snapshot old statuses for change detection
    old_status_map = {a["job_id"]: a.get("status") for a in local_apps if a.get("job_id")}

    # --- Saga: wrap merge + purge + persist + events in saga steps ---
    saga = SagaExecutor("sync_applications")
    stats = {}
    purged = 0
    status_changes = []

    async def step_merge():
        nonlocal stats
        stats = _merge_applications(local_apps, remote_jobs)
        return stats

    async def step_purge():
        nonlocal local_apps, purged
        cutoff = (datetime.now(timezone.utc) - timedelta(days=AUTO_PURGE_DAYS)).isoformat()
        before_count = len(local_apps)
        local_apps = [a for a in local_apps if a.get("applied_at", "") >= cutoff or a.get("source") == "manual"]
        purged = before_count - len(local_apps)
        if purged:
            logger.info("Auto-purged %d applications older than %d days", purged, AUTO_PURGE_DAYS)
        return {"purged": purged}

    async def step_detect_changes():
        nonlocal status_changes
        for app in local_apps:
            jid = app.get("job_id")
            old_s = old_status_map.get(jid)
            new_s = app.get("status")
            if old_s and new_s and old_s != new_s:
                status_changes.append({
                    "job_id": jid,
                    "title": app.get("title"),
                    "company": app.get("company"),
                    "old_status": old_s,
                    "new_status": new_s,
                })
        return {"status_changes_count": len(status_changes)}

    async def step_persist():
        cutoff = (datetime.now(timezone.utc) - timedelta(days=AUTO_PURGE_DAYS)).isoformat()
        for app in local_apps:
            await upsert_application(app)
        if purged:
            await delete_applications_before(cutoff)
        return {"persisted": len(local_apps)}

    async def step_emit_events():
        for change in status_changes:
            await event_bus.emit(ApplicationStatusChanged(
                job_id=change.get("job_id", ""),
                company=change.get("company", ""),
                title=change.get("title", ""),
                old_status=change.get("old_status", ""),
                new_status=change.get("new_status", ""),
            ))
        return {"events_emitted": len(status_changes)}

    saga.add_step("merge", step_merge)
    saga.add_step("purge", step_purge)
    saga.add_step("detect_changes", step_detect_changes)
    saga.add_step("persist", step_persist)
    saga.add_step("emit_events", step_emit_events)

    saga_result = await saga.run()

    if saga_result["status"] == "error":
        saga_errors = saga_result.get("errors", [])
        error_detail = saga_errors[0] if saga_errors else "unknown"
        return {
            "status": "error",
            "message": f"Sync saga failed at step '{saga_result.get('failed_step')}': {error_detail}",
            "error_code": "API_ERROR",
            "saga_steps": saga_result.get("completed_steps", []),
            "saga_errors": saga_errors,
        }

    # Record sync metadata
    async with _sync_state_lock:
        state = await _load_sync_state_async()
        state["last_applications_sync"] = datetime.now(timezone.utc).isoformat()
        state["last_applications_method"] = method
        state["last_applications_count"] = len(remote_jobs)
        await _save_sync_state_async(state)

    # Emit SyncCompleted event for reactive subscribers
    from naukri_server.events import SyncCompleted as SyncCompletedEvent
    await event_bus.emit(SyncCompletedEvent(
        entity="applications",
        new_added=stats.get("new_added", 0),
        updated=stats.get("updated", 0),
        status_changes_count=len(status_changes),
    ))

    new_count = stats.get("new_added", 0)
    updated_count = stats.get("updated", 0)
    if ctx:
        try:
            await ctx.info(
                f"Sync complete: {new_count} new, {updated_count} updated "
                f"(method={method})"
            )
        except Exception:
            pass

    result = {
        "status": "success",
        "method": method,
        "total_remote": len(remote_jobs),
        **stats,
        "purged": purged,
        "days_back": days_back,
        "last_sync": state.get("last_applications_sync"),
        "applications": local_apps[:20],
        "saga_steps": saga_result.get("completed_steps", []),
        "step_timings": saga_result.get("step_timings", {}),
    }
    if status_changes:
        result["status_changes"] = status_changes
    return result


async def _sync_saved_jobs(
    force_browser: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Sync saved/bookmarked jobs from Naukri.com into local tracking.

    Pulls saved jobs from Naukri and merges with local saved_jobs.json.
    """
    remote_data = None
    method = "unknown"

    if not force_browser:
        if ctx:
            try:
                await ctx.info("Trying REST API for saved jobs...")
            except Exception:
                pass
        remote_data = await _fetch_via_rest(SAVED_JOBS_API)
        if remote_data is not None:
            method = "rest_api"

    if remote_data is None:
        if ctx:
            try:
                await ctx.info("REST failed, trying browser intercept...")
            except Exception:
                pass
        remote_data = await _fetch_via_browser(
            SAVED_JOBS_PAGE,
            url_pattern=_SAVED_JOBS_URL_PATTERN,
        )
        method = "browser_intercept"

    if remote_data is None:
        return {
            "status": "error",
            "message": "Could not fetch saved jobs. Run naukri_debug(action='discover') to find the API endpoint.",
            "error_code": "API_ERROR",
        }

    remote_jobs = _parse_saved_jobs(remote_data)

    from naukri_server.database import list_all_saved_jobs, upsert_saved_job
    from naukri_server.sagas import SagaExecutor
    from naukri_server.events import SyncCompleted as SyncCompletedEvent

    local_saved = await list_all_saved_jobs()

    # --- Saga: wrap merge + persist + emit events in saga steps ---
    saga = SagaExecutor("sync_saved_jobs")
    stats = {}

    async def step_merge():
        nonlocal stats
        stats = _merge_saved_jobs(local_saved, remote_jobs)
        return stats

    async def step_persist():
        for sj in local_saved:
            await upsert_saved_job(sj)
        return {"persisted": len(local_saved)}

    async def step_emit_events():
        await event_bus.emit(SyncCompletedEvent(
            entity="saved_jobs",
            new_added=stats.get("new_added", 0),
            updated=0,
            status_changes_count=0,
        ))
        return {"events_emitted": 1}

    saga.add_step("merge", step_merge)
    saga.add_step("persist", step_persist)
    saga.add_step("emit_events", step_emit_events)

    saga_result = await saga.run()

    if saga_result["status"] == "error":
        saga_errors = saga_result.get("errors", [])
        error_detail = saga_errors[0] if saga_errors else "unknown"
        return {
            "status": "error",
            "message": f"Sync saga failed at step '{saga_result.get('failed_step')}': {error_detail}",
            "error_code": "API_ERROR",
            "saga_steps": saga_result.get("completed_steps", []),
            "saga_errors": saga_errors,
        }

    # Record sync metadata
    async with _sync_state_lock:
        state = await _load_sync_state_async()
        state["last_saved_jobs_sync"] = datetime.now(timezone.utc).isoformat()
        state["last_saved_jobs_method"] = method
        state["last_saved_jobs_count"] = len(remote_jobs)
        await _save_sync_state_async(state)

    new_count = stats.get("new_added", 0)
    if ctx:
        try:
            await ctx.info(
                f"Saved jobs sync complete: {new_count} new (method={method})"
            )
        except Exception:
            pass

    return {
        "status": "success",
        "method": method,
        "total_remote": len(remote_jobs),
        **stats,
        "last_sync": state.get("last_saved_jobs_sync"),
        "saved_jobs": local_saved[:20],
        "saga_steps": saga_result.get("completed_steps", []),
        "step_timings": saga_result.get("step_timings", {}),
    }


# ---------------------------------------------------------------------------
# Individual tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def naukri_sync_applications(
    force_browser: bool = False,
    days_back: Optional[int] = None,
    ctx: Context | None = None,
) -> dict:
    """Sync applied jobs from Naukri.com into local tracking.

    Uses 3-tier fallback: REST API -> browser intercept -> HTML scrape.
    Merges with local applications, preserves local-only fields.

    Args:
        force_browser: If True, skip REST API and use browser strategies.
        days_back: Fetch from last N days (default 365). Use smaller values
                   (e.g., 7 or 30) for faster incremental syncs.

    Returns:
        {status, method, total_remote, new_added, updated, unchanged, local_only,
         days_back, last_sync, applications: [...first 20...]}
    """
    # Bypasses handle_tool_action by design: sync classifies ALL transient errors
    # (timeouts, connection resets, browser crashes) as API_ERROR rather than
    # INTERNAL_ERROR, since they reflect upstream service state, not local bugs.
    # handle_tool_action would tag generic Exceptions as INTERNAL_ERROR, which is
    # misleading for sync — callers should retry rather than file a bug.
    try:
        kwargs = {
            "force_browser": force_browser,
            "days_back": days_back if days_back is not None else 365,
        }
        if ctx is not None:
            kwargs["ctx"] = ctx
        return await _sync_applications(**kwargs)
    except Exception as e:
        return {"status": "error", "message": f"Sync failed: {type(e).__name__}: {e!r}", "error_code": "API_ERROR"}


@mcp.tool()
async def naukri_sync_saved(
    force_browser: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Sync saved/bookmarked jobs from Naukri.com into local tracking.

    Uses 2-tier fallback: REST API -> browser intercept.
    Merges with local saved jobs list.

    Args:
        force_browser: If True, skip REST API and use browser strategy.

    Returns:
        {status, method, total_remote, new_added, already_local, local_only,
         last_sync, saved_jobs: [...first 20...]}
    """
    # Bypasses handle_tool_action by design — see naukri_sync_applications for rationale
    # (transient sync errors are classified as API_ERROR, not INTERNAL_ERROR).
    try:
        kwargs = {"force_browser": force_browser}
        if ctx is not None:
            kwargs["ctx"] = ctx
        return await _sync_saved_jobs(**kwargs)
    except Exception as e:
        return {"status": "error", "message": f"Sync failed: {type(e).__name__}: {e!r}", "error_code": "API_ERROR"}


@mcp.tool()
async def naukri_export_data(
    data_type: str,
    export_format: str = "json",
    keywords: Optional[str] = None,
    output_path: Optional[str] = None,
) -> dict:
    """Export applications, saved jobs, or search results to a file.

    Args:
        data_type: What to export — "applications", "saved_jobs", or "search_results".
        export_format: Output format — "json" or "csv" (default "json").
        keywords: Required when data_type is "search_results" — the search query.
        output_path: Custom file path (default: exports/<type>_<date>.<ext>).

    Returns:
        {status, file_path, record_count, data_type, format}
    """
    from naukri_server.tools.export import _export_data
    try:
        return await _export_data(
            data_type=data_type,
            format=export_format,
            keywords=keywords,
            output_path=output_path,
        )
    except Exception as e:
        return {"status": "error", "message": f"Export failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}
