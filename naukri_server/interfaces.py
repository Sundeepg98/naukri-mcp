"""Abstraction interfaces for infrastructure — decouples tools from concrete implementations.

Tools that import api_get/api_post directly are tightly coupled to the aiohttp-based
implementation in api.py. These interfaces let new tools depend on an abstract contract
instead, making them testable with simple fakes (no mock.patch gymnastics).

Usage for new tools:
    from naukri_server.interfaces import api_client

    async def _my_helper():
        data = await api_client.get("/some/path", params={"key": "val"})
        await api_client.post("/some/path", body={"field": "value"})

Existing tools still use direct imports (migration is optional and incremental).
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Abstract interfaces
# ---------------------------------------------------------------------------

class ApiClient(ABC):
    """Interface for REST API calls. Tools depend on this, not api.py directly."""

    @abstractmethod
    async def get(self, path: str, params: dict = None, extra_headers: dict = None) -> dict:
        """GET request to Naukri API."""

    @abstractmethod
    async def post(self, path: str, body: dict) -> dict:
        """POST request to Naukri API."""

    @abstractmethod
    async def put(self, path: str, body: dict) -> dict:
        """PUT request to Naukri API."""

    @abstractmethod
    async def delete(self, path: str, body: dict = None) -> dict:
        """DELETE request to Naukri API."""


class BrowserProvider(ABC):
    """Interface for browser automation. Tools depend on this, not browser.py directly."""

    @abstractmethod
    async def navigate(self, url: str) -> Any:
        """Navigate to a URL and return the page object."""

    @abstractmethod
    async def evaluate_js(self, page: Any, js: str, arg: Any = None, timeout: int = 15) -> Any:
        """Execute JavaScript on a page with timeout."""


# ---------------------------------------------------------------------------
# Concrete implementations that wrap existing infrastructure
# ---------------------------------------------------------------------------

class NaukriApiClient(ApiClient):
    """Concrete API client wrapping api.py functions.

    Uses lazy imports to avoid circular dependency issues at module load time.
    """

    async def get(self, path: str, params: dict = None, extra_headers: dict = None) -> dict:
        from naukri_server.api import api_get
        return await api_get(path, params=params, extra_headers=extra_headers)

    async def post(self, path: str, body: dict) -> dict:
        from naukri_server.api import api_post
        return await api_post(path, body=body)

    async def put(self, path: str, body: dict) -> dict:
        from naukri_server.api import api_put
        return await api_put(path, body=body)

    async def delete(self, path: str, body: dict = None) -> dict:
        from naukri_server.api import api_delete
        return await api_delete(path, body=body)


class PlaywrightBrowserProvider(BrowserProvider):
    """Concrete browser provider wrapping browser.py.

    Acquires a tab from the PagePool, navigates, and returns the page.
    Caller is responsible for releasing the tab (use page_pool.acquire() context
    manager for automatic release in production code).
    """

    async def navigate(self, url: str) -> Any:
        from naukri_server.browser import browser, page_goto
        page_ctx = browser.page_pool.acquire()
        page = await page_ctx.__aenter__()
        await page_goto(page, url)
        return page

    async def evaluate_js(self, page: Any, js: str, arg: Any = None, timeout: int = 15) -> Any:
        from naukri_server.browser import page_evaluate_safe
        return await page_evaluate_safe(page, js, arg=arg, timeout=timeout)


# ---------------------------------------------------------------------------
# Default instances — singleton pattern, tools import these
# ---------------------------------------------------------------------------

api_client: ApiClient = NaukriApiClient()
browser_provider: BrowserProvider = PlaywrightBrowserProvider()
