"""Debug tool subpackage — dispatches to browser, API, and discovery action handlers."""

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


# Action dispatch table
_HANDLERS = {
    "snapshot": do_snapshot,
    "screenshot": do_screenshot,
    "scan": do_scan,
    "deepscan": do_deepscan,
    "explore": do_explore,
    "notif_explore": do_notif_explore,
    "fetch_api": do_fetch_api,
    "post_api": do_post_api,
    "put_api": do_put_api,
    "delete_api": do_delete_api,
    "fetch_widget": do_fetch_widget,
    "settings_api": do_settings_api,
    "discover": do_discover,
    "fetch_all_statuses": do_fetch_all_statuses,
    "intercept_requests": do_intercept_requests,
    "click_discover": do_click_discover,
}

# Actions that use url for API path or selector — skip page navigation
_SKIP_GOTO_ACTIONS = frozenset({
    "fetch_api", "fetch_widget", "post_api", "delete_api", "put_api", "click_discover",
})

# Browser actions receive (page, current_url, title)
_BROWSER_ACTIONS = frozenset({
    "snapshot", "screenshot", "scan", "deepscan", "explore", "notif_explore",
})

# API actions receive (page, url)
_API_ACTIONS = frozenset({
    "fetch_api", "post_api", "put_api", "delete_api", "fetch_widget", "settings_api",
})

# Discovery actions receive (page, url)
_DISCOVERY_ACTIONS = frozenset({
    "discover", "fetch_all_statuses", "intercept_requests", "click_discover",
})


@mcp.tool()
async def naukri_debug(action: str = "snapshot", url: Optional[str] = None) -> dict:
    """Debug tool: capture current page state for troubleshooting.

    Args:
        action: One of the actions below.

            Browser actions (navigate to url first, then act):
                "snapshot"    -- (default) DOM structure of current page (job cards, buttons, etc.)
                "screenshot"  -- save a PNG screenshot to debug.png in the project root
                "scan"        -- deep scan for chatbot/widget/iframe/neo/agent elements
                "deepscan"    -- thorough scan: high z-index, shadow DOM, web components, bottom-right floaters
                "explore"     -- comprehensive DOM exploration: forms, buttons, scripts, data attrs, embedded JSON
                "notif_explore" -- targeted notification page exploration (NEXT_DATA, scripts, DOM tree)

            API actions (url = API path or "API_PATH|JSON_BODY"):
                "fetch_api"   -- browser-context GET request; url is the API path to fetch
                "post_api"    -- browser-context POST; url format "API_PATH|JSON_BODY"
                "put_api"     -- browser-context PUT; url format "API_PATH|JSON_BODY"
                "delete_api"  -- browser-context DELETE; url is API path, optional "|JSON_BODY"
                "fetch_widget" -- like fetch_api but uses widget headers (appid:109, systemid:109)
                "settings_api" -- fetch profile/communication/visibility settings from Naukri API (url ignored)

            Discovery actions (navigate + intercept network traffic):
                "discover"          -- navigate to applied/saved jobs pages, capture all 200 JSON responses;
                                       url can be a page key ("applied_jobs"/"saved_jobs") or a custom URL
                "fetch_all_statuses" -- like discover but captures ALL HTTP statuses (not just 200)
                "intercept_requests" -- capture outgoing POST request bodies (method, url, post_data)
                "click_discover"     -- click a CSS selector then capture resulting API calls;
                                       url format "CSS_SELECTOR|OPTIONAL_NAV_URL"

        url: Meaning depends on the action (see above).
             For browser actions: optional URL to navigate to before performing the action.
             For API actions: the API path (or "path|body" for POST/PUT/DELETE).
             For discovery actions: page key, custom URL, or "selector|url".

    Returns:
        - {status: "ok", url, title, ...action-specific data...}
        - {status: "error", message}
    """
    handler = _HANDLERS.get(action)
    if handler is None:
        return {"status": "error", "message": f"Unknown action: {action}"}

    async with browser.page_pool.acquire() as page:
        # Navigate to url first, unless the action uses url differently
        if url and action not in _SKIP_GOTO_ACTIONS:
            await page_goto(page, url)
            await asyncio.sleep(3)
        current_url = page.url
        title = await page.title()

        # Dispatch based on action category
        if action in _BROWSER_ACTIONS:
            return await handler(page, current_url, title)
        else:
            # API and discovery actions receive (page, url)
            return await handler(page, url)
