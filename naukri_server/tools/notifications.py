"""Notification tools — view and manage Naukri notification center."""

import asyncio

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.config import logger, NOTIFICATION_FEED_API, NOTIFICATION_READ_API, NOTIFICATION_COUNT_API


@mcp.tool()
@api_tool("Get notifications")
async def naukri_get_notifications(limit: int = 20, page: int = 1) -> dict:
    """List notifications from your Naukri notification center.

    Args:
        limit: Max notifications to return (default 20)
        page: Page number for pagination (default 1)

    Returns:
        - {status: "success", count, notifications: [{id, title, message, type, date, is_read, url}]}
        - {status: "error", message}
    """
    data = await api_get(NOTIFICATION_FEED_API, params={
        "page": str(page),
        "limit": str(limit),
    })

    # Response is a list of notification objects
    notif_list = data if isinstance(data, list) else data.get("notifications", data.get("feed", []))

    notifications = []
    for notif in notif_list:
        notifications.append({
            "id": notif.get("id", ""),
            "title": notif.get("displayTitle", ""),
            "message": notif.get("message", ""),
            "type": notif.get("type", ""),
            "date": notif.get("createdAt", ""),
            "is_read": bool(notif.get("readStatus")),
            "url": notif.get("url", ""),
        })

    return {
        "status": "success",
        "total": data.get("totalCount", len(notifications)) if isinstance(data, dict) else len(notifications),
        "count": len(notifications),
        "notifications": notifications,
    }


@mcp.tool()
@api_tool("Mark notification read")
async def naukri_mark_notification_read(notification_id: str, date: str) -> dict:
    """Mark a notification as read in your Naukri notification center.

    Requires: notification_id and date from naukri_get_notifications results.

    Args:
        notification_id: Notification ID to mark as read
        date: The notification's date field from naukri_get_notifications

    Returns:
        - {status: "success", notification_id}
        - {status: "error", message}
    """
    await api_post(NOTIFICATION_READ_API, body={
        "notificationId": notification_id,
        "createdAt": date,
    })
    return {
        "status": "success",
        "notification_id": notification_id,
    }


@mcp.tool()
@api_tool("Mark all notifications read")
async def naukri_mark_all_notifications_read() -> dict:
    """Mark all unread notifications as read in your Naukri notification center.

    Fetches all notifications (with pagination), filters unread ones, and
    marks them as read in parallel batches.

    Returns:
        - {status: "success", marked_count, already_read, total_processed}
        - {status: "error", message}
    """
    total_marked = 0
    total_read = 0
    total_processed = 0
    all_errors = []

    while True:
        result = await naukri_get_notifications(limit=50)
        if result.get("status") != "success":
            if total_processed == 0:
                return result
            break

        notifications = result.get("notifications", [])
        if not notifications:
            break

        total_processed += len(notifications)
        unread = [n for n in notifications if not n.get("is_read")]
        total_read += len(notifications) - len(unread)

        if unread:
            # Parallel mark-read
            mark_tasks = [
                naukri_mark_notification_read(notification_id=n["id"], date=n["date"])
                for n in unread
            ]
            mark_results = await asyncio.gather(*mark_tasks, return_exceptions=True)
            for mr in mark_results:
                if isinstance(mr, Exception):
                    all_errors.append(str(mr))
                elif isinstance(mr, dict) and mr.get("status") == "success":
                    total_marked += 1
                else:
                    all_errors.append(mr.get("message", "unknown") if isinstance(mr, dict) else str(mr))

        # If we got fewer than 50, we've reached the end
        if len(notifications) < 50:
            break

    return {
        "status": "success",
        "marked_count": total_marked,
        "already_read": total_read,
        "total_processed": total_processed,
        "errors": all_errors if all_errors else None,
    }


@mcp.tool()
@api_tool("Get notification count")
async def naukri_get_notification_count() -> dict:
    """Get the count of unread notifications. Lightweight alternative to fetching the full notification feed.

    Returns:
        - {status: "success", count: N}
        - {status: "error", message}
    """
    data = await api_get(NOTIFICATION_COUNT_API)
    return {
        "status": "success",
        "count": data.get("count", 0),
    }
