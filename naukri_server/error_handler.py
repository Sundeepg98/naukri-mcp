"""Shared error handling for MCP tool dispatch functions."""

import logging
from naukri_server.api import NaukriAPIError
from naukri_server.correlation import set_request_id, get_request_id

logger = logging.getLogger(__name__)


async def handle_tool_action(handler_fn, action_name: str) -> dict:
    """Run a tool action with standardized error handling.

    Auto-assigns a correlation ID for tracing multi-step workflows.

    Args:
        handler_fn: Async callable that returns a dict
        action_name: Name for logging (e.g., "profile.get", "insights.taxonomy")

    Returns:
        Result dict from handler_fn, or error dict on failure.
        Always includes ``_request_id`` for correlation.
    """
    rid = set_request_id()
    try:
        result = await handler_fn()
        if isinstance(result, dict):
            result["_request_id"] = rid
        return result
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR", "_request_id": rid}
    except Exception as e:
        logger.exception("Unexpected error in %s [%s]", action_name, rid)
        return {"status": "error", "message": f"Internal error: {type(e).__name__}: {e}", "error_code": "INTERNAL_ERROR", "_request_id": rid}
