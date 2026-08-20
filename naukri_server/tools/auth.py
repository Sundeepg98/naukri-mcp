"""Auth tools — login, OTP verification, and session status for Naukri."""

import asyncio
import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import NaukriAPIError
from naukri_server.error_handler import handle_tool_action
from naukri_server.interfaces import api_client, browser_provider
from naukri_server.config import (
    logger, ACTIVITY_LEVEL_API, NAUKRI_BASE,
    BROWSER_MODAL_APPEAR, BROWSER_FORM_SAVE, BROWSER_PAGE_LOAD, BROWSER_PAGE_SETTLE,
)
from naukri_server.services.auth_service import (
    get_login_status as _service_get_login_status,
    harvest_post_login_state,
    build_login_success_result,
)


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _harvest() -> tuple[Optional[str], Optional[str]]:
    """Adapter — calls the existing ``harvest_post_login_state`` service helper
    while keeping the ``browser`` singleton out of this module's import surface.

    The lazy import inside the function body is intentional: the tool layer
    must not import ``browser`` at module level (Wave 3B hexagonal
    completion), but the auth_service helper still needs the concrete
    browser singleton to access TokenManager + get_profile_name. A future
    pass could replace this with ``browser_provider.extract_token()`` plus
    a separate profile-name fetch via ``api_client``.
    """
    from naukri_server import browser as _browser_mod
    return await harvest_post_login_state(_browser_mod.browser)


async def _login(page, method: str, email: Optional[str], password: Optional[str]) -> dict:
    """Handle login flow via Google SSO or email/password.

    Browser state machine stays here (Playwright-tied); token harvesting and
    success-result construction delegate to ``services.auth_service``.
    """
    await browser_provider.safe_goto(page, f"{NAUKRI_BASE}/nlogin/login")
    await asyncio.sleep(BROWSER_PAGE_LOAD)

    if "/nlogin" not in page.url:
        # Already logged in — short-circuit via service helpers
        token, name = await _harvest()
        return build_login_success_result(token=token, profile_name=name, already_logged_in=True)

    if method == "google":
        google_clicked = False
        for selector in [
            "button:has-text('Google')", "a:has-text('Google')",
            "[class*='google']", "button[data-ga-track*='google']",
            "[class*='social'] button:first-child", "img[alt*='Google']",
        ]:
            try:
                el = await page.query_selector(selector)
                if el:
                    await el.click()
                    google_clicked = True
                    break
            except Exception as e:
                logger.debug("Google login button selector '%s' failed: %s", selector, e)
                continue

        if not google_clicked:
            return {"status": "error", "message": "Google login button not found. Try method='email'.", "error_code": "NOT_FOUND"}

        # Wait up to 30s for Google SSO to complete
        for _ in range(30):
            await asyncio.sleep(BROWSER_PAGE_SETTLE)
            current_url = page.url
            if "/nlogin" not in current_url and "accounts.google" not in current_url:
                token, name = await _harvest()
                if not token:
                    return {"status": "error", "message": "Login redirect completed but token not found. Try again.", "error_code": "AUTH_ERROR"}
                return build_login_success_result(token=token, profile_name=name, method="google")

        current_url = page.url
        if "accounts.google" in current_url:
            return {"status": "waiting_for_user", "message": "Google account picker is still open. Select your account in the browser, then call naukri_auth(action='login') again."}
        return {"status": "error", "message": "Google login did not complete within 30s. Check the browser.", "error_code": "BROWSER_ERROR"}

    else:
        if not email or not password:
            return {"status": "error", "message": "email and password required for method='email'", "error_code": "VALIDATION_ERROR"}
        try:
            await browser_provider.safe_fill(page, "input#usernameField", email)
            await browser_provider.safe_fill(page, "input#passwordField", password)
            await page.click("button[type='submit']")
        except Exception as e:
            return {"status": "error", "message": f"Login form error: {e}", "error_code": "BROWSER_ERROR"}

        await asyncio.sleep(BROWSER_FORM_SAVE)

        otp_field = await page.query_selector("input#otp")
        if otp_field:
            return {"status": "otp_required", "needs_otp": True, "message": "Enter OTP sent to your phone"}

        if "/nlogin" not in page.url:
            token, name = await _harvest()
            return build_login_success_result(token=token, profile_name=name, method="email")

        error = await browser_provider.safe_text(page, ".err-message, .error-msg, [class*='error']")
        return {"status": "error", "message": error or "Login failed — check credentials", "error_code": "AUTH_ERROR"}


