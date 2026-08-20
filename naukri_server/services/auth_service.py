"""Auth service — pure orchestration for login status and post-login state harvest.

Extracts the I/O-bound, test-friendly pieces of tools/auth.py so the tool layer
becomes a thin adapter over the browser state machine. Browser-tied helpers
(`_login`, `_verify_otp`) remain in tools/auth.py because tests patch them at
that path and the Playwright page state machine has too many SSO branches to
move cleanly.
"""

from __future__ import annotations

from typing import Any

from naukri_server.api import NaukriAPIError
from naukri_server.browser import AuthExpiredError, NotLoggedInError
from naukri_server.config import ACTIVITY_LEVEL_API, logger
from naukri_server.domain import safe_get
from naukri_server.interfaces import api_client, browser_provider

__all__ = [
    "get_login_status",
    "harvest_post_login_state",
    "build_login_success_result",
]


# Session-expiry exceptions: the API layer has PROVEN the session is gone.
_EXPIRY_ERRORS = (NotLoggedInError, AuthExpiredError)
# HTTP codes that are themselves proof of an expired/rejected session.
_EXPIRY_STATUSES = (401, 403)


def _answer(logged_in: bool, verified: bool, reason: str) -> dict:
    """Build the one and only shape this function returns.

    ``status`` is ALWAYS "success": the question "am I logged in?" was answered
    (or explicitly declared unanswerable), so it must never come back as a tool
    error with no ``logged_in`` key -- that is what made the preflight gate
    ``if auth_status()["logged_in"]`` raise KeyError instead of returning False.
    """
    return {
        "status": "success",
        "logged_in": bool(logged_in),
        "verified": bool(verified),
        "reason": reason,
    }


async def get_login_status() -> dict:
    """Answer "is this Naukri session active?" without ever raising.

    Returns ``{status, logged_in: bool, verified: bool, reason: str}``:

    ==========================================  =========  ========  =================
    condition                                   logged_in  verified  reason
    ==========================================  =========  ========  =================
    no cached token                             False      True      no_token
    API confirms the session                    True       True      api_confirmed
    API explicitly denies the session           False      True      api_denied
    NotLoggedInError / AuthExpiredError         False      True      session_expired
    HTTP 401 / 403                              False      True      session_expired: ...
    anything else (network, 5xx, bug, no field) False      FALSE     check_failed: ...
    ==========================================  =========  ========  =================

    ``verified`` is the honesty bit: ``verified=False`` means "could NOT check",
    which is NOT the same claim as "proven logged out". Callers that gate
    destructive work on a logout should require ``verified``.

    Two deliberate properties:

    * The token is read via ``browser_provider.cached_token()``, a NON-MUTATING
      read. ``extract_token()`` would call ``TokenManager.extract()``, which
      CLEARS the cached token when cookie extraction fails -- a status check
      during a browser blip would then destroy a perfectly good session.
    * With no token we answer WITHOUT calling the activity-level API, because
      that endpoint requires a token: calling it was the original bug (it
      raised, the tool layer returned an error dict, and ``logged_in`` was
      simply absent).
    """
    try:
        token = await browser_provider.cached_token()
    except Exception as e:
        logger.warning(
            "auth status: could not read the cached token (%s: %s) - reporting "
            "unverified, NOT a proven logout", type(e).__name__, e,
        )
        return _answer(False, False, "check_failed: %s" % type(e).__name__)

    if not token:
        return _answer(False, True, "no_token")

    try:
        data = await api_client.get(ACTIVITY_LEVEL_API)
    except _EXPIRY_ERRORS as e:
        logger.error(
            "auth status: session expired (%s: %s) - re-authentication required",
            type(e).__name__, e,
        )
        return _answer(False, True, "session_expired")
    except NaukriAPIError as e:
        status_code = getattr(e, "status", None)
        if status_code in _EXPIRY_STATUSES:
            logger.error(
                "auth status: session rejected by the API (HTTP %s) - "
                "re-authentication required", status_code,
            )
            return _answer(False, True, "session_expired: http %s" % status_code)
        logger.warning(
            "auth status: check could not complete (NaukriAPIError HTTP %s: %s)",
            status_code, e,
        )
        return _answer(False, False, "check_failed: NaukriAPIError http %s" % status_code)
    except Exception as e:
        logger.warning(
            "auth status: check could not complete (%s: %s) - reporting "
            "unverified, NOT a proven logout", type(e).__name__, e,
        )
        return _answer(False, False, "check_failed: %s" % type(e).__name__)

    # A response that never mentions loggedInStatus has DENIED nothing -- do not
    # let a missing field masquerade as an explicit "you are logged out".
    if not isinstance(data, dict) or "loggedInStatus" not in data:
        logger.warning(
            "auth status: activity-level response carried no loggedInStatus field "
            "(type %s) - cannot verify", type(data).__name__,
        )
        return _answer(False, False, "check_failed: missing loggedInStatus")

    logged_in = safe_get(
        data, "loggedInStatus", field_name="loggedInStatus", warn=False, default=False)
    if logged_in:
        return _answer(True, True, "api_confirmed")
    return _answer(False, True, "api_denied")


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
