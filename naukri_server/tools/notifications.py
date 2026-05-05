"""Notification tools — view and manage Naukri notification center.

Tool layer — business logic lives in ``services/notification_service.py``.
``api_client``, ``asyncio``, and the underscore-prefixed helpers are
re-exported on this module because the test suite patches paths like
``naukri_server.tools.notifications.api_client.get`` and
``naukri_server.tools.notifications._fetch_notifications`` directly.
"""

import asyncio  # noqa: F401 — re-exported so tests can patch asyncio.sleep on this module
from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action
from naukri_server.interfaces import api_client  # noqa: F401 — tests patch this path
from naukri_server.services.notification_service import (
    fetch_notifications as _fetch_notifications,
    mark_single_read as _mark_single_read,
    get_unified_notify as _get_unified_notify,
    get_notification_count as _get_notification_count,
    mark_all_notifications_read as _mark_all_notifications_read_service,
)


async def _mark_all_notifications_read_impl() -> dict:
    """Tool-layer adapter — re-binds fetch/mark/sleep to the patchable
    aliases on this module, then delegates to the service implementation.

    This indirection lets tests patch
    ``naukri_server.tools.notifications._fetch_notifications``,
    ``_mark_single_read``, and ``asyncio.sleep`` and have those replacements
    take effect inside the saga loop.
    """
    # Late lookup so test patches on this module are honored.
    import naukri_server.tools.notifications as _self
    return await _mark_all_notifications_read_service(
        fetch=_self._fetch_notifications,
        mark=_self._mark_single_read,
        sleep=_self.asyncio.sleep,
    )


# Re-export for backward compatibility (tests + daily_brief).
__all__ = [
    "_fetch_notifications",
    "_mark_single_read",
    "_get_unified_notify",
    "_get_notification_count",
    "_mark_all_notifications_read_impl",
]


# ---------------------------------------------------------------------------
# Individual MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_list_notifications(
    limit: int = 20,
    page: int = 1,
    notif_type: Optional[str] = None,
) -> dict:
    """List Naukri notifications with optional type filtering.

    Args:
        limit: Max notifications to return (default 20, max 50).
        page: Page number for pagination (default 1).
        notif_type: Filter by type substring (case-insensitive). Common: "JA", "RA", "SYSTEM", "APPLICATION_UPDATE".

    Returns:
        {status, total, count, notifications: [{id, title, message, type, date, is_read, url, metadata}], filtered_by?}
    """
    return await handle_tool_action(
        lambda: _fetch_notifications(limit=limit, page=page, notif_type=notif_type),
        "notifications.list",
    )


@mcp.tool()
async def naukri_notification_count() -> dict:
    """Get unread notification count.

    Returns:
        {status, count}
    """
    return await handle_tool_action(_get_notification_count, "notifications.count")


@mcp.tool()
async def naukri_mark_notification_read(
    notification_id: Optional[str] = None,
    date: Optional[str] = None,
) -> dict:
    """Mark a single notification as read.

    Args:
        notification_id: The notification ID (required).
        date: The notification date string (required).

    Returns:
        {status, notification_id}
    """
    if not notification_id or not date:
        return {
            "status": "error",
            "message": "mark_read requires notification_id and date.",
            "error_code": "VALIDATION_ERROR",
        }
    return await handle_tool_action(
        lambda: _mark_single_read(notification_id, date),
        "notifications.mark_read",
    )


@mcp.tool()
async def naukri_mark_all_notifications_read() -> dict:
    """Mark all unread notifications as read (paginated, up to MAX_MARK_ALL_ITERATIONS batches).

    Returns:
        {status, marked_count, already_read, total_processed, errors?}
    """
    return await handle_tool_action(
        _mark_all_notifications_read_impl, "notifications.mark_all_read",
    )


@mcp.tool()
async def naukri_notification_summary() -> dict:
    """Get unified notification dashboard — all categories in one call.

    Returns 8 categories (recoJobs, appStatus, criticalActions, rmj, FF, NL, RR, recruiterSearch)
    with counts, latest status, and display metadata.

    Returns:
        {status, source, new_count, total_count, categories, total_types, display_order}
    """
    return await handle_tool_action(_get_unified_notify, "notifications.summary")
