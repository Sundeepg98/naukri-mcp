"""Auth service — pure orchestration for login status and post-login state harvest.

Extracts the I/O-bound, test-friendly pieces of tools/auth.py so the tool layer
becomes a thin adapter over the browser state machine. Browser-tied helpers
(`_login`, `_verify_otp`) remain in tools/auth.py because tests patch them at
that path and the Playwright page state machine has too many SSO branches to
move cleanly.
"""

from __future__ import annotations

from typing import Any

from naukri_server.config import ACTIVITY_LEVEL_API, logger
from naukri_server.domain import safe_get
from naukri_server.interfaces import api_client

__all__ = [
    "get_login_status",
    "harvest_post_login_state",
    "build_login_success_result",
]


async def get_login_status() -> dict:
    """Check whether the Naukri session is still active via the activity-level API.

    Pure orchestration: hits the API through the ``api_client`` interface and
    extracts a single boolean via the anti-corruption layer.
    """
    data = await api_client.get(ACTIVITY_LEVEL_API)
    return {
        "status": "success",
        "logged_in": safe_get(data, "loggedInStatus", field_name="loggedInStatus", warn=False, default=False),
    }


async def harvest_post_login_state(browser_module: Any) -> tuple[str | None, str | None]:
    """Extract the auth token and profile name after a successful login redirect.

    Returns ``(token, profile_name)``. Either may be None on failure.

    Takes the browser module as a parameter so callers can inject the production
    ``naukri_server.browser`` or a fake. The token extraction is intentionally
    eager — Naukri's login redirect drops the token into a cookie that the
    `TokenManager` reads on demand.
    """
    try:
        await browser_module.token_manager.extract()
        token = browser_module.token_manager._token
        # Mirror the legacy assignment so other consumers reading
        # browser.token directly continue to work.
        browser_module.token = token
    except Exception as e:
        logger.debug("post-login token extract failed: %s", e)
        token = None

    try:
        name = await browser_module.get_profile_name()
    except Exception as e:
        logger.debug("post-login profile-name fetch failed: %s", e)
        name = None

    return token, name


def build_login_success_result(
    *, token: str | None, profile_name: str | None,
    method: str | None = None, already_logged_in: bool = False,
) -> dict:
    """Build the standard logged_in / already_logged_in result dict.

    Returns a structured error dict if no token was harvested — every caller
    used to repeat this same check inline.
    """
    if not token:
        return {
            "status": "error",
            "message": "Login redirect completed but auth token not found. Try again.",
            "error_code": "AUTH_ERROR",
        }
    if already_logged_in:
        return {
            "status": "already_logged_in",
            "profile_name": profile_name,
            "has_token": True,
        }
    result: dict = {"status": "logged_in", "has_token": True, "profile_name": profile_name}
    if method:
        result["method"] = method
    return result
