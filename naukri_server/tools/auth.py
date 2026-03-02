"""Auth tools — login, OTP verification, and session status for Naukri."""

import asyncio
import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.browser import browser, page_goto, page_text, page_safe_fill
from naukri_server.config import ACTIVITY_LEVEL_API, NAUKRI_BASE


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _login(page, method: str, email: Optional[str], password: Optional[str]) -> dict:
    """Handle login flow via Google SSO or email/password."""
    await page_goto(page, f"{NAUKRI_BASE}/nlogin/login")
    await asyncio.sleep(2)

    if "/nlogin" not in page.url:
        # Already logged in
        await browser.token_manager.extract()
        browser.token = browser.token_manager._token
        if not browser.token:
            return {"status": "error", "message": "Login redirect completed but auth token not found. Try again.", "error_code": "AUTH_ERROR"}
        name = await browser.get_profile_name()
        return {"status": "already_logged_in", "profile_name": name, "has_token": True}

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
            except Exception:
                continue

        if not google_clicked:
            return {"status": "error", "message": "Google login button not found. Try method='email'.", "error_code": "NOT_FOUND"}

        # Wait up to 30s for Google SSO to complete
        for _ in range(30):
            await asyncio.sleep(1)
            current_url = page.url
            if "/nlogin" not in current_url and "accounts.google" not in current_url:
                await browser.token_manager.extract()
                if not browser.token_manager._token:
                    return {"status": "error", "message": "Login redirect completed but token not found. Try again.", "error_code": "AUTH_ERROR"}
                name = await browser.get_profile_name()
                return {"status": "logged_in", "method": "google", "profile_name": name, "has_token": True}

        current_url = page.url
        if "accounts.google" in current_url:
            return {"status": "waiting_for_user", "message": "Google account picker is still open. Select your account in the browser, then call naukri_auth(action='login') again."}
        return {"status": "error", "message": "Google login did not complete within 30s. Check the browser.", "error_code": "BROWSER_ERROR"}

    else:
        if not email or not password:
            return {"status": "error", "message": "email and password required for method='email'", "error_code": "VALIDATION_ERROR"}
        try:
            await page_safe_fill(page, "input#usernameField", email)
            await page_safe_fill(page, "input#passwordField", password)
            await page.click("button[type='submit']")
        except Exception as e:
            return {"status": "error", "message": f"Login form error: {e}", "error_code": "BROWSER_ERROR"}

        await asyncio.sleep(3)

        otp_field = await page.query_selector("input#otp")
        if otp_field:
            return {"status": "otp_required", "needs_otp": True, "message": "Enter OTP sent to your phone"}

        if "/nlogin" not in page.url:
            await browser.token_manager.extract()
            browser.token = browser.token_manager._token
            if not browser.token:
                return {"status": "error", "message": "Login redirect completed but auth token not found. Try again.", "error_code": "AUTH_ERROR"}
            name = await browser.get_profile_name()
            return {"status": "logged_in", "method": "email", "profile_name": name, "has_token": True}

        error = await page_text(page, ".err-message, .error-msg, [class*='error']")
        return {"status": "error", "message": error or "Login failed — check credentials", "error_code": "AUTH_ERROR"}


async def _verify_otp(page, otp: str) -> dict:
    """Enter OTP code to complete login."""
    otp = otp.strip()
    if not re.match(r'^\d{4,6}$', otp):
        return {"status": "error", "message": f"Invalid OTP format: expected 4-6 digits, got '{otp}'", "error_code": "AUTH_ERROR"}

    try:
        await page_safe_fill(page, "input#otp", otp)
        await page.click("button[type='submit']")
        await asyncio.sleep(3)

        if "/nlogin" not in page.url:
            await browser.token_manager.extract()
            browser.token = browser.token_manager._token
            if not browser.token:
                return {"status": "error", "message": "Login redirect completed but auth token not found. Try again.", "error_code": "AUTH_ERROR"}
            name = await browser.get_profile_name()
            return {"status": "logged_in", "profile_name": name, "has_token": True}

        error = await page_text(page, ".err-message, .error-msg")
        return {"status": "error", "message": error or "OTP verification failed", "error_code": "AUTH_ERROR"}
    except Exception as e:
        return {"status": "error", "message": str(e), "error_code": "API_ERROR"}


async def _get_login_status() -> dict:
    """Check if the Naukri session is still active."""
    data = await api_get(ACTIVITY_LEVEL_API)
    return {
        "status": "success",
        "logged_in": data.get("loggedInStatus", False),
    }


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_auth(
    action: str = "status",
    method: Optional[str] = None,
    email: Optional[str] = None,
    password: Optional[str] = None,
    otp: Optional[str] = None,
) -> dict:
    """Unified authentication — login, OTP verification, and session status.

    Actions:
      - "login": Login via Google SSO or email/password (use method, email, password)
      - "verify_otp": Submit OTP after login returns needs_otp (use otp)
      - "status": Quick check if your Naukri session is still active

    Args:
        action: "login" | "verify_otp" | "status"
        method: For login — "google" (recommended) or "email"
        email: For login with method="email" — Naukri email
        password: For login with method="email" — Naukri password
        otp: For verify_otp — 4-6 digit OTP from SMS/email

    Returns:
        - login: {status: "logged_in"|"already_logged_in"|"waiting_for_user"|"otp_required"|"error", ...}
        - verify_otp: {status: "logged_in"|"error", ...}
        - status: {status: "success", logged_in: true/false}
        - {status: "error", message} on failure
    """
    # ── login ──────────────────────────────────────────────────────────
    if action == "login":
        if not method:
            return {"status": "error", "message": "login action requires 'method' param ('google' or 'email').", "error_code": "VALIDATION_ERROR"}
        try:
            async with browser.page_pool.acquire() as page:
                return await _login(page, method=method, email=email, password=password)
        except Exception as e:
            return {"status": "error", "message": f"Login failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── verify_otp ─────────────────────────────────────────────────────
    elif action == "verify_otp":
        if not otp:
            return {"status": "error", "message": "verify_otp action requires 'otp' param.", "error_code": "VALIDATION_ERROR"}
        try:
            async with browser.page_pool.acquire() as page:
                return await _verify_otp(page, otp=otp)
        except Exception as e:
            return {"status": "error", "message": f"OTP verification failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── status ─────────────────────────────────────────────────────────
    elif action == "status":
        try:
            return await _get_login_status()
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Failed to check login status: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── unknown action ─────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: login, verify_otp, status", "error_code": "VALIDATION_ERROR"}
