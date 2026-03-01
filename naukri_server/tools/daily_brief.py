"""Daily brief — morning dashboard combining notifications, inbox, recommendations, activity, and dashboard stats."""

import asyncio
from datetime import datetime, timezone

from naukri_server import mcp
from naukri_server.config import logger


@mcp.tool()
async def naukri_daily_brief() -> dict:
    """Get your morning job-hunting dashboard in a single call.

    Runs 9 checks in parallel: unread messages, notifications, new recommendations,
    recruiter activity, profile activity level, today's applications, dashboard stats,
    early access roles, and subscription status.

    Returns:
        - {status: "success", unread_messages, notifications, recommendations,
           recruiter_activity, activity_level, todays_applications, dashboard, errors}
    """
    from naukri_server.tools.inbox import naukri_get_inbox
    from naukri_server.tools.notifications import naukri_get_notifications
    from naukri_server.tools.search import naukri_get_recommendations
    from naukri_server.tools.performance import naukri_get_recruiter_activity, naukri_get_activity_level
    from naukri_server.tools.tracking import naukri_get_applications
    from naukri_server.tools.profile import naukri_get_dashboard
    from naukri_server.tools.early_access import naukri_get_early_access_roles
    from naukri_server.tools.subscription import naukri_get_subscription_status

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    errors = []

    results = await asyncio.gather(
        naukri_get_inbox(limit=5, unread_only=True),
        naukri_get_notifications(limit=5),
        naukri_get_recommendations(limit=5),
        naukri_get_recruiter_activity(size=5),
        naukri_get_activity_level(),
        naukri_get_applications(date_from=today),
        naukri_get_dashboard(),
        naukri_get_early_access_roles(limit=3),
        naukri_get_subscription_status(),
        return_exceptions=True,
    )

    def _extract(idx, label):
        r = results[idx]
        if isinstance(r, Exception):
            errors.append(f"{label}: {type(r).__name__}: {r}")
            return None
        if isinstance(r, dict) and r.get("status") == "error":
            errors.append(f"{label}: {r.get('message', 'unknown')}")
            return None
        return r

    inbox = _extract(0, "Inbox")
    notifs = _extract(1, "Notifications")
    recs = _extract(2, "Recommendations")
    recruiter = _extract(3, "Recruiter activity")
    activity = _extract(4, "Activity level")
    apps = _extract(5, "Applications")
    dashboard = _extract(6, "Dashboard")
    early_access = _extract(7, "Early access")
    subscription = _extract(8, "Subscription")

    brief = {
        "status": "success",
        "date": today,
        "unread_messages": {
            "count": inbox.get("count", 0) if inbox else 0,
            "messages": inbox.get("messages", []) if inbox else [],
        },
        "notifications": {
            "count": notifs.get("count", 0) if notifs else 0,
            "items": notifs.get("notifications", []) if notifs else [],
        },
        "recommendations": {
            "count": recs.get("count", 0) if recs else 0,
            "jobs": recs.get("jobs", []) if recs else [],
        },
        "recruiter_activity": {
            "total": recruiter.get("total_actions", 0) if recruiter else 0,
            "change": recruiter.get("percentage_change") if recruiter else None,
            "recent": recruiter.get("activities", []) if recruiter else [],
        },
        "activity_level": activity.get("level", "UNKNOWN") if activity else "UNKNOWN",
        "todays_applications": {
            "count": apps.get("count", 0) if apps else 0,
            "applications": apps.get("applications", []) if apps else [],
        },
        "dashboard": {
            "profile_views": dashboard.get("profile_views", 0) if dashboard else 0,
            "total_matches": dashboard.get("total_matches", 0) if dashboard else 0,
            "unread_invites": dashboard.get("unread_invites", 0) if dashboard else 0,
        },
        "early_access_roles": {
            "count": early_access.get("count", 0) if early_access else 0,
            "roles": early_access.get("roles", []) if early_access else [],
        },
        "subscription": subscription if subscription else None,
    }

    if errors:
        brief["status"] = "partial_success"
        brief["errors"] = errors

    return brief
