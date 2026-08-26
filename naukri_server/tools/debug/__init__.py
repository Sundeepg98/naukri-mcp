"""Debug tool subpackage - 1 merged tool with prefixed actions for browser, API, and discovery debugging.

DISPATCH HAS THREE SHAPES, not two (changed 2026-08-26):

* ``_BROWSER_ACTIONS``  -- DOM inspection. Navigates first, then
  ``handler(page, current_url, title)``.
* ``_REST_ACTIONS``     -- the aiohttp/bearer transport. ``handler(url)``, and
  crucially NO PAGE IS CHECKED OUT AT ALL. These calls do not need a browser
  tab and must not fail merely because the browser is down or suspended;
  checking one out was pure cost and a false dependency.
* everything else       -- needs a live page: ``handler(page, url)``.

``_BROWSER_ACTIONS`` is now an EXPLICIT set. It used to be derived by scanning
for the ``browser_`` prefix, which silently coupled the dispatch signature to a
naming convention: adding any new action whose name began with ``browser_``
would have handed it the wrong signature and navigated to its argument first.
"""

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
    # REST transport (aiohttp + bearer) - what the server's own tools use
    do_fetch_api,
    do_post_api,
    do_put_api,
    do_delete_api,
    do_fetch_widget,
    do_settings_api,
    # Browser transport (page fetch + cookies) - a different question
    do_fetch_api_via_browser,
    do_post_api_via_browser,
    do_put_api_via_browser,
    do_delete_api_via_browser,
    do_fetch_widget_via_browser,
)
from naukri_server.tools.debug.discovery_actions import (
    do_discover,
    do_fetch_all_statuses,
    do_intercept_requests,
    do_click_discover,
)


# -- Merged handler dict --------------------------------------------

_HANDLERS = {
    # Browser DOM actions (Signature A: page, current_url, title)
    "browser_snapshot":      do_snapshot,
    "browser_screenshot":    do_screenshot,
    "browser_scan":          do_scan,
    "browser_deepscan":      do_deepscan,
    "browser_explore":       do_explore,
    "browser_notif_explore": do_notif_explore,
    # API actions, REST transport (Signature C: url)
    "api_fetch":             do_fetch_api,
    "api_post":              do_post_api,
    "api_put":               do_put_api,
    "api_delete":            do_delete_api,
    "api_fetch_widget":      do_fetch_widget,
    "api_settings":          do_settings_api,
    # API actions, BROWSER transport (Signature B: page, url)
    "api_fetch_via_browser":        do_fetch_api_via_browser,
    "api_post_via_browser":         do_post_api_via_browser,
    "api_put_via_browser":          do_put_api_via_browser,
    "api_delete_via_browser":       do_delete_api_via_browser,
    "api_fetch_widget_via_browser": do_fetch_widget_via_browser,
    # Discovery actions (Signature B: page, url)
    "discover_pages":        do_discover,
    "discover_statuses":     do_fetch_all_statuses,
    "discover_intercept":    do_intercept_requests,
    "discover_click":        do_click_discover,
}

#: DOM-inspection actions: navigate first, then (page, current_url, title).
_BROWSER_ACTIONS = {
    "browser_snapshot",
    "browser_screenshot",
    "browser_scan",
    "browser_deepscan",
    "browser_explore",
    "browser_notif_explore",
}

#: Actions that go out over aiohttp with the bearer token. They take (url) and
#: are dispatched WITHOUT acquiring a page from the pool.
_REST_ACTIONS = {
    "api_fetch",
    "api_post",
    "api_put",
    "api_delete",
    "api_fetch_widget",
    "api_settings",
}


