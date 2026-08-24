"""Notification service — business logic for fetch / mark-read / unified
notification dashboard.

Extracted from tools/notifications.py as part of Wave 2B layer-separation
refactor. Tools become thin orchestrators that delegate here.
"""

import asyncio
from typing import Optional

from naukri_server.interfaces import api_client
from naukri_server.config import (
    logger, NOTIFICATION_FEED_API, NOTIFICATION_READ_API,
    NOTIFICATION_COUNT_API, MAX_MARK_ALL_ITERATIONS, RECOMMEND_NOTIFY_API,
    BROWSER_DOM_SETTLE,
)
from naukri_server.domain import safe_get
from naukri_server.validation import validate_limit, validate_page

__all__ = [
    "fetch_notifications",
    "mark_single_read",
    "get_unified_notify",
    "get_notification_count",
    "mark_all_notifications_read",
]


async def fetch_notifications(
    limit: int = 20,
    page: int = 1,
    notif_type: Optional[str] = None,
) -> dict:
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
    data = await api_client.get(NOTIFICATION_FEED_API, params={
        "page": str(page),
        "limit": str(limit),
    })

    # Response is a list of notification objects
    notif_list = data if isinstance(data, list) else data.get(
        "notifications", data.get("feed", []),
    )

    notifications = []
    for notif in notif_list:
        notifications.append({
            "id": safe_get(notif, "id", default=""),
            "title": safe_get(notif, "displayTitle", default=""),
            "message": safe_get(notif, "message", default=""),
            "type": safe_get(notif, "type", default=""),
            "date": safe_get(notif, "createdAt", default=""),
            "is_read": bool(safe_get(notif, "readStatus", default=False)),
            "url": safe_get(notif, "url", default=""),
            "metadata": safe_get(notif, "metadata", default={}),
        })

    # Client-side type filtering (API does not support server-side filtering)
    if notif_type:
        notif_type_lower = notif_type.lower()
        notifications = [
            n for n in notifications if notif_type_lower in n["type"].lower()
        ]

    result: dict = {
        "status": "success",
        "total": (
            data.get("totalCount", len(notifications))
            if isinstance(data, dict) else len(notifications)
        ),
        "count": len(notifications),
        "notifications": notifications,
    }
    if notif_type:
        result["filtered_by"] = notif_type
    return result


async def mark_single_read(notification_id: str, date: str) -> dict:
    """Mark a single notification as read via the API."""
    await api_client.post(NOTIFICATION_READ_API, body={
        "notificationId": notification_id,
        "createdAt": date,
    })
    return {
        "status": "success",
        "notification_id": notification_id,
    }


async def get_unified_notify() -> dict:
    """Fetch unified notification dashboard — all categories in one call.

    Returns 8 notification categories with counts, latest status, and display metadata.
    Single call replaces multiple separate notification API calls.
    """
    data = await api_client.get(RECOMMEND_NOTIFY_API)
    status_obj = safe_get(data, "status", default=data)
    order = safe_get(data, "order", default=[
        "recoJobs", "appStatus", "criticalActions", "rmj",
        "FF", "NL", "RR", "recruiterSearch",
    ])
    categories = {}
    for key in order:
        cat_data = status_obj.get(key, {}) if isinstance(status_obj, dict) else {}
        if not cat_data or not isinstance(cat_data, dict):
            continue
        entry = {
            "label": safe_get(cat_data, "label", default=key),
            "type": safe_get(cat_data, "type", default=key),
            "count": (
                cat_data.get("noti_count")
                or cat_data.get("total_count")
                or cat_data.get("count", 0)
            ),
            "total_count": cat_data.get("total_count"),
            "noti_count": cat_data.get("noti_count"),
            "show_on_gnb": safe_get(cat_data, "showOnGnb", default=True),
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
        "new_count": safe_get(data, "newCount", default=0),
        "total_count": safe_get(data, "totalCount", default=0),
        "categories": categories,
        "total_types": len([c for c in categories.values() if c.get("show_on_gnb")]),
        "display_order": order,
    }


async def get_notification_count() -> dict:
    """Fetch unread notification count from API.

    A MISSING FIELD REPORTS UNKNOWN, NEVER ZERO. This read
    ``safe_get(data, "count", default=0)`` with the anti-corruption layer's
    ``field_name``/``warn`` arguments both omitted, so an absent or drifted
    key became a confident ``0`` under ``status: "success"`` -- no log line,
    no error channel, and indistinguishable from a real measurement of
    "nothing is unread". That zero is the failure mode; ``count: None`` with
    ``counted: False`` and a ``reason`` cannot be mistaken for a measurement.

    Same shape as ``_triage_inbox``'s ``scored``/``scoring_error`` pair.

    MEASURED 2026-08-24 via a read-only GET through the live server: the
    endpoint answers HTTP 200 with body ``{"count": 8}``, so the key is
    correct today and this is a guard against drift, not a repair of it.

    Returns:
        {status: "success", count: int|None, counted: bool, reason?: str}
    """
    data = await api_client.get(NOTIFICATION_COUNT_API)
    raw = safe_get(data, "count", default=None, field_name="count",
                   warn=True, context="notifications.count")

    count = None
    if isinstance(raw, bool):
        count = None
    elif isinstance(raw, int):
        count = raw
    elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        # A gateway that stringifies its numbers has still MEASURED one.
        count = int(raw.strip())

    if count is None:
        return {
            "status": "success",
            "count": None,
            "counted": False,
            "reason": "the count endpoint returned no usable 'count' field "
                      "(got %s) -- unknown, not zero" % type(raw).__name__,
        }
    return {"status": "success", "count": count, "counted": True}


async def mark_all_notifications_read(
    fetch=fetch_notifications,
    mark=mark_single_read,
    sleep=asyncio.sleep,
) -> dict:
    """Iterate through unread notifications and mark all read in parallel batches.

    Args:
        fetch: Async callable to fetch a page of notifications (defaults to
            this module's ``fetch_notifications`` so tests can patch the
            tool-layer alias and have it propagate).
        mark: Async callable to mark a single notification read.
        sleep: Async sleep used between batches.
    """
    total_marked = 0
    total_read = 0
    total_processed = 0
    all_errors: list[str] = []

    iteration = 0
    while iteration < MAX_MARK_ALL_ITERATIONS:
        iteration += 1
        result = await fetch(limit=50)
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

        mark_tasks = [
            mark(notification_id=n["id"], date=n["date"])
            for n in unread
        ]
        mark_results = await asyncio.gather(*mark_tasks, return_exceptions=True)
        for mr in mark_results:
            if isinstance(mr, Exception):
                all_errors.append(str(mr))
            elif isinstance(mr, dict) and mr.get("status") == "success":
                total_marked += 1
            else:
                all_errors.append(
                    mr.get("message", "unknown") if isinstance(mr, dict) else str(mr)
                )

        await sleep(BROWSER_DOM_SETTLE)

        if len(notifications) < 50:
            break

    return {
        "status": "success",
        "marked_count": total_marked,
        "already_read": total_read,
        "total_processed": total_processed,
        "errors": all_errors if all_errors else None,
    }
