"""
Browser state — Playwright browser with multi-tab page pool and cached token manager.
"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page
from playwright._impl._errors import TargetClosedError

from naukri_server.config import CHROME_PROFILE, NAUKRI_BASE, NAV_TIMEOUT, ELEMENT_TIMEOUT, MAX_TABS, logger


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
                    return
        except Exception as e:
            logger.debug("Token extraction failed: %s", e)
        self._token = None

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

    async def refresh(self, page: Page) -> Optional[str]:
        """Re-extract token by reloading a page. Protected by lock to prevent parallel refreshes."""
        async with self._refresh_lock:
            # Double-check after acquiring lock — another refresh may have succeeded
            if self._token:
                return self._token
            try:
                await page.reload(timeout=NAV_TIMEOUT)
            except Exception:
                pass
            await self.extract()
            return self._token

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
                                    wait_until="domcontentloaded", timeout=15000)
                    await self.extract()
                except Exception as e:
                    logger.debug("Token renewal navigation failed: %s", e)
        if not self._token:
            raise ValueError("Not logged in — call naukri_login first")
        return self._token


class PagePool:
    """Manages a pool of browser pages (tabs) with semaphore-based checkout."""

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
        t0 = time.monotonic()
        await self._semaphore.acquire()
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
                page = await self._recover_page(page)

            yield page
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


async def page_text(page: Page, selector: str) -> Optional[str]:
    """Get text content of an element on the page."""
    el = await page.query_selector(selector)
    return (await el.text_content()).strip() if el else None


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

    async def start(self):
        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
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
                from naukri_server.config import PROFILE_API as _PROFILE_API
                await asyncio.wait_for(
                    api_get(
                        _PROFILE_API,
                        {"expand_level": "1"},
                    ),
                    timeout=5,
                )
                logger.info("Session validated — token is active")
            except asyncio.TimeoutError:
                logger.warning("Session validation timed out — will validate lazily on first tool call")
            except Exception as e:
                logger.warning("Token found but invalid: %s. Call naukri_login.", e)
                self.token_manager.invalidate()

    async def stop(self):
        from naukri_server.api import close_session
        await close_session()
        if self.page_pool:
            await self.page_pool.close_all()
        if self.context:
            await self.context.close()
        if self.pw:
            await self.pw.stop()
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
