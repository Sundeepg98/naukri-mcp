"""Naukri data sync — pull applied jobs and saved jobs from Naukri's backend."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.browser import browser, page_goto
from naukri_server.config import (
    logger, APPLIED_JOBS_PAGE, SAVED_JOBS_PAGE,
    APPLIED_JOBS_API, SAVED_JOBS_API,
)
from naukri_server.tools.tracking import (
    _load_json, _save_json, _applications_lock, _saved_jobs_lock,
    APPLICATIONS_FILE, SAVED_JOBS_FILE,
)


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

async def _fetch_via_rest(api_path: Optional[str], params: dict = None) -> Optional[dict]:
    """Try fetching data via direct REST API call.
    Returns None if endpoint is not configured or returns an error."""
    if not api_path:
        return None
    try:
        return await api_get(api_path, params)
    except NaukriAPIError as e:
        logger.warning("REST fetch failed for %s: %s", api_path, e)
        return None
    except Exception as e:
        logger.warning("REST fetch exception for %s: %s: %s", api_path, type(e).__name__, e)
        return None


async def _fetch_applied_jobs_rest(max_pages: int = 10) -> Optional[list]:
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
        params = {"pageSize": "20", "days": "365", "pageNumber": str(page), "filterInfo": "2"}
        try:
            data = await api_get(APPLIED_JOBS_API, params)
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
    """Fetch data by navigating to a page and intercepting JSON API responses.

    Args:
        page_url: The Naukri page to navigate to.
        url_pattern: Substring to match in response URLs. If None, returns largest JSON response.
        timeout: Seconds to wait for the target response.

    Returns the captured JSON body, or None.
    """
    async with browser.page_pool.acquire() as page:
        captured_responses = []
        response_event = asyncio.Event()

        async def on_response(response):
            content_type = response.headers.get("content-type", "")
            if response.status == 200 and "json" in content_type:
                try:
                    body = await response.json()
                    captured_responses.append({"url": response.url, "body": body})
                    if url_pattern and url_pattern in response.url:
                        response_event.set()
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            await page_goto(page, page_url)
            try:
                await asyncio.wait_for(response_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            # Extra wait for trailing API calls
            await asyncio.sleep(2)
        finally:
            page.remove_listener("response", on_response)

        # Return pattern-matched response if available
        if url_pattern:
            matched = [r for r in captured_responses if url_pattern in r["url"]]
            if matched:
                return matched[0]["body"]

        # Fallback: largest JSON response (most likely the data payload)
        if captured_responses:
            return max(captured_responses,
                       key=lambda r: len(json.dumps(r["body"], default=str)))["body"]

        return None


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
            await asyncio.sleep(2)  # let server-rendered content settle

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
# Parsers (defensive multi-key lookups for unknown schemas)
# ---------------------------------------------------------------------------

def _parse_applied_jobs(data) -> list:
    """Parse Naukri applied-jobs API response into normalized format.

    Handles two schemas:
    - History API (applyDetails): {jobId, jobTitle, company, appliedDate, location, ...}
    - Generic fallback: {jobs, jobDetails, applications, data, ...}
    """
    items = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("applyDetails", "appliedJobs", "jobs", "jobDetails",
                     "applications", "data", "appliedJobList", "results"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break

    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("jobId") or item.get("job_id") or item.get("id") or "")
        if not job_id:
            continue

        # Extract status — history API uses statusMsg or nested status list
        raw_status = item.get("statusMsg") or item.get("status") or item.get("applicationStatus")
        if isinstance(raw_status, list) and raw_status:
            # History API: status is a list of {statusMsg, statusDate, ...}
            raw_status = raw_status[0].get("statusMsg") if isinstance(raw_status[0], dict) else raw_status

        jobs.append({
            "job_id": job_id,
            "title": item.get("jobTitle") or item.get("title") or item.get("designation"),
            "company": item.get("company") or item.get("companyName"),
            "status": _map_naukri_status(raw_status),
            "applied_date": item.get("appliedDate") or item.get("applied_date") or item.get("createdDate"),
            "salary": (item.get("salary")
                       or (item.get("salaryDetail", {}).get("label")
                           if isinstance(item.get("salaryDetail"), dict) else None)),
            "location": item.get("location") or item.get("cityName"),
            "url": item.get("jdUrl") or item.get("url"),
            "apply_type": item.get("applyType"),
            "recruiter_active": item.get("isRecruiterActive"),
        })
    return jobs


def _parse_saved_jobs(data) -> list:
    """Parse Naukri saved-jobs API response into normalized format."""
    items = []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("savedJobs", "jobs", "jobDetails", "data",
                     "savedJobList", "results", "bookmarks"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break

    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("jobId") or item.get("job_id") or item.get("id") or "")
        if not job_id:
            continue
        jobs.append({
            "job_id": job_id,
            "title": item.get("title") or item.get("jobTitle") or item.get("designation"),
            "company": item.get("companyName") or item.get("company"),
            "saved_date": item.get("savedDate") or item.get("createdDate") or item.get("savedAt"),
            "salary": item.get("salary"),
            "location": item.get("location") or item.get("cityName"),
            "url": item.get("url") or item.get("jdUrl"),
        })
    return jobs


def _map_naukri_status(status) -> str:
    """Map Naukri's internal status labels to our tracking statuses."""
    if not status:
        return "applied"
    s = str(status).lower()
    mapping = {
        "applied": "applied",
        "viewed": "viewed_by_recruiter",
        "shortlisted": "shortlisted",
        "rejected": "rejected",
        "not interested": "rejected",
        "interview": "interview",
        "hired": "hired",
    }
    for pattern, local_status in mapping.items():
        if pattern in s:
            return local_status
    return s


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def _merge_applications(local_apps: list, remote_jobs: list) -> dict:
    """Merge remote applied jobs into local list. Mutates local_apps in place.

    Rules:
    - Existing job_id: update status/title/company from remote, preserve local fields.
    - New job_id: add with source="naukri_sync".
    - Local-only jobs are left untouched.
    """
    local_by_id = {a["job_id"]: a for a in local_apps if a.get("job_id")}
    now = datetime.now(timezone.utc).isoformat()

    new_added = 0
    updated = 0
    unchanged = 0
    remote_ids = set()

    for rj in remote_jobs:
        rid = rj["job_id"]
        remote_ids.add(rid)

        if rid in local_by_id:
            existing = local_by_id[rid]
            changed = False
            for field in ("title", "company", "status"):
                if rj.get(field) and rj[field] != existing.get(field):
                    existing[field] = rj[field]
                    changed = True
            for field in ("applied_date", "salary", "location", "url"):
                if rj.get(field) and field not in existing:
                    existing[field] = rj[field]
            if changed:
                existing["last_synced"] = now
                updated += 1
            else:
                unchanged += 1
        else:
            entry = {
                "job_id": rid,
                "title": rj.get("title"),
                "company": rj.get("company"),
                "status": rj.get("status", "applied"),
                "applied_at": rj.get("applied_date") or now,
                "source": "naukri_sync",
                "last_synced": now,
            }
            for k, v in rj.items():
                if k not in entry and v is not None:
                    entry[k] = v
            local_apps.append(entry)
            new_added += 1

    local_only = sum(1 for a in local_apps
                     if a.get("job_id") and a["job_id"] not in remote_ids)

    return {"new_added": new_added, "updated": updated,
            "unchanged": unchanged, "local_only": local_only}


