"""Shared error handling for MCP tool dispatch functions."""

import logging

from naukri_server.api import NaukriAPIError, api_tool  # noqa: F401 — re-export

logger = logging.getLogger(__name__)


async def handle_tool_action(handler_fn, action_name: str) -> dict:
    """Run a tool action with standardized error handling.

    Args:
        handler_fn: Async callable that returns a dict
        action_name: Name for logging (e.g., "profile.get", "insights.taxonomy")

    Returns:
        Result dict from handler_fn, or error dict on failure.
    """
    try:
        result = await handler_fn()
        return result
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
    except Exception as e:
        logger.exception("Unexpected error in %s", action_name)
        return {"status": "error", "message": f"Internal error: {type(e).__name__}: {e}", "error_code": "INTERNAL_ERROR"}