@mcp.tool()
async def naukri_debug(action: str = "browser_snapshot", url: str = "") -> dict:
    """Debug toolkit - inspect browser state, test APIs, discover endpoints.

    TWO TRANSPORTS, and which one an action uses decides what its answer means.
    REST is aiohttp + API_HEADERS + Authorization bearer: the exact path every
    real tool takes, so "REST reached it" means a real tool can reach it.
    BROWSER is a fetch() inside the logged-in page: cookies, no bearer, subject
    to CORS and Akamai edge rules, but able to read response headers and reach
    the cookie-authenticated surfaces the bearer cannot.

    Every api_* action reports `transport`, `outcome`, `reached_origin` and
    `http_status`, so a CORS/network failure can never be read as "the endpoint
    returned nothing". `outcome` is one of: ok, http_error, bot_check,
    auth_required, transport_failure, client_refused, bad_request - and only
    `ok` / `http_error` are evidence about the ENDPOINT (`endpoint_evidence`
    is the boolean form). REST successes carry no exact 2xx code; every
    non-success does carry its status.

    BROWSER actions - browser transport (inspect page DOM, screenshots):
        - browser_snapshot: Get page title, URL, and visible text content
        - browser_screenshot: Take a screenshot (saves to file, returns path)
        - browser_scan: Scan for interactive elements (buttons, inputs, links)
        - browser_deepscan: Deep scan including hidden elements and iframes
        - browser_explore: Explore page structure and JavaScript data
        - browser_notif_explore: Explore notification center elements

    API actions - REST transport (aiohttp + bearer; needs no browser tab):
        - api_fetch: GET a Naukri API path. Query strings ARE supported and are
          sent byte-for-byte, e.g. "/path?jobId=123&applyType=normal"
        - api_post: POST to API. Format url as "PATH|JSON_BODY"
        - api_put: PUT to API. Format url as "PATH|JSON_BODY" (WRITES upstream)
        - api_delete: DELETE an API path, optional "PATH|JSON_BODY" (WRITES upstream)
        - api_fetch_widget: GET with widget headers (appid 109, systemid 109)
        - api_settings: GET + reshape all account settings (url ignored)

    API actions - BROWSER transport (page fetch, cookies, no bearer, CORS applies):
        - api_fetch_via_browser: GET as the page would issue it
        - api_post_via_browser: POST as the page. Format url as "PATH|JSON_BODY"
        - api_put_via_browser: PUT as the page (WRITES upstream)
        - api_delete_via_browser: DELETE as the page (WRITES upstream)
        - api_fetch_widget_via_browser: GET with widget headers, as the page
      Reach for these when REST answers bot_check/403/406, or for a surface
      documented as cookie-authenticated rather than JWT-authenticated. A
      "FetchRejected" outcome here is opaque BY SPECIFICATION - it cannot
      separate a CORS block from a dead network, so confirm with the REST
      sibling before drawing any conclusion about the endpoint.

    DISCOVERY actions - browser transport (intercept network traffic):
        - discover_pages: Navigate to a page and capture all JSON API responses
        - discover_statuses: Navigate + scroll, capture ALL HTTP statuses
        - discover_intercept: Capture outgoing POST request bodies
        - discover_click: Click element and capture resulting API calls

    Args:
        action: See groups above (default "browser_snapshot")
        url: URL or API path. Context-dependent:
             - Browser actions: page URL to navigate to (uses current page if omitted)
             - API actions: API path, query string allowed
               (e.g., "/jobapi/v3/search", "/x?days=30"). For post/put: "PATH|{json}"
             - discover_click: "CSS_SELECTOR|OPTIONAL_NAV_URL" -- SELECTOR FIRST.
               This read "PAGE_URL|CSS_SELECTOR" until 2026-08-23, which is
               backwards: `discovery_actions.do_click_discover` splits once on
               "|" and takes part 0 as the selector, part 1 as an optional URL
               to navigate to first. Following the old order made the tool
               try to click a URL as a selector. Omit the second half to click
               on whatever page the pool tab is already showing.

    Returns:
        - api_* actions: {status, outcome, transport, reached_origin,
          http_status, endpoint_evidence, request{...}, body|error, detail}
        - other actions: {status: "ok", ...action-specific data}
        - {status: "error", message, error_code} for an unknown action
    """
    handler = _HANDLERS.get(action)
    if handler is None:
        valid = ", ".join(sorted(_HANDLERS))
        return {"status": "error", "message": f"Unknown action: {action!r}. Valid: {valid}", "error_code": "BROWSER_ERROR"}

    # REST actions never touch the browser. Acquiring a pool tab for them was a
    # false dependency: it made a pure aiohttp call fail whenever the browser
    # was down, and burned one of only three tabs while doing so.
    if action in _REST_ACTIONS:
        return await handler(url)

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


# -- Backward-compat aliases ----------------------------------------

naukri_debug_browser = naukri_debug
naukri_debug_api = naukri_debug
naukri_debug_discovery = naukri_debug