async def _verify_otp(page, otp: str) -> dict:
    """Enter OTP code to complete login."""
    otp = otp.strip()
    if not re.match(r'^\d{4,6}$', otp):
        return {"status": "error", "message": f"Invalid OTP format: expected 4-6 digits, got '{otp}'", "error_code": "AUTH_ERROR"}

    try:
        await browser_provider.safe_fill(page, "input#otp", otp)
        await page.click("button[type='submit']")
        await asyncio.sleep(BROWSER_FORM_SAVE)

        if "/nlogin" not in page.url:
            token, name = await _harvest()
            return build_login_success_result(token=token, profile_name=name)

        error = await browser_provider.safe_text(page, ".err-message, .error-msg")
        return {"status": "error", "message": error or "OTP verification failed", "error_code": "AUTH_ERROR"}
    except Exception as e:
        return {"status": "error", "message": str(e), "error_code": "API_ERROR"}


async def _get_login_status() -> dict:
    """Check if the Naukri session is still active.

    Thin wrapper around ``services.auth_service.get_login_status`` — kept at
    this path because tests patch ``naukri_server.tools.auth._get_login_status``.
    """
    return await _service_get_login_status()


# ---------------------------------------------------------------------------
# Single-purpose MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_login(
    method: str = "token",
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> dict:
    """Login to Naukri via Google SSO or email/password.

    Args:
        method: "google" (SSO, requires manual browser interaction) or "email"
        email: Naukri email — required when method="email"
        password: Naukri password — required when method="email"

    Returns:
        {status: "logged_in"|"already_logged_in"|"waiting_for_user"|"otp_required"|"error", ...}
    """
    if not method:
        return {"status": "error", "message": "login requires 'method' param ('google' or 'email').", "error_code": "VALIDATION_ERROR"}

    async def _impl():
        async with browser_provider.acquire_page() as page:
            return await _login(page, method=method, email=email, password=password)

    return await handle_tool_action(_impl, "auth.login")


@mcp.tool()
async def naukri_verify_otp(otp: str) -> dict:
    """Submit OTP to complete Naukri login after naukri_login returns otp_required.

    Args:
        otp: 4-6 digit OTP from SMS/email

    Returns:
        {status: "logged_in"|"error", ...}
    """
    if not otp:
        return {"status": "error", "message": "otp is required.", "error_code": "VALIDATION_ERROR"}

    async def _impl():
        async with browser_provider.acquire_page() as page:
            return await _verify_otp(page, otp=otp)

    return await handle_tool_action(_impl, "auth.verify_otp")


@mcp.tool()
async def naukri_auth_status() -> dict:
    """Check if the Naukri session is still active.

    This is the preflight gate for agent loops, so it ALWAYS answers rather
    than erroring: ``logged_in`` is always present and always a bool.

    Returns:
        {status: "success", logged_in: bool, verified: bool, reason: str}

        - logged_in: is the session usable right now?
        - verified: did we actually DETERMINE that? ``False`` means the check
          could not be completed (network / 5xx / unexpected error), which is
          NOT the same claim as a proven logout -- do not treat it as one.
        - reason: one of "no_token", "api_confirmed", "api_denied",
          "session_expired[: http NNN]", or "check_failed: <detail>".

        A ``logged_in: false`` with reason "no_token" or "session_expired"
        means: call naukri_login.
    """
    return await handle_tool_action(_get_login_status, "auth.status")
