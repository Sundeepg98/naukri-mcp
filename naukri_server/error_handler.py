"""Shared error handling for MCP tool dispatch functions."""

import logging

from naukri_server.browser import (
    AuthExpiredError,
    BrowserUnavailableError,
    NotLoggedInError,
)
from naukri_server.api import NaukriAPIError, api_tool  # noqa: F401 — re-export

logger = logging.getLogger(__name__)

# One error_code per condition. Without these, an expired session and a dead
# browser both surfaced as INTERNAL_ERROR, so callers could not tell "run
# naukri_login" from "the browser died" -- the mis-attribution this module
# exists to stop.
_AUTH_ERRORS = (NotLoggedInError, AuthExpiredError)

# Playwright's "the tab/context/browser went away" error. Imported defensively:
# a Playwright packaging change should degrade to "no browser classification",
# never break every tool dispatch in the server.
try:  # pragma: no cover - import shape, not behaviour
    from playwright._impl._errors import TargetClosedError
    _BROWSER_ERRORS = (BrowserUnavailableError, TargetClosedError)
except Exception:  # pragma: no cover - playwright always present in practice
    _BROWSER_ERRORS = (BrowserUnavailableError,)

_REAUTH_HINT = (
    "Naukri session expired or not logged in. Call naukri_login to re-authenticate."
)


async def handle_tool_action(handler_fn, action_name: str) -> dict:
    """Run a tool action with standardized error handling.

    Args:
        handler_fn: Async callable that returns a dict
        action_name: Name for logging (e.g., "profile.get", "insights.taxonomy")

    Returns:
        Result dict from handler_fn, or an error dict on failure. The
        ``error_code`` names the CONDITION, one per class:
        API_ERROR (NaukriAPIError, carries http_status), AUTH_ERROR (session
        expired / never logged in -> run naukri_login), BROWSER_ERROR (browser
        dead or circuit open), INTERNAL_ERROR (genuinely unexpected).
    """
    try:
        result = await handler_fn()
        return result
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
    except _AUTH_ERRORS as e:
        logger.error("Auth failure in %s (%s): %s", action_name, type(e).__name__, e)
        return {"status": "error", "message": str(e) or _REAUTH_HINT, "error_code": "AUTH_ERROR"}
    except _BROWSER_ERRORS as e:
        logger.error("Browser failure in %s (%s): %s", action_name, type(e).__name__, e)
        return {
            "status": "error",
            "message": f"Browser unavailable: {type(e).__name__}: {e}",
            "error_code": "BROWSER_ERROR",
        }
    except Exception as e:
        logger.exception("Unexpected error in %s", action_name)
        return {"status": "error", "message": f"Internal error: {type(e).__name__}: {e}", "error_code": "INTERNAL_ERROR"}
