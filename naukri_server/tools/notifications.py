"""Notification tools — view and manage Naukri notification center."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post
from naukri_server.config import logger, NOTIFICATION_FEED_API, NOTIFICATION_READ_API, NOTIFICATION_COUNT_API, MAX_MARK_ALL_ITERATIONS, RECOMMEND_NOTIFY_API, BROWSER_DOM_SETTLE
from naukri_server.error_handler import handle_tool_action
from naukri_server.validation import validate_limit, validate_page


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _fetch_notifications(limit: int = 20, page: int = 1, notif_type: Optional[str] = None) -> dict:
    """Fetch notifications from the API and return structured result.

    Args:
        limit: Max notifications to return (capped at 50).
        page: Page number (1-based).
        notif_type: Optional type filter — case-insensitive substring match
                    against the notification ``type`` field.  Common values
                    include ``"JA"`` (job alerts), ``"RA"`` (recruiter
                    activity), ``"SYSTEM"``, ``"APPLICATION_UPDATE"``.
    """
    limit = validate_limit(limit)
    page = validate_page(page)
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
            "metadata": notif.get("metadata", {}),
        })

    # Client-side type filtering (API does not support server-side filtering)
    if notif_type:
        notif_type_lower = notif_type.lower()
        notifications = [n for n in notifications if notif_type_lower in n["type"].lower()]

    result: dict = {
        "status": "success",
        "total": data.get("totalCount", len(notifications)) if isinstance(data, dict) else len(notifications),
        "count": len(notifications),
        "notifications": notifications,
    }
    if notif_type:
        result["filtered_by"] = notif_type
    return result


async def _mark_single_read(notification_id: str, date: str) -> dict:
    """Mark a single notification as read via the API."""
    await api_post(NOTIFICATION_READ_API, body={
        "notificationId": notification_id,
        "createdAt": date,
    })
    return {
        "status": "success",
        "notification_id": notification_id,
    }


async def _get_unified_notify() -> dict:
    """Fetch unified notification dashboard — all categories in one call.

    Returns 8 notification categories with counts, latest status, and display metadata.
    Single call replaces multiple separate notification API calls.
    """
    data = await api_get(RECOMMEND_NOTIFY_API)
    status_obj = data.get("status", data)
    order = data.get("order", [
        "recoJobs", "appStatus", "criticalActions", "rmj",
        "FF", "NL", "RR", "recruiterSearch",
    ])
    categories = {}
    for key in order:
        cat_data = status_obj.get(key, {})
        if not cat_data or not isinstance(cat_data, dict):
            continue
        entry = {
            "label": cat_data.get("label", key),
            "type": cat_data.get("type", key),
            "count": cat_data.get("noti_count") or cat_data.get("total_count") or cat_data.get("count", 0),
            "total_count": cat_data.get("total_count"),
            "noti_count": cat_data.get("noti_count"),
            "show_on_gnb": cat_data.get("showOnGnb", True),
            "has_new": bool(cat_data.get("noti_count", 0)),
        }
        if "status" in cat_data:
            entry["latest_status"] = cat_data["status"]
        if "noti_description" in cat_data:
            entry["latest_description"] = cat_data["noti_description"]
        if "freq" in cat_data:
            entry["frequency_cap"] = cat_data["freq"]
        categories[key] = entry
    return {
        "status": "success",
        "source": "unified_notify",
        "new_count": data.get("newCount", 0),
        "total_count": data.get("totalCount", 0),
        "categories": categories,
        "total_types": len([c for c in categories.values() if c.get("show_on_gnb")]),
        "display_order": order,
    }


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_notifications(
    action: str = "list",
    notification_id: Optional[str] = None,
    date: Optional[str] = None,
    limit: int = 20,
    page: int = 1,
    notif_type: Optional[str] = None,
) -> dict:
    """Unified notification management — list, count, and mark read.

    Actions:
      - "list": Fetch notifications (use limit/page for pagination, notif_type to filter)
      - "count": Get unread notification count
      - "mark_read": Mark a single notification as read (requires notification_id, date)
      - "mark_all_read": Mark ALL unread notifications as read
      - "summary": Get unified notification dashboard — all categories in one call (recoJobs, appStatus, criticalActions, rmj, recruiterSearch, etc.)

    Args:
        action: "list" | "count" | "mark_read" | "mark_all_read" | "summary"
        notification_id: Required for mark_read — the notification ID
        date: Required for mark_read — the notification date string
        limit: Max notifications for list action (default 20)
        page: Page number for list action (default 1)
        notif_type: For list — filter by notification type (case-insensitive substring).
                    Common types: "JA" (job alerts), "RA" (recruiter activity),
                    "SYSTEM", "APPLICATION_UPDATE".  Omit for all types.

    Returns:
        - list: {status, count, notifications: [...], filtered_by?}
        - count: {status, count}
        - mark_read: {status, notification_id}
        - mark_all_read: {status, marked_count, already_read, total_processed}
        - summary: {status, source, categories, total_types}
        - {status: "error", message} on failure
    """
    # ── list ───────────────────────────────────────────────────────────
    if action == "list":
        return await handle_tool_action(lambda: _fetch_notifications(limit=limit, page=page, notif_type=notif_type), "notifications.list")

    # ── count ──────────────────────────────────────────────────────────
    elif action == "count":
        async def _count():
            data = await api_get(NOTIFICATION_COUNT_API)
            return {
                "status": "success",
                "count": data.get("count", 0),
            }
        return await handle_tool_action(_count, "notifications.count")

    # ── mark_read ──────────────────────────────────────────────────────
    elif action == "mark_read":
        if not notification_id or not date:
            return {"status": "error", "message": "mark_read requires notification_id and date.", "error_code": "VALIDATION_ERROR"}
        return await handle_tool_action(lambda: _mark_single_read(notification_id, date), "notifications.mark_read")

    # ── mark_all_read ──────────────────────────────────────────────────
    elif action == "mark_all_read":
        async def _mark_all():
            total_marked = 0
            total_read = 0
            total_processed = 0
            all_errors = []

            iteration = 0
            while iteration < MAX_MARK_ALL_ITERATIONS:
                iteration += 1
                result = await _fetch_notifications(limit=50)
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

                if not unread:
                    break

                # Parallel mark-read
                mark_tasks = [
                    _mark_single_read(notification_id=n["id"], date=n["date"])
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

                await asyncio.sleep(BROWSER_DOM_SETTLE)

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
        return await handle_tool_action(_mark_all, "notifications.mark_all_read")

    # ── summary ────────────────────────────────────────────────────────
    elif action == "summary":
        return await handle_tool_action(_get_unified_notify, "notifications.summary")

    # ── unknown action ─────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, count, mark_read, mark_all_read, summary", "error_code": "VALIDATION_ERROR"}
