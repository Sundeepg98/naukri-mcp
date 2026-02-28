import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser
from naukri_server.config import NAUKRI_BASE


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
    """
    async with browser._lock:
        await browser.goto(f"{NAUKRI_BASE}/nlogin/login")
        await asyncio.sleep(2)

        if "/nlogin" not in browser.page.url:
            # Already logged in
            token = await browser._extract_token()
            name = await browser.get_profile_name()
            return {"status": "already_logged_in", "profile_name": name, "has_token": bool(token)}

        if method == "google":
            google_clicked = False
            for selector in [
                "button:has-text('Google')", "a:has-text('Google')",
                "[class*='google']", "button[data-ga-track*='google']",
                "[class*='social'] button:first-child", "img[alt*='Google']",
            ]:
                try:
                    el = await browser.page.query_selector(selector)
                    if el:
                        await el.click()
                        google_clicked = True
                        break
                except Exception:
                    continue

            if not google_clicked:
                return {"status": "error", "message": "Google login button not found. Try method='email'."}

            await asyncio.sleep(5)

            if "/nlogin" not in browser.page.url and "accounts.google" not in browser.page.url:
                token = await browser._extract_token()
                name = await browser.get_profile_name()
                return {"status": "logged_in", "method": "google", "profile_name": name, "has_token": bool(token)}

            if "accounts.google" in browser.page.url:
                return {"status": "waiting_for_user", "message": "Google account picker is open. Select your account, then call naukri_login() again."}

            await asyncio.sleep(3)
            if "/nlogin" not in browser.page.url:
                token = await browser._extract_token()
                name = await browser.get_profile_name()
                return {"status": "logged_in", "method": "google", "profile_name": name, "has_token": bool(token)}

            return {"status": "error", "message": "Google login did not complete. Check the browser."}

        else:
            if not email or not password:
                return {"status": "error", "message": "email and password required for method='email'"}
            try:
                await browser.safe_fill("input#usernameField", email)
                await browser.safe_fill("input#passwordField", password)
                await browser.page.click("button[type='submit']")
            except Exception as e:
                return {"status": "error", "message": f"Login form error: {e}"}

            await asyncio.sleep(3)

            otp_field = await browser.page.query_selector("input#otp")
            if otp_field:
                return {"status": "otp_required", "needs_otp": True, "message": "Enter OTP sent to your phone"}

            if "/nlogin" not in browser.page.url:
                token = await browser._extract_token()
                name = await browser.get_profile_name()
                return {"status": "logged_in", "method": "email", "profile_name": name, "has_token": bool(token)}

            error = await browser.text(".err-message, .error-msg, [class*='error']")
            return {"status": "error", "message": error or "Login failed — check credentials"}


# ============================================================================
# Tool 2: Verify OTP (Playwright)
# ============================================================================


@mcp.tool()
async def naukri_verify_otp(otp: str) -> dict:
    """Enter OTP code after login. Call after naukri_login returns needs_otp.

    Args:
        otp: 6-digit OTP from SMS/email
    """
    async with browser._lock:
        try:
            await browser.safe_fill("input#otp", otp)
            await browser.page.click("button[type='submit']")
            await asyncio.sleep(3)

            if "/nlogin" not in browser.page.url:
                token = await browser._extract_token()
                name = await browser.get_profile_name()
                return {"status": "logged_in", "profile_name": name, "has_token": bool(token)}

            error = await browser.text(".err-message, .error-msg")
            return {"status": "error", "message": error or "OTP verification failed"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
