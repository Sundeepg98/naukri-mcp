"""Notification tools — view and manage Naukri notification center."""

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import logger, NOTIFICATION_FEED_API, NOTIFICATION_READ_API


@mcp.tool()
async def naukri_get_notifications(limit: int = 20, page: int = 1) -> dict:
    """List notifications from your Naukri notification center.

    Args:
        limit: Max notifications to return (default 20)
        page: Page number for pagination (default 1)

    Returns:
        - {status: "success", count, notifications: [{id, title, message, type, date, is_read, url}]}
        - {status: "error", message}
    """
    try:
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
            "count": len(notifications),
            "notifications": notifications,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get notifications: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_mark_notification_read(notification_id: str, created_at: str) -> dict:
    """Mark a notification as read in your Naukri notification center.

    Requires: notification_id and created_at from naukri_get_notifications results.

    Args:
        notification_id: Notification ID to mark as read
        created_at: The notification's creation timestamp (from the 'date' field in naukri_get_notifications)

    Returns:
        - {status: "success", notification_id}
        - {status: "error", message}
    """
    try:
        await api_post(NOTIFICATION_READ_API, body={
            "notificationId": notification_id,
            "createdAt": created_at,
        })
        return {
            "status": "success",
            "notification_id": notification_id,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to mark notification read: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_mark_all_notifications_read() -> dict:
    """Mark all unread notifications as read in your Naukri notification center.

    Fetches all notifications, filters unread ones, and marks each as read.

    Returns:
        - {status: "success", marked_count, already_read}
        - {status: "error", message}
    """
    try:
        result = await naukri_get_notifications(limit=50)
        if result.get("status") != "success":
            return result

        notifications = result.get("notifications", [])
        unread = [n for n in notifications if not n.get("is_read")]

        if not unread:
            return {
                "status": "success",
                "marked_count": 0,
                "already_read": len(notifications),
            }

        marked = 0
        errors = []
        for n in unread:
            mark_result = await naukri_mark_notification_read(n["id"], n["date"])
            if mark_result.get("status") == "success":
                marked += 1
            else:
                errors.append({"id": n["id"], "error": mark_result.get("message")})

        return {
            "status": "success",
            "marked_count": marked,
            "already_read": len(notifications) - len(unread),
            "errors": errors if errors else None,
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to mark all notifications read: {type(e).__name__}: {e}"}
