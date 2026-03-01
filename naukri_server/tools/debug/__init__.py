"""Debug tool subpackage — 3 tools: browser, API, and discovery debugging."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser, page_goto

from naukri_server.tools.debug.browser_actions import (
    do_snapshot,
    do_screenshot,
    do_scan,
    do_deepscan,
    do_explore,
    do_notif_explore,
)
from naukri_server.tools.debug.api_actions import (
    do_fetch_api,
    do_post_api,
    do_put_api,
    do_delete_api,
    do_fetch_widget,
    do_settings_api,
)
from naukri_server.tools.debug.discovery_actions import (
    do_discover,
    do_fetch_all_statuses,
    do_intercept_requests,
    do_click_discover,
)


# ── Browser debugging ────────────────────────────────────────────────

_BROWSER_HANDLERS = {
    "snapshot": do_snapshot,
    "screenshot": do_screenshot,
    "scan": do_scan,
    "deepscan": do_deepscan,
    "explore": do_explore,
    "notif_explore": do_notif_explore,
}


@mcp.tool()
async def naukri_debug_browser(action: str = "snapshot", url: Optional[str] = None) -> dict:
    """Browser-based debugging — inspect page state, take screenshots, scan for elements.

    Actions:
        - snapshot: Get page title, URL, and visible text content
        - screenshot: Take a screenshot (saves to file, returns path)
        - scan: Scan for interactive elements (buttons, inputs, links)
        - deepscan: Deep scan including hidden elements and iframes
        - explore: Explore page structure and JavaScript data
        - notif_explore: Explore notification center elements

    Args:
        action: One of "snapshot", "screenshot", "scan", "deepscan", "explore", "notif_explore"
        url: Optional URL to navigate to before action (uses current page if omitted)

    Returns:
        - {status: "ok", ...action-specific data}
        - {status: "error", message}
    """
    handler = _BROWSER_HANDLERS.get(action)
    if handler is None:
        valid = ", ".join(sorted(_BROWSER_HANDLERS))
        return {"status": "error", "message": f"Unknown browser action: {action!r}. Valid: {valid}", "error_code": "BROWSER_ERROR"}

    async with browser.page_pool.acquire() as page:
        if url:
            await page_goto(page, url)
            await asyncio.sleep(3)
        current_url = page.url
        title = await page.title()
        return await handler(page, current_url, title)


# ── API debugging ────────────────────────────────────────────────────

_API_HANDLERS = {
    "fetch_api": do_fetch_api,
    "post_api": do_post_api,
    "put_api": do_put_api,
    "delete_api": do_delete_api,
    "fetch_widget": do_fetch_widget,
    "settings_api": do_settings_api,
}


@mcp.tool()
async def naukri_debug_api(action: str = "fetch_api", url: str = "") -> dict:
    """API debugging — test Naukri REST endpoints directly.

    Actions:
        - fetch_api: GET request to a Naukri API path (e.g., "/jobapi/v3/search")
        - post_api: POST to API. Format url as "PATH|JSON_BODY"
        - put_api: PUT to API. Format url as "PATH|JSON_BODY"
        - delete_api: DELETE request to API path
        - fetch_widget: Fetch a Naukri widget/component endpoint
        - settings_api: Fetch all account settings from Naukri

    Args:
        action: One of "fetch_api", "post_api", "put_api", "delete_api", "fetch_widget", "settings_api"
        url: API path for fetch/post/put/delete (e.g., "/jobapi/v3/job/123"). For post/put, use "PATH|{json_body}"

    Returns:
        - {status: "ok", ...response data}
        - {status: "error", message}
    """
    handler = _API_HANDLERS.get(action)
    if handler is None:
        valid = ", ".join(sorted(_API_HANDLERS))
        return {"status": "error", "message": f"Unknown API action: {action!r}. Valid: {valid}", "error_code": "BROWSER_ERROR"}

    async with browser.page_pool.acquire() as page:
        return await handler(page, url)


# ── Discovery debugging ─────────────────────────────────────────────

_DISCOVERY_HANDLERS = {
    "discover": do_discover,
    "fetch_all_statuses": do_fetch_all_statuses,
    "intercept_requests": do_intercept_requests,
    "click_discover": do_click_discover,
}


@mcp.tool()
async def naukri_debug_discovery(action: str = "discover", url: Optional[str] = None) -> dict:
    """API discovery — find undocumented Naukri endpoints by intercepting network traffic.

    Actions:
        - discover: Navigate to a page and capture all JSON API responses
        - fetch_all_statuses: Navigate + scroll to trigger lazy-loaded API calls, capture all
        - intercept_requests: Capture outgoing POST request bodies (see what the page sends)
        - click_discover: Click an element and capture resulting API calls. url = "PAGE_URL|CSS_SELECTOR"

    Args:
        action: One of "discover", "fetch_all_statuses", "intercept_requests", "click_discover"
        url: Page URL to navigate to. For click_discover: "PAGE_URL|CSS_SELECTOR"

    Returns:
        - {status: "ok", captured_responses: [...]}
        - {status: "error", message}
    """
    handler = _DISCOVERY_HANDLERS.get(action)
    if handler is None:
        valid = ", ".join(sorted(_DISCOVERY_HANDLERS))
        return {"status": "error", "message": f"Unknown discovery action: {action!r}. Valid: {valid}", "error_code": "BROWSER_ERROR"}

    async with browser.page_pool.acquire() as page:
        return await handler(page, url)
