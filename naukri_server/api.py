"""
API helpers — aiohttp-based GET/POST/PUT/DELETE for Naukri REST endpoints.
"""

import asyncio
import json
from functools import wraps
from urllib.parse import urlencode

import aiohttp

from naukri_server.config import NAUKRI_BASE, API_HEADERS, API_TIMEOUT, logger
from naukri_server.browser import browser

RETRIABLE_STATUSES = {429, 502, 503, 504}
SUCCESS_STATUSES = {200, 201, 202, 204}
MAX_RETRIES = 2
BACKOFF_BASE = 1.0

# --- Global shared session (avoids per-call ClientSession creation) ---
_session: aiohttp.ClientSession = None
_refresh_lock = asyncio.Lock()


class NaukriAPIError(Exception):
    """Structured API error with status code and parsed message."""
    def __init__(self, status: int, message: str, code: str = None):
        self.status = status
        self.message = message
        self.code = code
        super().__init__(f"HTTP {status}: {message}")


def api_tool(context: str = None):
    """Decorator: wraps async MCP tool with standardized error handling.

    Catches NaukriAPIError and Exception, returns {status: "error", message, http_status}.
    Use only on tools with a single top-level try/except pattern.
    Tools with custom error handling should NOT use this:
    apply, batch_apply, get_job, sync_applications, sync_saved_jobs,
    boost_visibility, get_profile, update_profile, audit_profile,
    upload_photo, delete_photo, accept_nvite, update_job_alert,
    delete_job_alert, get_company_slug, smart_apply, compare_jobs,
    auto_hunt, skill_gap_analysis, resume_tailor, daily_brief,
    research_company, export_data.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except NaukriAPIError as e:
                return {"status": "error", "message": str(e), "http_status": e.status}
            except Exception as e:
                label = context or func.__name__.replace("naukri_", "").replace("_", " ").title()
                return {"status": "error", "message": f"{label} failed: {type(e).__name__}: {e}"}
        return wrapper
    return decorator


def _raise_api_error(status: int, text: str):
    """Parse JSON error body and raise structured NaukriAPIError."""
    message = text[:500]
    code = None
    try:
        data = json.loads(text)
        message = data.get("message") or data.get("error", {}).get("message") or message
        code = data.get("code") or data.get("error", {}).get("code")
    except (json.JSONDecodeError, AttributeError):
        stripped = text.strip()
        if stripped.startswith("<") or stripped.lower().startswith("<!doctype"):
            message = f"Server returned HTTP {status} (non-JSON response)"
    raise NaukriAPIError(status, message, code)


def _cookie_header() -> str:
    """Get cached cookie header from token manager."""
    return browser.token_manager.get_cookies()


async def get_session() -> aiohttp.ClientSession:
    """Lazily create and return a shared aiohttp session."""
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            limit=20,
            limit_per_host=10,
            ttl_dns_cache=300,
        )
        _session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            trust_env=True,
        )
    return _session


async def close_session():
    """Close the shared aiohttp session (for cleanup)."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        _session = None


async def _api_request(method: str, path: str, params: dict = None,
                       body=None, extra_headers: dict = None,
                       _attempt: int = 0) -> dict:
    """Unified HTTP request handler with retry, 401 refresh, and error parsing."""
    token = await browser.token_manager.ensure_token()
    cookie_str = _cookie_header()
    headers = {**API_HEADERS, "Authorization": f"Bearer {token}", "cookie": cookie_str}
    if extra_headers:
        headers.update(extra_headers)

    url = f"{NAUKRI_BASE}{path}" if path.startswith("/") else path
    if params:
        url = f"{url}?{urlencode(params)}"

    logger.info("API %s %s", method, url)
    session = await get_session()

    kwargs = {"headers": headers}
    if method in ("POST", "PUT"):
        kwargs["json"] = body
    elif method == "DELETE" and body is not None:
        kwargs["json"] = body

    async with session.request(method, url, **kwargs) as resp:
        logger.info("API %s %s -> %s", method, path, resp.status)
        if resp.status in SUCCESS_STATUSES:
            if resp.status == 204:
                return {}
            return await resp.json()
        text = await resp.text()

        # 401 — token refresh with lock to prevent parallel refresh storms
        if resp.status == 401 and _attempt == 0:
            logger.info("Token expired, refreshing and retrying...")
            async with _refresh_lock:
                # Re-get token — another caller may have refreshed while we waited
                new_token = await browser.token_manager.ensure_token()
                if new_token == token:  # Still the same stale token
                    browser.token_manager.invalidate()
                    try:
                        async with browser.page_pool.acquire() as refresh_page:
                            await browser.token_manager.refresh(refresh_page)
                    except Exception as e:
                        logger.error("Token refresh failed: %s: %s", type(e).__name__, e)
                        raise NaukriAPIError(
                            401,
                            f"Session expired and token refresh failed: {e}. Call naukri_login to re-authenticate.",
                        )
            return await _api_request(method, path, params, body,
                                      extra_headers, _attempt=1)

        # Transient errors — exponential backoff retry
        if resp.status in RETRIABLE_STATUSES and _attempt < MAX_RETRIES:
            delay = BACKOFF_BASE * (2 ** _attempt)
            logger.info("Transient error %s, retrying after %.1fs...", resp.status, delay)
            await asyncio.sleep(delay)
            return await _api_request(method, path, params, body,
                                      extra_headers, _attempt=_attempt + 1)

        _raise_api_error(resp.status, text)


# --- Public thin wrappers (preserve existing signatures) ---

async def api_get(path: str, params: dict = None, extra_headers: dict = None,
                  _attempt: int = 0) -> dict:
    """GET request to Naukri API."""
    return await _api_request("GET", path, params=params,
                              extra_headers=extra_headers, _attempt=_attempt)


async def api_post(path: str, body: dict, _attempt: int = 0) -> dict:
    """POST request to Naukri API."""
    return await _api_request("POST", path, body=body, _attempt=_attempt)


async def api_put(path: str, body: dict, _attempt: int = 0) -> dict:
    """PUT request to Naukri API."""
    return await _api_request("PUT", path, body=body, _attempt=_attempt)


async def api_delete(path: str, body: dict = None, _attempt: int = 0) -> dict:
    """DELETE request to Naukri API."""
    return await _api_request("DELETE", path, body=body, _attempt=_attempt)
