"""
Auth bridge — cross-process access to Naukri browser and REST APIs.

Provides two modes for team agents running in separate processes:

1. CDP Browser Mode: Connect to the shared Chrome instance via CDP,
   get a real browser tab with full cookies and Playwright capabilities.

2. REST-Only Mode: Read exported auth state from file, make API calls
   with aiohttp using the same headers as the MCP server.

Usage (CDP — full browser access):
    from naukri_server.auth_bridge import cdp_connect, cdp_page

    async with cdp_connect() as (browser, context):
        async with cdp_page(context) as page:
            await page.goto("https://www.naukri.com/mnjuser/homepage")
            title = await page.evaluate("document.title")

Usage (REST — no Playwright needed):
    from naukri_server.auth_bridge import get_auth_headers, rest_request

    async with aiohttp.ClientSession() as session:
        headers = get_auth_headers()
        data = await rest_request(session, "GET", "/jobapi/v3/job/123")
"""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from naukri_server.config import (
    API_HEADERS, CDP_PORT, CHROME_PROFILE, NAUKRI_BASE, logger,
)

_AUTH_STATE_FILE = Path(CHROME_PROFILE) / "auth_state.json"
# Fallback only -- used when the token's own `exp` will not decode. See
# get_auth_state: the file is rewritten only on a token refresh, so its age
# says almost nothing about whether the credential still works.
_STALE_THRESHOLD = 300  # 5 minutes
# Refuse a token this close to expiry, so a caller does not start a sweep on a
# credential that dies partway through it.
_EXPIRY_MARGIN = 120  # 2 minutes


# ---------------------------------------------------------------------------
# REST-Only Mode (no Playwright dependency at call site)
# ---------------------------------------------------------------------------

def get_auth_state() -> dict:
    """Read exported auth state from file. Raises if missing, tokenless or expired.

    THE GATE IS THE TOKEN'S OWN `exp`, NOT THE FILE'S AGE.

    This file is written only by ``TokenManager._export_auth_state``, whose
    only caller is ``extract()``, which runs only on a token REFRESH -- and
    the token lives an hour. ``get_token()`` short-circuits on the cached
    token, so nothing rewrites the file in between. Gating on file age
    therefore rejected a perfectly good credential for most of its life:
    measured 2026-08-23 at ``exported_at`` 2127s old holding a token with
    1469s left, against a 300s threshold. The bridge exists so a second
    process can reach Naukri without fighting for the Chrome profile lock,
    and it was shut 91.7% of the time, saying "stale" about a working login.

    Age was always a PROXY for "is the token still good". The token answers
    that itself, so the proxy survives only for a token whose `exp` will not
    decode -- the one case where there is nothing better to ask.

    NOTHING SENSITIVE REACHES THE EXCEPTIONS. The file holds the live
    ``nauk_at`` and the whole cookie header; every raise below is built from
    a path, a float, or a fixed string.
    """
    from naukri_server.services.session_service import jwt_exp

    if not _AUTH_STATE_FILE.exists():
        raise FileNotFoundError(
            f"No auth state file at {_AUTH_STATE_FILE}. Is the MCP server running?"
        )
    state = json.loads(_AUTH_STATE_FILE.read_text())
    token = state.get("token")
    if not token:
        raise ValueError(
            "Auth state holds no exported token -- the MCP server has not "
            "logged in yet, or logged out. Call naukri_login."
        )

    exp = jwt_exp(token)
    if exp is not None:
        remaining = exp - time.time()
        if remaining <= _EXPIRY_MARGIN:
            raise ValueError(
                f"Exported token is expired or too close to it "
                f"({remaining:.0f}s left, margin {_EXPIRY_MARGIN}s). The MCP "
                f"server re-exports on its next refresh."
            )
        return state

    # No readable `exp`. Fall back to the old, weaker proxy -- file age is the
    # only signal left, and a token we cannot read is one we cannot vouch for.
    age = time.time() - state.get("exported_at", 0)
    if age > _STALE_THRESHOLD:
        raise ValueError(
            f"Auth state is {age:.0f}s old (threshold: {_STALE_THRESHOLD}s) and "
            f"its token carries no readable exp claim to check instead."
        )
    return state


def get_auth_headers() -> dict:
    """Get ready-to-use headers for Naukri REST API calls."""
    state = get_auth_state()
    return {
        **API_HEADERS,
        "Authorization": f"Bearer {state['token']}",
        "cookie": state["cookies"],
    }


async def rest_request(
    session,
    method: str,
    path: str,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
) -> dict:
    """Make an authenticated request to Naukri API using exported auth state.

    Args:
        session: aiohttp.ClientSession instance
        method: HTTP method (GET, POST, PUT, DELETE)
        path: API path (e.g., "/jobapi/v3/job/123")
        params: Optional query parameters
        body: Optional JSON body for POST/PUT

    Returns:
        Parsed JSON response dict

    Raises:
        Exception on HTTP errors
    """
    headers = get_auth_headers()
    url = f"{NAUKRI_BASE}{path}"
    kwargs = {"headers": headers}
    if params:
        kwargs["params"] = params
    if body is not None:
        kwargs["json"] = body

    async with session.request(method, url, **kwargs) as resp:
        if resp.status == 200:
            return await resp.json()
        text = await resp.text()
        raise Exception(f"HTTP {resp.status} {method} {path}: {text[:500]}")


# ---------------------------------------------------------------------------
# CDP Browser Mode (full Playwright access via shared Chrome)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def cdp_connect(port: Optional[int] = None):
    """Connect to the MCP server's Chrome instance via CDP.

    Yields (browser, context) where context is the persistent context
    with all Naukri cookies. The browser.close() on exit only disconnects
    the CDP client — it does NOT kill Chrome.

    Usage:
        async with cdp_connect() as (browser, context):
            page = await context.new_page()
            ...
    """
    from playwright.async_api import async_playwright

    cdp_port = port or CDP_PORT
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.connect_over_cdp(
            f"http://localhost:{cdp_port}",
            timeout=15000,
        )
        context = browser.contexts[0]
        yield browser, context
    finally:
        if browser:
            await browser.close()  # Disconnects only, Chrome stays alive
        await pw.stop()


@asynccontextmanager
async def cdp_page(context, close_on_exit: bool = True):
    """Create a new page (tab) in the shared CDP context.

    The page inherits all cookies from the persistent context.
    Closed automatically on exit unless close_on_exit=False.

    Usage:
        async with cdp_connect() as (browser, context):
            async with cdp_page(context) as page:
                await page.goto(url)
                data = await page.evaluate("document.title")
    """
    page = await context.new_page()
    try:
        yield page
    finally:
        if close_on_exit:
            try:
                await page.close()
            except Exception:
                pass


def get_cdp_endpoint() -> str:
    """Get the CDP endpoint URL. Reads port from auth state file or config."""
    try:
        state = get_auth_state()
        port = state.get("cdp_port", CDP_PORT)
    except (FileNotFoundError, ValueError):
        port = CDP_PORT
    return f"http://localhost:{port}"


async def extract_token_from_cdp(context) -> tuple[str, str]:
    """Extract JWT token and cookie string from a CDP-connected context.

    Returns (token, cookies) tuple. Useful when agents need to make REST
    calls without reading the auth state file.
    """
    cookies = await context.cookies(NAUKRI_BASE)
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    token = None
    for c in cookies:
        if c["name"] == "nauk_at":
            token = c["value"]
            break
    if not token:
        raise ValueError("No nauk_at token found in CDP context cookies")
    return token, cookie_str
