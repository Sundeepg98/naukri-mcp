import asyncio
import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.browser import browser, page_goto, page_text, page_safe_fill
from naukri_server.config import ACTIVITY_LEVEL_API, NAUKRI_BASE


# ============================================================================
# Tool 1: Login (Playwright — handles Google SSO visually)
# ============================================================================


@mcp.tool()
async def naukri_login(
    method: str = "google",
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> dict:
    """Login to Naukri.com. Supports Google SSO (recommended) or email/password.

    Google SSO uses the Chrome profile's saved Google session — no credentials
    needed. Cookies and API token persist across sessions (~30 days).

    Args:
        method: "google" (recommended) or "email"
        email: Naukri email (only for method="email")
        password: Naukri password (only for method="email")

    Returns:
        - {status: "logged_in", method, profile_name, has_token: true}
        - {status: "already_logged_in", profile_name, has_token: true}
        - {status: "waiting_for_user", message} -- Google picker still open
        - {status: "otp_required", needs_otp: true, message}
        - {status: "error", message}
    """
    async with browser.page_pool.acquire() as page:
        await page_goto(page, f"{NAUKRI_BASE}/nlogin/login")
        await asyncio.sleep(2)

        if "/nlogin" not in page.url:
            # Already logged in
            await browser.token_manager.extract()
            browser.token = browser.token_manager._token
            if not browser.token:
                return {"status": "error", "message": "Login redirect completed but auth token not found. Try again."}
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
                return {"status": "error", "message": "Google login button not found. Try method='email'."}

            # Wait up to 30s for Google SSO to complete
            for _ in range(30):
                await asyncio.sleep(1)
                current_url = page.url
                if "/nlogin" not in current_url and "accounts.google" not in current_url:
                    await browser.token_manager.extract()
                    if not browser.token_manager._token:
                        return {"status": "error", "message": "Login redirect completed but token not found. Try again."}
                    name = await browser.get_profile_name()
                    return {"status": "logged_in", "method": "google", "profile_name": name, "has_token": True}

            current_url = page.url
            if "accounts.google" in current_url:
                return {"status": "waiting_for_user", "message": "Google account picker is still open. Select your account in the browser, then call naukri_login() again."}
            return {"status": "error", "message": "Google login did not complete within 30s. Check the browser."}

        else:
            if not email or not password:
                return {"status": "error", "message": "email and password required for method='email'"}
            try:
                await page_safe_fill(page, "input#usernameField", email)
                await page_safe_fill(page, "input#passwordField", password)
                await page.click("button[type='submit']")
            except Exception as e:
                return {"status": "error", "message": f"Login form error: {e}"}

            await asyncio.sleep(3)

            otp_field = await page.query_selector("input#otp")
            if otp_field:
                return {"status": "otp_required", "needs_otp": True, "message": "Enter OTP sent to your phone"}

            if "/nlogin" not in page.url:
                await browser.token_manager.extract()
                browser.token = browser.token_manager._token
                if not browser.token:
                    return {"status": "error", "message": "Login redirect completed but auth token not found. Try again."}
                name = await browser.get_profile_name()
                return {"status": "logged_in", "method": "email", "profile_name": name, "has_token": True}

            error = await page_text(page, ".err-message, .error-msg, [class*='error']")
            return {"status": "error", "message": error or "Login failed — check credentials"}


# ============================================================================
# Tool 2: Verify OTP (Playwright)
# ============================================================================


@mcp.tool()
async def naukri_verify_otp(otp: str) -> dict:
    """Enter OTP code after login. Call after naukri_login returns needs_otp.

    Args:
        otp: 6-digit OTP from SMS/email

    Returns:
        - {status: "logged_in", profile_name, has_token: true}
        - {status: "error", message}
    """
    async with browser.page_pool.acquire() as page:
        # Validate OTP format
        otp = otp.strip()
        if not re.match(r'^\d{4,6}$', otp):
            return {"status": "error", "message": f"Invalid OTP format: expected 4-6 digits, got '{otp}'"}

        try:
            await page_safe_fill(page, "input#otp", otp)
            await page.click("button[type='submit']")
            await asyncio.sleep(3)

            if "/nlogin" not in page.url:
                await browser.token_manager.extract()
                browser.token = browser.token_manager._token
                if not browser.token:
                    return {"status": "error", "message": "Login redirect completed but auth token not found. Try again."}
                name = await browser.get_profile_name()
                return {"status": "logged_in", "profile_name": name, "has_token": True}

            error = await page_text(page, ".err-message, .error-msg")
            return {"status": "error", "message": error or "OTP verification failed"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ============================================================================
# Tool 3: Login Status Check (API — no browser needed)
# ============================================================================


@mcp.tool()
async def naukri_get_login_status() -> dict:
    """Quick check if your Naukri session is still active and authenticated.

    Uses the activity level endpoint which reliably returns login state.

    Returns:
        - {status: "success", logged_in: true/false}
        - {status: "error", message}
    """
    try:
        data = await api_get(ACTIVITY_LEVEL_API)
        return {
            "status": "success",
            "logged_in": data.get("loggedInStatus", False),
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to check login status: {type(e).__name__}: {e}"}
