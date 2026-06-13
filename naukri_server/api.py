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

from naukri_server.config import NAUKRI_BASE, API_HEADERS, API_TIMEOUT, API_MAX_RETRIES, API_BACKOFF_BASE, API_MAX_BACKOFF_SECONDS, BLOCK_STATE_CLASSIFIER_ENFORCE, logger
from naukri_server.browser import browser, AuthExpiredError
from naukri_server.block_state import classify_block_state, BlockState, BlockAssessment

RETRIABLE_STATUSES = {429, 502, 503, 504}
SUCCESS_STATUSES = {200, 201, 202, 204}
# Akamai Bot Manager flags requests it classifies as automation with 406/403
# (see README "Akamai CDN Blocks"). These are NOT generic transient errors and
# must NOT be retry-hammered — hammering deepens a block. Treat them as a
# bot-check / re-auth signal: refresh the (likely stale) token ONCE behind the
# refresh lock, apply a cooldown, then surface a clear "re-auth needed" error.
BOT_CHECK_STATUSES = {403, 406}
# Cooldown (seconds) applied after a bot-check before the single re-auth retry.
# Deliberately a real wait (not jittered-tiny) so we back off the edge instead
# of immediately re-hitting it. Capped by API_MAX_BACKOFF_SECONDS.
BOT_CHECK_COOLDOWN_SECONDS = min(5.0, API_MAX_BACKOFF_SECONDS)

# --- Global shared session (avoids per-call ClientSession creation) ---
_session: aiohttp.ClientSession = None
# NOTE: token-refresh serialization now lives on the SINGLE TokenManager lock
# (browser.token_manager._refresh_lock), reached via refresh_via_pool(). The old
# module-level api lock was a SECOND, separate lock — a 401 here and a concurrent
# ensure_token() serialized on different locks → refresh storm + a pool page
# acquired while a lock was held → deadlock risk at MAX_TABS. Both paths now share
# one lock and check out the pool page inside it. (Fix B)


