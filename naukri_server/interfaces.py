"""Abstraction interfaces for infrastructure — decouples tools from concrete implementations.

Tools that import api_get/api_post directly are tightly coupled to the aiohttp-based
implementation in api.py. These interfaces let new tools depend on an abstract contract
instead, making them testable with simple fakes (no mock.patch gymnastics).

Usage for new tools:
    from naukri_server.interfaces import api_client

    async def _my_helper():
        data = await api_client.get("/some/path", params={"key": "val"})
        await api_client.post("/some/path", body={"field": "value"})

All tools use these interfaces — no tool imports api_get/api_post directly.
"""

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from naukri_server.config import DISPLAY_MIN_FIT_SCORE


# ---------------------------------------------------------------------------
# Abstract interfaces
# ---------------------------------------------------------------------------

class ApiClient(ABC):
    """Interface for REST API calls. Tools depend on this, not api.py directly."""

    @abstractmethod
    async def get(self, path: str, params: Optional[dict] = None, extra_headers: Optional[dict] = None) -> dict:
        """GET request to Naukri API."""

    @abstractmethod
    async def post(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        """POST request to Naukri API."""

    @abstractmethod
    async def put(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        """PUT request to Naukri API."""

    @abstractmethod
    async def delete(self, path: str, body: Optional[dict] = None,
                     extra_headers: Optional[dict] = None) -> dict:
        """DELETE request to Naukri API."""

    @abstractmethod
    async def get_session(self) -> Any:
        """Return the underlying HTTP session for low-level operations (e.g. binary downloads)."""


class BrowserProvider(ABC):
    """Interface for browser automation. Tools depend on this, not browser.py directly.

    Methods follow a layered design:

    * ``acquire_page()`` is the **primary** entry point — yields a Page from the
      pooled browser context. Use this in an ``async with`` block; the Page is
      automatically returned to the pool on exit. The Page object itself is
      treated as opaque (Playwright-typed) so advanced patterns like
      ``page.on("response", ...)`` event listeners remain available without
      bloating the interface.

    * ``navigate(url)`` is a **convenience** that combines acquire + goto for
      callers that only need a one-shot navigation.

    * ``safe_goto / safe_text / safe_fill / evaluate_js / intercept_json``
      are page-level helpers that wrap the corresponding ``browser.py``
      functions with timeout + retry semantics already baked in.

    * ``extract_token()`` exposes the cached JWT token from the
      ``TokenManager`` singleton — the single browser-derived piece of
      auth state most non-browser tools actually need.
    """

    @abstractmethod
    async def navigate(self, url: str) -> Any:
        """Navigate to a URL and return the page object."""

    @abstractmethod
    def acquire_page(self) -> Any:
        """Async context manager that yields a Page from the pool.

        Usage:
            async with browser_provider.acquire_page() as page:
                await browser_provider.safe_goto(page, url)
                ...
        """

    @abstractmethod
    async def evaluate_js(self, page: Any, js: str, arg: Any = None, timeout: int = 15) -> Any:
        """Execute JavaScript on a page with timeout."""

    @abstractmethod
    async def safe_goto(self, page: Any, url: str, wait_until: str = "domcontentloaded") -> bool:
        """Navigate ``page`` to ``url`` with fallback wait strategy.

        Returns ``True`` on success, ``False`` if navigation raised.
        """

    @abstractmethod
    async def safe_text(self, page: Any, selector: str) -> Optional[str]:
        """Get text content of an element on the page (None if not present)."""

    @abstractmethod
    async def safe_fill(self, page: Any, selector: str, value: str, delay: int = 30) -> bool:
        """Wait for selector, click, clear, and type ``value`` (returns success)."""

    @abstractmethod
    async def intercept_json(
        self,
        page: Any,
        url: str,
        url_pattern: Optional[str] = None,
        timeout: float = 10,
        wait: str = "domcontentloaded",
    ) -> Optional[dict]:
        """Navigate to ``url`` and capture the first matching JSON XHR response."""

    @abstractmethod
    async def extract_token(self) -> Optional[str]:
        """Return the current cached auth token (None if unavailable)."""

    async def cached_token(self) -> Optional[str]:
        """Return the cached auth token WITHOUT triggering a re-extraction.

        Deliberately CONCRETE, not abstract: adding an abstract method would
        break every existing BrowserProvider fake. The default simply delegates
        to ``extract_token()`` so behaviour is unchanged for implementations
        that do not override it.

        Providers backed by a real ``TokenManager`` MUST override this, because
        ``TokenManager.extract()`` CLEARS the cached token when cookie
        extraction fails: calling it from a read-only check (auth status,
        health probe) during a browser blip would destroy a valid session.
        """
        return await self.extract_token()


# ---------------------------------------------------------------------------
# Concrete implementations that wrap existing infrastructure
# ---------------------------------------------------------------------------

class NaukriApiClient(ApiClient):
    """Concrete API client wrapping api.py functions.

    Uses lazy imports to avoid circular dependency issues at module load time.
    """

    async def get(self, path: str, params: Optional[dict] = None, extra_headers: Optional[dict] = None) -> dict:
        from naukri_server.api import api_get
        return await api_get(path, params=params, extra_headers=extra_headers)

    async def post(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        from naukri_server.api import api_post
        return await api_post(path, body=body, extra_headers=extra_headers)

    async def put(self, path: str, body: dict, extra_headers: Optional[dict] = None) -> dict:
        from naukri_server.api import api_put
        return await api_put(path, body=body, extra_headers=extra_headers)

    async def delete(self, path: str, body: Optional[dict] = None,
                     extra_headers: Optional[dict] = None) -> dict:
        from naukri_server.api import api_delete
        return await api_delete(path, body=body, extra_headers=extra_headers)

    async def get_session(self) -> Any:
        from naukri_server.api import get_session
        return await get_session()


class PlaywrightBrowserProvider(BrowserProvider):
    """Concrete browser provider wrapping browser.py.

    All methods are thin wrappers that lazy-import from ``naukri_server.browser``
    to avoid circular import issues at module load time.
    """

    async def navigate(self, url: str) -> Any:
        from naukri_server.browser import browser, page_goto
        page_ctx = browser.page_pool.acquire()
        page = await page_ctx.__aenter__()
        await page_goto(page, url)
        return page

    def acquire_page(self):
        """Return the PagePool's async context manager directly."""
        from naukri_server.browser import browser
        return browser.page_pool.acquire()

    async def evaluate_js(self, page: Any, js: str, arg: Any = None, timeout: int = 15) -> Any:
        from naukri_server.browser import page_evaluate_safe
        return await page_evaluate_safe(page, js, arg=arg, timeout=timeout)

    async def safe_goto(self, page: Any, url: str, wait_until: str = "domcontentloaded") -> bool:
        from naukri_server.browser import page_goto
        try:
            await page_goto(page, url, wait=wait_until)
            return True
        except Exception:
            return False

    async def safe_text(self, page: Any, selector: str) -> Optional[str]:
        from naukri_server.browser import page_text
        return await page_text(page, selector)

    async def safe_fill(self, page: Any, selector: str, value: str, delay: int = 30) -> bool:
        from naukri_server.browser import page_safe_fill
        try:
            await page_safe_fill(page, selector, value, delay=delay)
            return True
        except Exception:
            return False

    async def intercept_json(
        self,
        page: Any,
        url: str,
        url_pattern: Optional[str] = None,
        timeout: float = 10,
        wait: str = "domcontentloaded",
    ) -> Optional[dict]:
        from naukri_server.browser import page_intercept_json
        return await page_intercept_json(page, url, url_pattern=url_pattern, timeout=timeout, wait=wait)

    async def extract_token(self) -> Optional[str]:
        from naukri_server.browser import browser
        token_mgr = getattr(browser, "token_manager", None)
        if token_mgr is None:
            return None
        await token_mgr.extract()
        return getattr(token_mgr, "_token", None)

    async def cached_token(self) -> Optional[str]:
        """Read the ALREADY-cached token; never re-extract (non-mutating).

        Overrides the ABC default (which delegates to ``extract_token``) on
        purpose: ``TokenManager.extract()`` sets ``_token = None`` whenever
        cookie extraction fails, so using it for a read-only status check
        during a browser blip would DESTROY a perfectly good session.

        Every failure path yields None rather than raising -- "the token could
        not be read" is an answer the caller must be able to act on, not a
        crash. A dead or unbound browser therefore reads as "no token".
        """
        try:
            from naukri_server.browser import browser
            token_mgr = getattr(browser, "token_manager", None)
            if token_mgr is None:
                return None
            return getattr(token_mgr, "_token", None)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Default instances — singleton pattern, tools import these
# ---------------------------------------------------------------------------

api_client: ApiClient = NaukriApiClient()
browser_provider: BrowserProvider = PlaywrightBrowserProvider()


# ---------------------------------------------------------------------------
# Search & Apply ports — used by autonomous agent
# ---------------------------------------------------------------------------

class SearchPort(ABC):
    """Port for job search operations."""

    @abstractmethod
    async def hunt(self, keywords: str, location: Optional[str] = None,
                   min_fit_score: int = DISPLAY_MIN_FIT_SCORE, limit: int = 20,
                   freshness: int = 7, work_mode: Optional[str] = None) -> dict:
        """Search and score jobs against profile."""
        ...


class ApplyPort(ABC):
    """Port for job application operations."""

    @abstractmethod
    async def apply_one(self, job_id: str, title: str = "",
                        company: str = "", tracking_extra: Optional[dict] = None) -> dict:
        """Apply to a single job."""
        ...


class NaukriSearchPort(SearchPort):
    """Concrete search port wrapping naukri_auto_hunt."""

    async def hunt(self, keywords: str, location: Optional[str] = None,
                   min_fit_score: int = DISPLAY_MIN_FIT_SCORE, limit: int = 20,
                   freshness: int = 7, work_mode: Optional[str] = None) -> dict:
        from naukri_server.tools.auto_hunt import naukri_auto_hunt
        return await naukri_auto_hunt(
            keywords=keywords, location=location,
            min_fit_score=min_fit_score, limit=limit,
            freshness=freshness, work_mode=work_mode,
        )


class NaukriApplyPort(ApplyPort):
    """Concrete apply port wrapping _apply_single."""

    async def apply_one(self, job_id: str, title: str = "",
                        company: str = "", tracking_extra: Optional[dict] = None) -> dict:
        from naukri_server.tools.apply import _apply_single
        return await _apply_single(job_id, title=title, company=company,
                                    tracking_extra=tracking_extra or {})


# Singletons
search_port: SearchPort = NaukriSearchPort()
apply_port: ApplyPort = NaukriApplyPort()
