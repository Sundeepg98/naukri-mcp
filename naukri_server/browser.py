"""
Browser state — Playwright browser with multi-tab page pool and cached token manager.
"""

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page
from playwright._impl._errors import TargetClosedError, TimeoutError as PlaywrightTimeoutError

from naukri_server.config import CHROME_PROFILE, NAUKRI_BASE, NAV_TIMEOUT, ELEMENT_TIMEOUT, MAX_TABS, CDP_PORT, PROFILE_API, SESSION_VALIDATE_TIMEOUT, TOKEN_RENEWAL_TIMEOUT, POOL_CHECKOUT_TIMEOUT, logger
from naukri_server import profile_lock


class AuthExpiredError(Exception):
    """Token refresh failed for an auth reason that re-trying will NOT fix:
    the session itself is gone (Naukri served a login wall / checkpoint, or the
    renewal navigation landed somewhere other than a logged-in page) so the
    short-lived JWT could not be re-minted. The caller must surface a clear
    "re-authenticate (naukri_login)" signal rather than treat it as transient.

    Distinct from a *transient* failure (navigation timeout, target closed,
    network blip) where the session may still be valid and a later retry can
    succeed — those are re-raised as their original exception type so existing
    backoff/retry logic still applies.
    """
    pass


class TokenManager:
    """Caches JWT token + cookies from browser context. Lock-free reads, locked refresh."""

    def __init__(self):
        self._token: Optional[str] = None
        self._cookies: Optional[str] = None
        self._refresh_lock = asyncio.Lock()
        self._context: Optional[BrowserContext] = None

    def bind(self, context: BrowserContext):
        """Bind to a browser context for cookie extraction."""
        self._context = context

    _AUTH_STATE_FILE = Path(CHROME_PROFILE) / "auth_state.json"

    async def extract(self):
        """Pull token + cookies from browser context into cache."""
        if not self._context:
            return
        try:
            cookies = await self._context.cookies(NAUKRI_BASE)
            self._cookies = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            for c in cookies:
                if c["name"] == "nauk_at":
                    self._token = c["value"]
                    self._export_auth_state()
                    return
        except Exception as e:
            logger.debug("Token extraction failed: %s", e)
        self._token = None

    def _export_auth_state(self):
        """Write current token + cookies to file for cross-process agents."""
        try:
            state = {"token": self._token, "cookies": self._cookies or "", "exported_at": time.time(), "cdp_port": CDP_PORT}
            tmp = self._AUTH_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(self._AUTH_STATE_FILE)
            os.chmod(str(self._AUTH_STATE_FILE), 0o600)
        except Exception as e:
            logger.debug("Auth state export failed: %s", e)

    def get_token(self) -> str:
        """Get cached JWT token. Raises if not available."""
        if self._token:
            return self._token
        raise ValueError("Not logged in — call naukri_login first")

    def get_cookies(self) -> str:
        """Get cached cookie header string."""
        return self._cookies or ""

    def invalidate(self):
        """Mark token as stale. Next ensure_token() will refresh."""
        self._token = None

    async def _do_refresh(self, page: Page) -> Optional[str]:
        """Reload ``page`` and re-extract the token. Caller MUST hold ``_refresh_lock``.

        Failure handling (Fix A — no silent swallow): the reload result is
        classified rather than ignored.
          * Navigation timeout / target-closed / connection blip → *transient*:
            the session may still be valid, so the original exception is
            re-raised for the caller's existing backoff/retry to handle.
          * Navigation succeeds but no ``nauk_at`` reappears → *auth expiry /
            login-wall / checkpoint*: re-trying won't help; raise
            ``AuthExpiredError`` so the caller can surface "re-authenticate".
        Both paths are LOGGED (not debug-swallowed) so a checkpoint is no longer
        indistinguishable from a transient timeout.
        """
        try:
            await page.reload(timeout=NAV_TIMEOUT)
        except (PlaywrightTimeoutError, TargetClosedError, asyncio.TimeoutError) as e:
            # Transient: nav didn't complete. Don't swallow — log + re-raise
            # so the API layer's retry/backoff can act on it.
            logger.warning(
                "Token refresh reload failed (transient %s) — session may still be valid, "
                "surfacing for retry", type(e).__name__,
            )
            raise
        except Exception as e:
            logger.warning("Token refresh reload failed (transient %s): %s", type(e).__name__, e)
            raise
        await self.extract()
        if not self._token:
            # Reached a page but no auth cookie reappeared → the session is
            # gone (login wall / checkpoint), not a transient timeout.
            logger.error(
                "Token refresh navigated successfully but no nauk_at cookie returned — "
                "session expired (login-wall / checkpoint). Re-authentication required."
            )
            raise AuthExpiredError(
                "Session expired — Naukri returned no auth token after reload "
                "(login-wall or checkpoint). Call naukri_login to re-authenticate."
            )
        return self._token

    async def refresh(self, page: Page) -> Optional[str]:
        """Re-extract token by reloading a page. Protected by lock to prevent parallel refreshes.

        See ``_do_refresh`` for the (non-swallowing) failure classification.
        """
        async with self._refresh_lock:
            # Double-check after acquiring lock — another refresh may have succeeded
            if self._token:
                return self._token
            return await self._do_refresh(page)

    async def refresh_via_pool(self, page_pool, stale_token: Optional[str] = None) -> Optional[str]:
        """Full token refresh that owns BOTH the single refresh lock AND the page
        checkout, used by the API layer (Fix B).

        Why this exists: previously ``api.py`` held its OWN module-level lock,
        then *separately* called ``page_pool.acquire()`` and ``refresh(page)``
        (which grabbed a *different* lock). That meant (a) a 401 in api.py and a
        concurrent ``ensure_token()`` serialized on different locks → refresh
        storm, and (b) the pool page was checked out while a lock was held →
        deadlock risk at MAX_TABS. Routing every refresh through this one method
        unifies serialization on ``self._refresh_lock`` and ensures only ONE
        page is ever checked out for refresh at a time (the lock gates it), so
        concurrent refreshers can't exhaust the pool against each other.

        ``stale_token`` is the (now-rejected) token the caller just used. If,
        after acquiring the lock, the cached token is present AND different from
        ``stale_token``, another refresher already re-minted it — we skip the
        browser round-trip entirely (this is what collapses a concurrent storm
        to a single real refresh). Pass ``None`` to force a refresh.

        The page is acquired INSIDE the lock — but because the lock already
        serializes refreshers to one-at-a-time, at most a single refresh page is
        outstanding, so this cannot self-deadlock the pool.
        """
        async with self._refresh_lock:
            # Another refresher already produced a NEW token while we waited for
            # the lock → reuse it, skip the expensive browser round-trip.
            if self._token and self._token != stale_token:
                return self._token
            self._token = None  # our token is stale → force _do_refresh to reload
            async with page_pool.acquire() as page:
                return await self._do_refresh(page)

    async def ensure_token(self) -> str:
        """Get token, attempting extraction if cache is empty.

        If extraction from cached cookies fails, navigates to a Naukri page
        to trigger JWT renewal (the session cookie may still be valid even
        when the short-lived nauk_at JWT has expired). Uses _refresh_lock to
        prevent parallel navigation races.
        """
        if self._token:
            return self._token
        await self.extract()
        if self._token:
            return self._token
        # JWT missing/expired — try navigating to trigger server-side renewal
        # Lock prevents multiple parallel callers from navigating simultaneously
        async with self._refresh_lock:
            # Double-check after acquiring lock — another caller may have renewed
            if self._token:
                return self._token
            if self._context:
                try:
                    pages = self._context.pages
                    page = pages[0] if pages else await self._context.new_page()
                    await page.goto(f"{NAUKRI_BASE}/mnjuser/homepage",
                                    wait_until="domcontentloaded", timeout=TOKEN_RENEWAL_TIMEOUT)
                    await self.extract()
                    if not self._token:
                        # Reached the homepage but no auth cookie was reissued —
                        # the session is gone (login-wall / checkpoint), not a
                        # transient blip. Log at ERROR so it is distinguishable.
                        logger.error(
                            "Token renewal navigation completed but no nauk_at cookie returned — "
                            "session expired (login-wall / checkpoint). Re-authentication required."
                        )
                except (PlaywrightTimeoutError, TargetClosedError, asyncio.TimeoutError) as e:
                    # Transient navigation failure — the session may still be
                    # valid; a later call can retry. Don't silently debug-swallow.
                    logger.warning(
                        "Token renewal navigation failed (transient %s) — will retry on next call",
                        type(e).__name__,
                    )
                except Exception as e:
                    logger.warning("Token renewal navigation failed (%s): %s", type(e).__name__, e)
        if not self._token:
            raise ValueError("Not logged in — call naukri_login first")
        return self._token