class _ApiMetrics:
    """Simple request counters for observability."""
    def __init__(self):
        self.total = 0
        self.success = 0
        self.errors = 0
        self.retries = 0
        self.auth_refreshes = 0
        # Count of responses the block-state classifier flagged as non-healthy
        # (soft_block / captcha / rate_limited / login_wall). A soft block that
        # arrives as a 200 interstitial would otherwise be invisible — this is
        # the probe signal that distinguishes "API healthy" from "API is being
        # silently blocked". Broken down per state for diagnostics.
        self.blocks = 0
        self.blocks_by_state: dict[str, int] = {}

    def record_block(self, state: str) -> None:
        """Increment the block counters for a classified non-healthy state."""
        self.blocks += 1
        self.blocks_by_state[state] = self.blocks_by_state.get(state, 0) + 1

    def get_stats(self) -> dict:
        return {
            "total_requests": self.total,
            "success": self.success,
            "errors": self.errors,
            "retries": self.retries,
            "auth_refreshes": self.auth_refreshes,
            "blocks": self.blocks,
            "blocks_by_state": dict(self.blocks_by_state),
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
    """Structured API error with status code and parsed message.

    ``block_kind`` (optional) carries the block-state classifier's verdict when
    the error was raised because a response was classified as a block (e.g. a
    200 CAPTCHA interstitial, or a 403/406 bot-check). It is one of the
    ``BlockState`` values ("soft_block" / "captcha" / "rate_limited" /
    "login_wall") or None for ordinary API errors. Callers (kill-switch,
    health) can branch on it without re-parsing the message.
    """
    def __init__(self, status: int, message: str, code: Optional[str] = None,
                 block_kind: Optional[str] = None):
        self.status = status
        self.message = message
        self.code = code
        self.block_kind = block_kind
        super().__init__(f"HTTP {status}: {message}")


class NaukriBotCheckError(NaukriAPIError):
    """Akamai Bot Manager block (406/403) — re-auth + cooldown needed.

    Subclasses NaukriAPIError so existing ``except NaukriAPIError`` handlers
    still catch it, but the distinct type + ``code="BOT_CHECK"`` let callers
    detect a bot-flag specifically (vs a generic API error) and prompt re-auth
    rather than blindly retrying.
    """
    def __init__(self, status: int, message: str, block_kind: Optional[str] = None):
        super().__init__(status, message, code="BOT_CHECK",
                         block_kind=block_kind or BlockState.SOFT_BLOCK.value)


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
                if e.code == "BOT_CHECK":
                    error_code = "BOT_CHECK"
                elif e.status == 401:
                    error_code = "AUTH_ERROR"
                else:
                    error_code = "API_ERROR"
                return {"status": "error", "message": str(e), "http_status": e.status, "error_code": error_code}
            except Exception as e:
                label = context or func.__name__.replace("naukri_", "").replace("_", " ").title()
                return {"status": "error", "message": f"{label} failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}
        return wrapper
    return decorator


def _raise_api_error(status: int, text: str, block_kind: Optional[str] = None):
    """Parse JSON error body and raise structured NaukriAPIError.

    ``block_kind`` (from the block-state classifier) is attached to the raised
    error so downstream handlers can tell a bot-block apart from an ordinary
    API error without re-parsing the body.
    """
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
    raise NaukriAPIError(status, message, code, block_kind=block_kind)


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


def _assess_block(resp, text: str) -> BlockAssessment:
    """Run the pure block-state classifier over an aiohttp response + body text.

    Pulls the (already-received) status, content-type, redirect history and
    final URL off ``resp`` and hands them to the pure classifier. Never raises.
    """
    try:
        content_type = resp.headers.get("content-type", "")
        redirected = bool(getattr(resp, "history", None))
        final_url = str(getattr(resp, "url", "") or "")
        return classify_block_state(
            status=resp.status,
            content_type=content_type,
            body=text,
            redirected=redirected,
            final_url=final_url,
        )
    except Exception as exc:  # classifier must never break the request path
        logger.debug("block-state classifier errored (ignored): %s", exc)
        return BlockAssessment(BlockState.HEALTHY)


def _handle_block_assessment(assessment: BlockAssessment, method: str, path: str,
                             status: int) -> None:
    """Log + count a non-healthy classification.

    LOG-ONLY by default (see config.BLOCK_STATE_CLASSIFIER_ENFORCE): this always
    records the block in api_metrics and logs it, but does NOT itself alter
    control flow. Enforcement (kill-switch trip, raising tagged errors) is layered
    on top by callers reading ``api_metrics`` / the tagged ``block_kind`` once the
    classifier is trusted. Keeping this side-effecting step centralized means the
    log-only→enforce switch is observable in one place.
    """
    if not assessment.is_block:
        return
    api_metrics.record_block(assessment.state.value)
    log = logger.warning if BLOCK_STATE_CLASSIFIER_ENFORCE else logger.info
    log(
        "block-state=%s (%s) on %s %s [HTTP %s] enforce=%s — signal=%s",
        assessment.state.value, assessment.reason, method, path, status,
        BLOCK_STATE_CLASSIFIER_ENFORCE, assessment.signal,
    )


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
                # 204 No Content: nothing to inspect — a genuine success.
                if resp.status == 204:
                    api_metrics.success += 1
                    _api_circuit.record_success()
                    return {}
                content_type = resp.headers.get("content-type", "").lower()
                # --- BUG FIX (block-state corruption) -------------------------
                # Previously success was recorded HERE, before the content-type
                # guard. A 200 that is actually a CAPTCHA/HTML interstitial would
                # bump api_metrics.success + reset the circuit breaker, THEN raise
                # — so a soft-blocked session read as "healthy" and corrupted the
                # error-rate probe + breaker. We now read + classify the body and
                # only record success once we've confirmed it is a real JSON
                # payload (not an interstitial). The non-JSON guard, the
                # classifier, and success-recording all happen AFTER inspection.
                if "json" not in content_type and "javascript" not in content_type:
                    text = await resp.text()
                    assessment = _assess_block(resp, text)
                    _handle_block_assessment(assessment, method, path, resp.status)
                    # A non-JSON 2xx is never a usable API success — it's an
                    # interstitial/HTML. Count it as an error (NOT success) and
                    # let the circuit see a failure. Tag with the block verdict.
                    api_metrics.errors += 1
                    _api_circuit.record_failure()
                    block_kind = assessment.state.value if assessment.is_block else None
                    if assessment.is_block:
                        raise NaukriBotCheckError(
                            resp.status,
                            f"Soft block on {path}: {assessment.reason} "
                            f"(HTTP {resp.status}, content-type={content_type}): {text[:200]}",
                            block_kind=block_kind,
                        )
                    raise NaukriAPIError(
                        resp.status,
                        f"Expected JSON response, got {content_type}: {text[:200]}",
                    )
                # Content-type is JSON-ish — the happy path. A genuine JSON API
                # payload is healthy by definition; an interstitial arrives as
                # HTML and is caught by the non-JSON guard above. We record
                # success only now (AFTER the guard) and parse the body. The
                # response body is consumed once, so we do NOT additionally
                # re-read it for classification here (that would require a second
                # read on a real aiohttp response).
                api_metrics.success += 1
                _api_circuit.record_success()
                return await resp.json()
            text = await resp.text()

            # Classify every non-success response (log-only counter side-effect)
            # so 403/406/redirect-to-login bodies feed api_metrics.blocks and the
            # raised error can be tagged with the specific block_kind below.
            err_assessment = _assess_block(resp, text)
            _handle_block_assessment(err_assessment, method, path, resp.status)
            err_block_kind = err_assessment.state.value if err_assessment.is_block else None

            # 401 — token refresh. Serialized through the TokenManager's SINGLE
            # refresh lock (Fix B): both this path and a concurrent ensure_token()
            # now contend for the SAME lock, so a 401 and a background renewal
            # can't both refresh (no storm). The page checkout happens INSIDE
            # that locked path (refresh_via_pool), so we never hold the refresh
            # lock while separately blocking on a pool page (no deadlock).
            if resp.status == 401 and _attempt == 0:
                api_metrics.auth_refreshes += 1
                logger.info("Token expired, refreshing and retrying...")
                # Pass the rejected token as `stale_token` so refresh_via_pool
                # de-dupes: if another caller already re-minted a DIFFERENT token
                # while we waited for the shared lock, we reuse it instead of
                # re-refreshing (collapses a concurrent storm to one refresh).
                try:
                    await browser.token_manager.refresh_via_pool(browser.page_pool, stale_token=token)
                except AuthExpiredError as e:
                    # Session is truly gone (login-wall / checkpoint) — surface a
                    # clear re-auth signal and trip the circuit; do NOT retry.
                    api_metrics.errors += 1
                    _api_circuit.record_failure()
                    logger.error("Token refresh after 401 hit auth expiry: %s", e)
                    raise NaukriAPIError(
                        401,
                        f"Session expired and re-auth required: {e}",
                    )
                except Exception as e:
                    logger.error("Token refresh failed: %s: %s", type(e).__name__, e)
                    raise NaukriAPIError(
                        401,
                        f"Session expired and token refresh failed: {e}. Call naukri_login to re-authenticate.",
                    )
                return await _api_request(method, path, params, body,
                                          extra_headers, _attempt=1)

            # 406/403 — Akamai bot-check. DO NOT retry-hammer (that worsens a
            # block). The README documents an expired token as the usual cause
            # of being classified as bot traffic, so on the first hit we refresh
            # the (stale) token behind the lock + cool down, then retry ONCE.
            # If it still trips, surface a clear "bot-check / re-auth needed"
            # error and trip the circuit breaker instead of looping.
            if resp.status in BOT_CHECK_STATUSES:
                if _attempt == 0:
                    api_metrics.auth_refreshes += 1
                    logger.warning(
                        "Bot-check (HTTP %s) on %s — refreshing token + cooling down %.1fs before one retry",
                        resp.status, path, BOT_CHECK_COOLDOWN_SECONDS,
                    )
                    # Same unified, deadlock-free refresh path as the 401 case
                    # (Fix B): one shared lock; the pool page is checked out
                    # inside refresh_via_pool, never while separately holding a
                    # lock. The stale_token arg lets its double-check skip the
                    # browser round-trip if another caller already re-minted it.
                    try:
                        await browser.token_manager.refresh_via_pool(browser.page_pool, stale_token=token)
                    except AuthExpiredError as e:
                        logger.error("Token refresh after bot-check hit auth expiry: %s", e)
                        api_metrics.errors += 1
                        _api_circuit.record_failure()
                        raise NaukriBotCheckError(
                            resp.status,
                            f"Bot-check (HTTP {resp.status}) and session expired: {e}",
                            block_kind=err_block_kind or BlockState.LOGIN_WALL.value,
                        )
                    except Exception as e:
                        logger.error("Token refresh after bot-check failed: %s: %s", type(e).__name__, e)
                        api_metrics.errors += 1
                        _api_circuit.record_failure()
                        raise NaukriBotCheckError(
                            resp.status,
                            f"Bot-check (HTTP {resp.status}) and token refresh failed: {e}. "
                            "Call naukri_login to re-authenticate.",
                            block_kind=err_block_kind,
                        )
                    # Cool down before the single retry — back off the edge.
                    await asyncio.sleep(BOT_CHECK_COOLDOWN_SECONDS)
                    return await _api_request(method, path, params, body,
                                              extra_headers, _attempt=_attempt + 1)
                # Persisted past the re-auth retry — surface clearly, trip circuit.
                api_metrics.errors += 1
                _api_circuit.record_failure()
                logger.error("Bot-check (HTTP %s) persisted on %s after re-auth", resp.status, path)
                raise NaukriBotCheckError(
                    resp.status,
                    f"Bot-check (HTTP {resp.status}) persisted after re-auth on {path}. "
                    "Naukri's Akamai Bot Manager is blocking REST access — "
                    "call naukri_login to re-authenticate, or use a browser-backed tool.",
                    block_kind=err_block_kind,
                )

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
            _raise_api_error(resp.status, text, block_kind=err_block_kind)
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