def _merge_saved_jobs(local_saved: list, remote_jobs: list) -> dict:
    """Merge remote saved jobs into local list. Mutates local_saved in place."""
    local_ids = {j["job_id"] for j in local_saved if j.get("job_id")}
    now = datetime.now(timezone.utc).isoformat()

    new_added = 0
    already_local = 0
    remote_ids = set()

    for rj in remote_jobs:
        rid = rj["job_id"]
        remote_ids.add(rid)

        if rid in local_ids:
            already_local += 1
        else:
            local_saved.append({
                "job_id": rid,
                "title": rj.get("title"),
                "company": rj.get("company"),
                "saved_at": rj.get("saved_date") or now,
                "source": "naukri_sync",
                "last_synced": now,
                **{k: v for k, v in rj.items()
                   if k not in ("job_id", "title", "company", "saved_date") and v is not None},
            })
            new_added += 1

    local_only = sum(1 for j in local_saved
                     if j.get("job_id") and j["job_id"] not in remote_ids)

    return {"new_added": new_added, "already_local": already_local,
            "local_only": local_only}


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_sync_applications(
    force_browser: bool = False,
) -> dict:
    """Sync applied jobs from Naukri.com into local tracking.

    Pulls your application history from Naukri's backend and merges
    with local applications.json. Preserves local-only fields (pending_questions,
    tracking_extra, source, etc.). New jobs from Naukri are added with
    source="naukri_sync".

    Tries three strategies in order:
    1. Direct REST API (if endpoint configured)
    2. Browser JSON intercept (if URL pattern known)
    3. HTML scraping of server-rendered applied-jobs page

    Args:
        force_browser: If True, skip REST API and use browser/scrape strategies.

    Returns:
        - {status: "success", method, total_remote, new_added, updated, unchanged, local_only, applications: [...first 20...]}
        - {status: "error", message}
    """
    try:
        remote_data = None
        remote_jobs = None
        method = "unknown"

        # Strategy 1: Paginated REST API (fetches all pages, returns raw entries)
        if not force_browser:
            rest_entries = await _fetch_applied_jobs_rest()
            if rest_entries is not None:
                remote_data = {"applyDetails": rest_entries}
                method = "rest_api"

        # Strategy 2: Browser JSON intercept (only if URL pattern known)
        if remote_data is None and _APPLIED_JOBS_URL_PATTERN:
            remote_data = await _fetch_via_browser(
                APPLIED_JOBS_PAGE,
                url_pattern=_APPLIED_JOBS_URL_PATTERN,
            )
            if remote_data is not None:
                method = "browser_intercept"

        # Strategy 3: HTML scraping (server-rendered page)
        if remote_data is None and remote_jobs is None:
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
            }

        # Normalize status labels
        for job in remote_jobs:
            if job.get("status"):
                job["status"] = _map_naukri_status(job["status"])
            else:
                job["status"] = "applied"

        async with _applications_lock:
            local_apps = _load_json(APPLICATIONS_FILE)
            stats = _merge_applications(local_apps, remote_jobs)
            _save_json(APPLICATIONS_FILE, local_apps)

        return {
            "status": "success",
            "method": method,
            "total_remote": len(remote_jobs),
            **stats,
            "applications": local_apps[:20],
        }
    except Exception as e:
        return {"status": "error", "message": f"Sync failed: {type(e).__name__}: {e!r}"}


