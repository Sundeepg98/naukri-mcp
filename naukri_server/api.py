"""
API helpers — aiohttp-based GET/POST/PUT/DELETE for Naukri REST endpoints.
"""

import asyncio
from typing import Optional
import json
import random
import time
from functools import wraps
from urllib.parse import urlencode

import aiohttp

from naukri_server.config import NAUKRI_BASE, API_HEADERS, API_TIMEOUT, API_MAX_RETRIES, API_BACKOFF_BASE, API_MAX_BACKOFF_SECONDS, logger
from naukri_server.browser import browser

RETRIABLE_STATUSES = {429, 502, 503, 504}
SUCCESS_STATUSES = {200, 201, 202, 204}

# --- Global shared session (avoids per-call ClientSession creation) ---
_session: aiohttp.ClientSession = None
_refresh_lock = asyncio.Lock()


class _ApiMetrics:
    """Simple request counters for observability."""
    def __init__(self):
        self.total = 0
        self.success = 0
        self.errors = 0
        self.retries = 0
        self.auth_refreshes = 0

    def get_stats(self) -> dict:
        return {
            "total_requests": self.total,
            "success": self.success,
            "errors": self.errors,
            "retries": self.retries,
            "auth_refreshes": self.auth_refreshes,
        }

api_metrics = _ApiMetrics()


class ApiCircuitBreaker:
    """Circuit breaker for API calls — prevents hammering a down service.

    States: closed (normal) → open (failing, reject calls) → half_open (allow one probe).
    """

    def __init__(self, failure_threshold=5, reset_timeout=60):
        self._failures = 0
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._state = "closed"
        self._last_failure = 0.0

    def record_success(self):
        self._failures = 0
        self._state = "closed"

    def record_failure(self):
        self._failures += 1
        if self._failures >= self._threshold:
            self._state = "open"
            self._last_failure = time.time()

    def can_execute(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if time.time() - self._last_failure > self._reset_timeout:
                self._state = "half_open"
                return True
            return False
        return True  # half_open: allow one probe attempt

    @property
    def state(self) -> str:
        # Re-check for auto-transition from open to half_open on read
        if self._state == "open" and time.time() - self._last_failure > self._reset_timeout:
            self._state = "half_open"
        return self._state


_api_circuit = ApiCircuitBreaker()


class NaukriAPIError(Exception):
    """Structured API error with status code and parsed message."""
    def __init__(self, status: int, message: str, code: Optional[str] = None):
        self.status = status
        self.message = message
        self.code = code
        super().__init__(f"HTTP {status}: {message}")


def api_tool(context: Optional[str] = None):
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
                error_code = "AUTH_ERROR" if e.status == 401 else "API_ERROR"
                return {"status": "error", "message": str(e), "http_status": e.status, "error_code": error_code}
            except Exception as e:
                label = context or func.__name__.replace("naukri_", "").replace("_", " ").title()
                return {"status": "error", "message": f"{label} failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}
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


_session_lock = asyncio.Lock()

async def get_session() -> aiohttp.ClientSession:
    """Lazily create and return a shared aiohttp session."""
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
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


async def close_api_session():
    """Close the global aiohttp session if it exists."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        _session = None


async def _api_request(method: str, path: str, params: Optional[dict] = None,
                       body=None, extra_headers: Optional[dict] = None,
                       _attempt: int = 0) -> dict:
    """Unified HTTP request handler with retry, 401 refresh, circuit breaker, and error parsing."""
    if _attempt == 0:
        api_metrics.total += 1
        if not _api_circuit.can_execute():
            raise NaukriAPIError(503, f"Circuit breaker open — API unavailable (resets in {int(_api_circuit._reset_timeout - (time.time() - _api_circuit._last_failure))}s)")
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

    try:
        async with session.request(method, url, **kwargs) as resp:
            logger.info("API %s %s -> %s", method, path, resp.status)
            if resp.status in SUCCESS_STATUSES:
                api_metrics.success += 1
                _api_circuit.record_success()
                if resp.status == 204:
                    return {}
                content_type = resp.headers.get("content-type", "").lower()
                if "json" not in content_type and "javascript" not in content_type:
                    text = await resp.text()
                    raise NaukriAPIError(resp.status, f"Expected JSON response, got {content_type}: {text[:200]}")
                return await resp.json()
            text = await resp.text()

            # 401 — token refresh with lock to prevent parallel refresh storms
            if resp.status == 401 and _attempt == 0:
                api_metrics.auth_refreshes += 1
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

            # 429 — rate limit with Retry-After / exponential backoff
            if resp.status == 429 and _attempt < API_MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        delay = float(retry_after)
                    except (ValueError, TypeError):
                        delay = min(2 ** _attempt, API_MAX_BACKOFF_SECONDS)
                else:
                    delay = min(2 ** _attempt, API_MAX_BACKOFF_SECONDS)
                api_metrics.retries += 1
                logger.info("Rate limited (429), retrying after %.1fs...", delay)
                await asyncio.sleep(delay)
                return await _api_request(method, path, params, body,
                                          extra_headers, _attempt=_attempt + 1)

            # Transient server errors — exponential backoff with jitter
            if resp.status in RETRIABLE_STATUSES and resp.status != 429 and _attempt < API_MAX_RETRIES:
                delay = API_BACKOFF_BASE * (2 ** _attempt) * (0.5 + random.random())
                api_metrics.retries += 1
                logger.info("Transient error %s, retrying in %.1fs (attempt %d)...", resp.status, delay, _attempt + 1)
                await asyncio.sleep(delay)
                return await _api_request(method, path, params, body,
                                          extra_headers, _attempt=_attempt + 1)

            api_metrics.errors += 1
            _api_circuit.record_failure()
            _raise_api_error(resp.status, text)
    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
        if _attempt < API_MAX_RETRIES:
            delay = API_BACKOFF_BASE * (2 ** _attempt) * (0.5 + random.random())
            api_metrics.retries += 1
            logger.info("Transport error (%s), retrying in %.1fs (attempt %d)...",
                        type(e).__name__, delay, _attempt + 1)
            await asyncio.sleep(delay)
            return await _api_request(method, path, params, body,
                                      extra_headers, _attempt=_attempt + 1)
        api_metrics.errors += 1
        _api_circuit.record_failure()
        raise NaukriAPIError(0, f"Connection failed after {API_MAX_RETRIES} retries: {type(e).__name__}: {e}")


# --- Public thin wrappers (preserve existing signatures) ---

async def api_get(path: str, params: Optional[dict] = None, extra_headers: Optional[dict] = None,
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


async def api_delete(path: str, body: Optional[dict] = None, _attempt: int = 0) -> dict:
    """DELETE request to Naukri API."""
    return await _api_request("DELETE", path, body=body, _attempt=_attempt)
