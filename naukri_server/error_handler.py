"""Shared error handling for MCP tool dispatch functions."""

import logging

from naukri_server.browser import (
    AuthExpiredError,
    BrowserUnavailableError,
    NotLoggedInError,
    ServerWarmingUpError,
)
from naukri_server.api import NaukriAPIError, api_tool  # noqa: F401 — re-export
from naukri_server.readiness import readiness

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

#: What a signed-out caller is told to DO. First run of a fresh clone is the
#: case this exists for: whoever clones this repo is not the person who built
#: it, has no Chrome profile and no session, and the first authenticated tool
#: they touch must name the next step rather than leave them to infer it.
#:
#: MEASURED 2026-08-25, before this existed: a signed-out `naukri_get_profile`
#: returned `error_code: "API_ERROR"` -- not even classified as auth -- with
#: the exception class printed twice and no tool named. Nothing in that reply
#: tells a stranger what to do next.
_SIGNED_OUT_NEXT_STEP = (
    "Not signed in to Naukri. Run naukri_auth_status to confirm, then "
    "naukri_login to sign in (it opens a browser window; this server never "
    "handles a password). If you have signed in before, naukri_reauth renews "
    "an expired credential silently, without a sign-in form."
)

#: HTTP statuses that mean "who are you", as opposed to "what you asked for
#: went wrong". 403 is included deliberately: Naukri answers 403 for a lapsed
#: session on some routes, and reading it as a permissions problem sends the
#: caller looking for an entitlement they already have.
_AUTH_STATUSES = frozenset({401, 403})


def classify_api_error(exc) -> tuple:
    """(error_code, message) for a NaukriAPIError. One rule, one place.

    Exists because the same 401 was classified two different ways depending on
    which dispatch path a tool happened to use: ``api_tool`` mapped it to
    AUTH_ERROR, while :func:`handle_tool_action` returned API_ERROR
    unconditionally. A caller cannot act on an error code that depends on an
    implementation detail of the tool it called.
    """
    status = getattr(exc, "status", None)
    if getattr(exc, "code", None) == "BOT_CHECK":
        return "BOT_CHECK", str(exc)
    if status in _AUTH_STATUSES:
        # `str(exc)` already begins "HTTP <status>: ", so the status is not
        # repeated here -- it read "HTTP 401: Unauthorized (HTTP 401)".
        detail = str(exc) or "HTTP %s" % status
        return "AUTH_ERROR", "%s. %s" % (detail.rstrip(". "), _SIGNED_OUT_NEXT_STEP)
    return "API_ERROR", str(exc)


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
    # PRE-FLIGHT, NOT RETRY. If the background warm-up has not produced a
    # browser yet, spend a bounded moment here -- BEFORE the tool body runs --
    # rather than letting the body fail and re-running it. A retry-after-failure
    # would re-execute whatever the tool already did before its first browser
    # touch, and in this server that can include an application write or a saga
    # step. Waiting first is the only version that is safe to do unconditionally.
    #
    # Costs one bool read once the server is ready (browser_pending is False),
    # and the wait ends the instant the browser phase completes -- it does not
    # hold a caller for probes or the scheduler coming up behind it.
    #
    # Browser-free tools (config reads, DB reads, scheduler status) pay this
    # wait too during the startup window. Accepted deliberately: there is no
    # per-tool "needs browser" declaration in this codebase to key off, the
    # window is seconds, and the alternative -- inventing one across 120 tools --
    # buys latency at the cost of a classification that would rot.
    if readiness.browser_pending:
        await readiness.wait_for_browser()

    try:
        # ONE CALL, ONE POLICY. Bind the config snapshot here, at the single
        # seam every tool passes through, and hold it for the whole call. Bound
        # per-scoring-call instead, a concurrent write could swap the weights
        # between two rows of one ranking — naukri is one process running HTTP
        # + stdio + nine scheduled tasks on one event loop, so every `await`
        # inside a ranking loop is a yield point where that can happen.
        # Failure to bind must never break a tool: run unbound and let
        # `snapshot()` fall back to the shipped defaults.
        try:
            from naukri_server import policy as _policy

            with _policy.bind():
                return await handler_fn()
        except ImportError:
            return await handler_fn()
    except NaukriAPIError as e:
        code, message = classify_api_error(e)
        if code == "AUTH_ERROR":
            logger.error("Auth failure in %s (HTTP %s)", action_name, e.status)
        return {"status": "error", "message": message,
                "http_status": e.status, "error_code": code}
    except ServerWarmingUpError as e:
        # BEFORE _AUTH_ERRORS and _BROWSER_ERRORS on purpose. This is a
        # subclass of BrowserUnavailableError, so an ordering slip would bury it
        # as BROWSER_ERROR and the caller would go looking for a dead browser
        # that is merely unborn. The payload carries the phase and the elapsed
        # time because "wait and retry" is only actionable if the caller can see
        # that something is in fact progressing.
        logger.info("Tool %s arrived during warm-up (%s)", action_name, readiness.phase)
        return {
            "status": "error",
            "message": str(e),
            "error_code": "SERVER_WARMING_UP",
            "readiness": readiness.snapshot(),
        }
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