class BrowserUnavailableError(Exception):
    """Raised when circuit breaker is open — browser is known-dead."""
    pass


class PagePool:
    """Manages a pool of browser pages (tabs) with semaphore-based checkout."""

    _MAX_CRASHES = 10  # Max crashes before entering degraded mode

    def __init__(self, context: BrowserContext, max_pages: int = 3):
        self._context = context
        self._max_pages = max_pages
        self._semaphore = asyncio.Semaphore(max_pages)
        self._available: asyncio.Queue[Page] = asyncio.Queue()
        self._all_pages: list[Page] = []
        # Observability counters
        self._checkouts = 0
        self._returns = 0
        self._crashes = 0
        self._max_wait_ms = 0
        # Circuit breaker state
        self._circuit_state = "closed"  # closed, open, half_open
        self._circuit_failure_count = 0
        self._circuit_failure_threshold = 3
        self._circuit_open_time = 0.0
        self._circuit_cooldown = 30.0  # seconds before half-open

    async def initialize(self, first_page: Page):
        """Seed pool with the initial page from browser context."""
        self._all_pages.append(first_page)
        await self._available.put(first_page)

    async def _create_page(self) -> Page:
        """Create a new tab in the shared browser context."""
        page = await self._context.new_page()
        self._all_pages.append(page)
        logger.info("PagePool: created new tab (%d/%d)", len(self._all_pages), self._max_pages)
        return page

    async def _recover_page(self, dead_page: Page) -> Page:
        """Replace a crashed page with a new one."""
        logger.warning("PagePool: recovering crashed tab")
        if dead_page in self._all_pages:
            self._all_pages.remove(dead_page)
        page = await self._context.new_page()
        self._all_pages.append(page)
        logger.info("PagePool: recovered tab (%d total)", len(self._all_pages))
        return page

    @asynccontextmanager
    async def acquire(self):
        """Check out a page from the pool. Auto-returns on exit.

        Usage:
            async with page_pool.acquire() as page:
                await page.goto(url)
                result = await page.evaluate(...)
        """
        # Circuit breaker check — fast-fail when browser is known-dead
        if self._circuit_state == "open":
            elapsed = time.monotonic() - self._circuit_open_time
            if elapsed < self._circuit_cooldown:
                raise BrowserUnavailableError(
                    f"Browser unavailable (circuit open, {self._circuit_cooldown - elapsed:.0f}s until retry)"
                )
            self._circuit_state = "half_open"

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=POOL_CHECKOUT_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError("Page pool exhausted — all tabs busy for >%ds" % POOL_CHECKOUT_TIMEOUT)
        wait_ms = int((time.monotonic() - t0) * 1000)
        if wait_ms > self._max_wait_ms:
            self._max_wait_ms = wait_ms
        self._checkouts += 1
        page = None
        try:
            # Try to get an available page
            if not self._available.empty():
                page = self._available.get_nowait()
            elif len(self._all_pages) < self._max_pages:
                page = await self._create_page()
            else:
                # All pages in use, wait for one to return
                page = await self._available.get()

            # Verify page is alive
            try:
                _ = page.url  # Quick liveness check (property access, not network)
            except (TargetClosedError, Exception):
                self._crashes += 1
                if self._crashes > self._MAX_CRASHES:
                    logger.error("Page pool unstable (>%d crashes). Consider restarting.", self._MAX_CRASHES)
                page = await self._recover_page(page)

            yield page
            # Success — reset circuit breaker
            self._circuit_failure_count = 0
            if self._circuit_state == "half_open":
                self._circuit_state = "closed"
        except BrowserUnavailableError:
            raise
        except Exception:
            self._circuit_failure_count += 1
            if self._circuit_failure_count >= self._circuit_failure_threshold:
                self._circuit_state = "open"
                self._circuit_open_time = time.monotonic()
                logger.warning("Circuit breaker OPEN after %d failures", self._circuit_failure_count)
            raise
        finally:
            # Return page to pool
            if page is not None:
                self._returns += 1
                try:
                    await self._available.put(page)
                except Exception:
                    pass
            self._semaphore.release()

    async def close_all(self):
        """Close all pages in the pool."""
        for page in self._all_pages:
            try:
                await page.close()
            except Exception:
                pass
        self._all_pages.clear()
        while not self._available.empty():
            try:
                self._available.get_nowait()
            except asyncio.QueueEmpty:
                break

    def reset_circuit(self):
        """Called by watchdog after successful restart."""
        self._circuit_state = "closed"
        self._circuit_failure_count = 0

    @property
    def circuit_state(self) -> str:
        return self._circuit_state

    def get_stats(self) -> dict:
        """Return observability counters for the page pool."""
        return {
            "max_tabs": self._max_pages,
            "checkouts": self._checkouts,
            "returns": self._returns,
            "crashes": self._crashes,
            "max_wait_ms": self._max_wait_ms,
        }


