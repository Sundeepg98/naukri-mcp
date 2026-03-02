"""Daily brief — morning dashboard combining notifications, inbox, recommendations, activity, and dashboard stats."""

import asyncio
from datetime import datetime, timezone

from naukri_server import mcp
from naukri_server.config import logger


@mcp.tool()
async def naukri_daily_brief() -> dict:
    """Get your morning job-hunting dashboard in a single call.

    Runs 16 checks in parallel: unread messages, notifications, new recommendations,
    recruiter activity, profile activity level, today's applications, dashboard stats,
    early access roles, subscription status, due reminders, stale applications,
    job alerts, profile completeness, saved jobs count, search impressions,
    and assessment status.

    Returns:
        - {status: "success", unread_messages, notifications, recommendations,
           recruiter_activity, activity_level, todays_applications, dashboard,
           due_reminders, stale_applications, job_alerts, profile_completeness,
           saved_jobs, search_impressions, assessments, errors}
    """
    from naukri_server.tools.inbox import _fetch_inbox
    from naukri_server.tools.notifications import _fetch_notifications
    from naukri_server.tools.search import naukri_get_recommendations
    from naukri_server.tools.performance import _get_recruiter_activity, _get_activity_level, _get_search_impressions
    from naukri_server.tools.tracking import _list_applications, _get_stale_applications, _list_saved_jobs
    from naukri_server.tools.profile import naukri_get_dashboard
    from naukri_server.tools.early_access import _list_early_access_roles
    from naukri_server.tools.subscription import naukri_get_subscription_status
    from naukri_server.tools.reminders import _list_reminders
    from naukri_server.tools.alerts import _get_alerts_list
    from naukri_server.tools.assessments import _get_profile_completeness, _list_assessments

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    errors = []

    results = await asyncio.gather(
        _fetch_inbox(limit=5, unread_only=True),          # 0
        _fetch_notifications(limit=5),                     # 1
        naukri_get_recommendations(limit=5),               # 2
        _get_recruiter_activity(size=5),                   # 3
        _get_activity_level(),                             # 4
        _list_applications(date_from=today),               # 5
        naukri_get_dashboard(),                            # 6
        _list_early_access_roles(limit=3),                 # 7
        naukri_get_subscription_status(),                  # 8
        _list_reminders(include_past=True),                # 9
        _get_stale_applications(days_threshold=14, min_stale_score=50),  # 10
        _get_alerts_list(),                                # 11
        _get_profile_completeness(),                       # 12
        _list_saved_jobs(limit=1),                         # 13
        _get_search_impressions(days=7),                   # 14
        _list_assessments(),                               # 15
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
    reminders_result = _extract(9, "Reminders")
    stale = _extract(10, "Stale detection")
    alerts = _extract(11, "Job alerts")
    completeness = _extract(12, "Profile completeness")
    saved = _extract(13, "Saved jobs")
    impressions = _extract(14, "Search impressions")
    assessments_result = _extract(15, "Assessments")

    # Count pending assessments (those without a completed status)
    pending_count = 0
    if assessments_result:
        for a in assessments_result.get("assessments", []):
            status_val = (a.get("status") or "").lower()
            if status_val not in ("passed", "completed", "failed"):
                pending_count += 1

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
        "due_reminders": {
            "count": reminders_result.get("due_count", 0) if reminders_result else 0,
            "reminders": [r for r in (reminders_result.get("reminders") or []) if r.get("is_due")][:5] if reminders_result else [],
        },
        "stale_applications": {
            "count": stale.get("stale_count", 0) if stale else 0,
            "top_stale": stale.get("stale_applications", [])[:3] if stale else [],
        },
        "job_alerts": {
            "triggered_count": len(alerts.get("alerts", [])) if alerts else 0,
            "alerts": alerts.get("alerts", [])[:3] if alerts else [],
        },
        "profile_completeness": completeness if completeness else None,
        "saved_jobs": {
            "total": saved.get("total", 0) if saved else 0,
        },
        "search_impressions": impressions if impressions else None,
        "assessments": {
            "total": len(assessments_result.get("assessments", [])) if assessments_result else 0,
            "pending": pending_count,
        },
    }

    if errors:
        brief["status"] = "partial_success"
        brief["errors"] = errors

    return brief