@mcp.tool()
async def naukri_sync_saved_jobs(
    force_browser: bool = False,
) -> dict:
    """Sync saved/bookmarked jobs from Naukri.com into local tracking.

    Pulls your saved jobs from Naukri's backend and merges with local
    saved_jobs.json. Jobs saved locally but not on Naukri are preserved.

    Tries direct REST API first, falls back to browser interception.

    Args:
        force_browser: If True, skip REST API and use browser interception.

    Returns:
        - {status: "success", method, total_remote, new_added, already_local, local_only, saved_jobs: [...first 20...]}
        - {status: "error", message}
    """
    try:
        remote_data = None
        method = "unknown"

        if not force_browser:
            remote_data = await _fetch_via_rest(SAVED_JOBS_API)
            if remote_data is not None:
                method = "rest_api"

        if remote_data is None:
            remote_data = await _fetch_via_browser(
                SAVED_JOBS_PAGE,
                url_pattern=_SAVED_JOBS_URL_PATTERN,
            )
            method = "browser_intercept"

        if remote_data is None:
            return {
                "status": "error",
                "message": "Could not fetch saved jobs. Run naukri_debug(action='discover') to find the API endpoint.",
            }

        remote_jobs = _parse_saved_jobs(remote_data)

        async with _saved_jobs_lock:
            local_saved = _load_json(SAVED_JOBS_FILE)
            stats = _merge_saved_jobs(local_saved, remote_jobs)
            _save_json(SAVED_JOBS_FILE, local_saved)

        return {
            "status": "success",
            "method": method,
            "total_remote": len(remote_jobs),
            **stats,
            "saved_jobs": local_saved[:20],
        }
    except Exception as e:
        return {"status": "error", "message": f"Sync failed: {type(e).__name__}: {e!r}"}
