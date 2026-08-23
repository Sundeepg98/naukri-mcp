"""Debug tool subpackage — 1 merged tool with prefixed actions for browser, API, and discovery debugging."""

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


# ── Merged handler dict ─────────────────────────────────────────────

_HANDLERS = {
    # Browser actions (Signature A: page, current_url, title)
    "browser_snapshot":      do_snapshot,
    "browser_screenshot":    do_screenshot,
    "browser_scan":          do_scan,
    "browser_deepscan":      do_deepscan,
    "browser_explore":       do_explore,
    "browser_notif_explore": do_notif_explore,
    # API actions (Signature B: page, url)
    "api_fetch":             do_fetch_api,
    "api_post":              do_post_api,
    "api_put":               do_put_api,
    "api_delete":            do_delete_api,
    "api_fetch_widget":      do_fetch_widget,
    "api_settings":          do_settings_api,
    # Discovery actions (Signature B: page, url)
    "discover_pages":        do_discover,
    "discover_statuses":     do_fetch_all_statuses,
    "discover_intercept":    do_intercept_requests,
    "discover_click":        do_click_discover,
}

_BROWSER_ACTIONS = {k for k in _HANDLERS if k.startswith("browser_")}


@mcp.tool()
async def naukri_debug(action: str = "browser_snapshot", url: str = "") -> dict:
    """Debug toolkit — inspect browser state, test APIs, discover endpoints.

    BROWSER actions (inspect page DOM, take screenshots):
        - browser_snapshot: Get page title, URL, and visible text content
        - browser_screenshot: Take a screenshot (saves to file, returns path)
        - browser_scan: Scan for interactive elements (buttons, inputs, links)
        - browser_deepscan: Deep scan including hidden elements and iframes
        - browser_explore: Explore page structure and JavaScript data
        - browser_notif_explore: Explore notification center elements

    API actions (test Naukri REST endpoints directly):
        - api_fetch: GET request to a Naukri API path
        - api_post: POST to API. Format url as "PATH|JSON_BODY"
        - api_put: PUT to API. Format url as "PATH|JSON_BODY"
        - api_delete: DELETE request to API path
        - api_fetch_widget: Fetch using widget headers (appid:109)
        - api_settings: Fetch all account settings from Naukri

    DISCOVERY actions (intercept network traffic to find endpoints):
        - discover_pages: Navigate to a page and capture all JSON API responses
        - discover_statuses: Navigate + scroll, capture ALL HTTP statuses
        - discover_intercept: Capture outgoing POST request bodies
        - discover_click: Click element and capture resulting API calls

    Args:
        action: See groups above (default "browser_snapshot")
        url: URL or API path. Context-dependent:
             - Browser actions: page URL to navigate to (uses current page if omitted)
             - API actions: API path (e.g., "/jobapi/v3/search"). For post/put: "PATH|{json}"
             - discover_click: "CSS_SELECTOR|OPTIONAL_NAV_URL" -- SELECTOR FIRST.
               This read "PAGE_URL|CSS_SELECTOR" until 2026-08-23, which is
               backwards: `discovery_actions.do_click_discover` splits once on
               "|" and takes part 0 as the selector, part 1 as an optional URL
               to navigate to first. Following the old order made the tool
               try to click a URL as a selector. Omit the second half to click
               on whatever page the pool tab is already showing.

    Returns:
        - {status: "ok", ...action-specific data}
        - {status: "error", message, error_code}
    """
    handler = _HANDLERS.get(action)
    if handler is None:
        valid = ", ".join(sorted(_HANDLERS))
        return {"status": "error", "message": f"Unknown action: {action!r}. Valid: {valid}", "error_code": "BROWSER_ERROR"}

    async with browser.page_pool.acquire() as page:
        if action in _BROWSER_ACTIONS:
            if url:
                await page_goto(page, url)
                await asyncio.sleep(3)
            current_url = page.url
            title = await page.title()
            return await handler(page, current_url, title)
        else:
            return await handler(page, url)


# ── Backward-compat aliases ─────────────────────────────────────────

naukri_debug_browser = naukri_debug
naukri_debug_api = naukri_debug
naukri_debug_discovery = naukri_debug