# ---------------------------------------------------------------------------
# Page-level helper functions (replace old browser.goto, browser.text, etc.)
# ---------------------------------------------------------------------------

async def page_goto(page: Page, url: str, wait: str = "domcontentloaded") -> None:
    """Navigate a page with fallback to 'commit' wait strategy."""
    try:
        await page.goto(url, wait_until=wait, timeout=NAV_TIMEOUT)
    except TargetClosedError:
        raise  # Let PagePool handle recovery on next acquire()
    except Exception:
        try:
            await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT)
        except TargetClosedError:
            raise


async def page_evaluate_safe(page: Page, expression, arg=None, timeout=15):
    """Evaluate JS on page with timeout protection.

    Prevents JS deadlocks from freezing the entire page pool.
    """
    try:
        if arg is not None:
            return await asyncio.wait_for(page.evaluate(expression, arg), timeout=timeout)
        return await asyncio.wait_for(page.evaluate(expression), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("page.evaluate timed out after %ds", timeout)
        return None


async def browser_retry(async_fn, max_retries=2, description="browser operation"):
    """Retry a browser operation with exponential backoff.

    Args:
        async_fn: Async callable that performs the browser operation
        max_retries: Maximum retry attempts (default 2, total 3 attempts)
        description: Operation name for logging

    Returns:
        Result from async_fn on success

    Raises:
        Last exception if all retries fail
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return await async_fn()
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = (2 ** attempt) * 0.5  # 0.5s, 1s
                logger.warning(
                    "%s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                    description, attempt + 1, max_retries + 1,
                    type(e).__name__, delay
                )
                await asyncio.sleep(delay)
            else:
                logger.error("%s failed after %d attempts: %s", description, max_retries + 1, e)
    raise last_error


async def page_text(page: Page, selector: str) -> Optional[str]:
    """Get text content of an element on the page."""
    el = await page.query_selector(selector)
    if not el:
        return None
    content = await el.text_content()
    return content.strip() if content else None


async def page_exists(page: Page, selector: str) -> bool:
    """Check if an element exists on the page."""
    return await page.query_selector(selector) is not None


async def page_safe_fill(page: Page, selector: str, value: str, delay: int = 30):
    """Wait for element, click, clear, and type value."""
    el = await page.wait_for_selector(selector, timeout=ELEMENT_TIMEOUT)
    await el.click()
    await el.fill("")
    await el.type(value, delay=delay)


async def page_intercept_json(
    page: Page,
    url: str,
    url_pattern: str | None = None,
    timeout: float = 10,
    wait: str = "domcontentloaded",
) -> dict | None:
    """Navigate to a URL and capture the first matching JSON API response.

    Args:
        page: Playwright page (from page_pool.acquire())
        url: Page URL to navigate to
        url_pattern: Substring to match in response URL (None = first JSON response)
        timeout: Seconds to wait for the response (default 10)
        wait: Navigation wait strategy (default "domcontentloaded")

    Returns:
        Parsed JSON dict, or None if no matching response within timeout.
    """
    captured = {}
    event = asyncio.Event()

    async def on_response(response):
        if response.status == 200 and (url_pattern is None or url_pattern in response.url):
            try:
                captured["data"] = await response.json()
            except Exception:
                logger.warning("Failed to parse JSON from %s", response.url)
            event.set()

    page.on("response", on_response)
    try:
        await page_goto(page, url, wait=wait)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Response capture timed out after %ss for pattern: %s", timeout, url_pattern)
    finally:
        page.remove_listener("response", on_response)

    return captured.get("data")


# ---------------------------------------------------------------------------
# NaukriBrowser — orchestrator
# ---------------------------------------------------------------------------

class NaukriBrowser:
    """Playwright browser with multi-tab page pool and cached token manager."""

    def __init__(self):
        self.pw = None
        self.context: Optional[BrowserContext] = None
        self.token_manager = TokenManager()
        self.page_pool: Optional[PagePool] = None
        self.available = False
        self._holds_profile_lock = False

    async def start(self):
        # Cross-process guard: refuse to launch a SECOND instance against the
        # same persistent Chrome profile. Two instances on one user-data-dir
        # corrupt the profile — the highest-cost failure, since the profile is
        # the anti-detection moat. Re-entrant for our own PID (watchdog
        # restart) and reclaims stale locks from crashed instances.
        # ProfileLockedError propagates so the caller knows a live instance
        # already owns the profile — we do NOT touch it in that case.
        profile_lock.acquire()
        self._holds_profile_lock = True
        try:
            self.pw = await async_playwright().start()
            self.context = await self.pw.chromium.launch_persistent_context(
                user_data_dir=CHROME_PROFILE,
                headless=False,
                viewport={"width": 1280, "height": 800},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    f"--remote-debugging-port={CDP_PORT}",
                ],
            )

            # Initialize token manager
            self.token_manager.bind(self.context)
            await self.token_manager.extract()

            # Initialize page pool with first page
            first_page = self.context.pages[0] if self.context.pages else await self.context.new_page()

            self.page_pool = PagePool(self.context, max_pages=MAX_TABS)
            await self.page_pool.initialize(first_page)

            logger.info("Browser started (pool: %d max tabs), token: %s",
                         MAX_TABS, "found" if self.token_manager._token else "none")

            # Validate session
            if self.token_manager._token:
                try:
                    from naukri_server.api import api_get
                    await asyncio.wait_for(
                        api_get(
                            PROFILE_API,
                            {"expand_level": "1"},
                        ),
                        timeout=SESSION_VALIDATE_TIMEOUT,
                    )
                    logger.info("Session validated — token is active")
                except asyncio.TimeoutError:
                    logger.warning("Session validation timed out — will validate lazily on first tool call")
                except Exception as e:
                    logger.warning("Token found but invalid: %s. Call naukri_login.", e)
                    self.token_manager.invalidate()

            self.available = True
        except Exception as e:
            logger.error("Browser startup failed: %s. REST-only mode active.", e)
            self.available = False
            # Clean up partial initialization
            try:
                if self.page_pool:
                    await self.page_pool.close_all()
                if self.context:
                    await self.context.close()
                if self.pw:
                    await self.pw.stop()
            except Exception:
                pass
            self.page_pool = None
            self.context = None
            self.pw = None
            # Startup failed — we never actually held a live persistent context,
            # so release the profile lock to avoid a self-inflicted false
            # "already in use" on the next start attempt (e.g. watchdog retry).
            if self._holds_profile_lock:
                profile_lock.release()
                self._holds_profile_lock = False

    async def stop(self):
        from naukri_server.api import close_api_session
        await close_api_session()
        if self.page_pool:
            await self.page_pool.close_all()
        if self.context:
            await self.context.close()
        if self.pw:
            await self.pw.stop()
        # Release the cross-process profile lock so another instance (or a
        # watchdog restart in this process) can re-acquire it cleanly.
        if self._holds_profile_lock:
            profile_lock.release()
            self._holds_profile_lock = False
        logger.info("Browser stopped")

    async def get_profile_name(self) -> str:
        """Get profile name via profile API."""
        from naukri_server.api import api_get
        try:
            data = await api_get(
                PROFILE_API,
                {"expand_level": "4"},
            )
            profiles = data.get("profile") or []
            name = profiles[0].get("name") if profiles and isinstance(profiles[0], dict) else None
            if name:
                logger.info("Profile name from API: %s", name)
                return name
        except Exception as e:
            logger.warning("Profile name API failed: %s: %s", type(e).__name__, e)
        return "unknown"


browser = NaukriBrowser()
