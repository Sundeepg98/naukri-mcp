"""Daily brief — morning dashboard combining notifications, inbox, recommendations, activity, and dashboard stats.

Section builders moved to services/daily_brief_service.py for testability.
The tool itself stays a thin orchestrator (parallel fetch + result assembly).
"""

import asyncio
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

from naukri_server import mcp
from naukri_server import policy as _policy
from naukri_server.config import logger, STALE_MIN_SCORE, STALE_THRESHOLD_DAYS
from naukri_server.scoring import compute_fit_score, parse_skills
# Section builders live in the service — re-exported under their legacy
# underscored aliases so any caller that imports them by name keeps working.
from naukri_server.services.daily_brief_service import (
    build_early_access_section as _build_early_access_section,
    build_competition_section as _build_competition_section,
    build_recommended_actions as _build_recommended_actions,
)


async def _fetch_ab_applied_insights() -> dict:
    """Fetch AmbitionBox salary insights for recently applied jobs. Soft-fails."""
    try:
        from naukri_server.tools.ambitionbox import ab_get_applied_jobs_insights
        return await ab_get_applied_jobs_insights()
    except Exception:
        return {"status": "success", "count": 0, "insights": []}


async def _fetch_pending_notifications() -> list:
    """Fetch undelivered notifications for the daily brief."""
    try:
        from naukri_server.database import list_undelivered_notifications, mark_notifications_delivered
        notifs = await list_undelivered_notifications(limit=10)
        if notifs:
            ids = [n["id"] for n in notifs]
            await mark_notifications_delivered(ids, via="brief")
        return notifs
    except Exception as e:
        logger.warning("Failed to fetch notifications: %s", e)
        return []




@mcp.tool()
async def naukri_daily_brief() -> dict:
    """Get your morning job-hunting dashboard in a single call.

    Runs 20 checks in parallel plus a post-gather conversion funnel analysis:
    unread messages, notifications, new recommendations, recruiter activity,
    profile activity level, today's applications, dashboard stats, early access
    roles, subscription status, due reminders, stale applications, job alerts,
    profile completeness, saved jobs count, search impressions, assessment status,
    match quality, unified notify summary, AmbitionBox salary insights, and
    pending workflow notifications.

    Returns:
        - {status: "success", unread_messages, notifications, notification_summary,
           recommendations (with clusters, agent_eligible), recruiter_activity,
           activity_level, todays_applications, dashboard, due_reminders,
           stale_applications, job_alerts, profile_completeness, saved_jobs,
           search_impressions, assessments, match_quality,
           competition_overview (total_with_data, low, medium, high, very_high,
           average_applicants, top_competitive),
           conversion_funnel (total_applied, conversion_rate, dead_zones),
           pending_notifications, recommended_actions, errors}
    """
    from naukri_server.tools.inbox import _fetch_inbox
    from naukri_server.tools.notifications import _fetch_notifications, _get_unified_notify
    from naukri_server.tools.search import naukri_get_recommendations
    from naukri_server.tools.performance import _get_recruiter_activity, _get_activity_level, _get_search_impressions
    from naukri_server.tools.tracking import _list_applications, _get_stale_applications, _list_saved_jobs
    from naukri_server.tools.profile import get_cached_dashboard
    from naukri_server.tools.early_access import _list_early_access_roles, _detect_new_roles
    from naukri_server.tools.subscription import _get_subscription_status
    from naukri_server.tools.reminders import _list_reminders
    from naukri_server.tools.alerts import _get_alerts_list
    from naukri_server.tools.assessments import _get_profile_completeness, _list_assessments
    from naukri_server.tools.insights import _match_quality

    today = datetime.now(IST).strftime("%Y-%m-%d")
    errors = []

    # Section sizes and the staleness thresholds come from the config
    # file when he has set them; every default here is today's literal,
    # so no file and a malformed file both behave exactly as before.
    section = _policy.setting

    results = await asyncio.gather(
        _fetch_inbox(limit=section("daily_brief.sections.inbox", 5),
                     unread_only=True),                    # 0
        _fetch_notifications(
            limit=section("daily_brief.sections.notifications", 5)),  # 1
        naukri_get_recommendations(
            limit=section("daily_brief.sections.recommendations", 5)),  # 2
        _get_recruiter_activity(
            size=section("daily_brief.sections.recruiter", 5)),      # 3
        _get_activity_level(),                             # 4
        _list_applications(date_from=today),               # 5
        get_cached_dashboard(),                             # 6
        _list_early_access_roles(
            limit=section("daily_brief.sections.early_access", 3)),   # 7
        _get_subscription_status(),                        # 8
        _list_reminders(include_past=True),                # 9
        _get_stale_applications(
            days_threshold=section("staleness.days", STALE_THRESHOLD_DAYS),
            min_stale_score=section("staleness.min_stale_score",
                                    STALE_MIN_SCORE)),     # 10
        _get_alerts_list(),                                # 11
        _get_profile_completeness(),                       # 12
        _list_saved_jobs(
            limit=section("daily_brief.sections.saved", 1)),          # 13
        _get_search_impressions(days=7),                   # 14
        _list_assessments(),                               # 15
        _match_quality(days=7),                              # 16
        _get_unified_notify(),                             # 17
        _fetch_ab_applied_insights(),                      # 18
        _fetch_pending_notifications(),                    # 19
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
    match_quality = _extract(16, "Match quality")
    notify_summary = _extract(17, "Unified notify")
    ab_insights = _extract(18, "AB applied insights")
    pending_notifs_raw = results[19]
    if isinstance(pending_notifs_raw, Exception):
        errors.append(f"Pending notifications: {type(pending_notifs_raw).__name__}: {pending_notifs_raw}")
        pending_notifs = []
    else:
        pending_notifs = pending_notifs_raw if isinstance(pending_notifs_raw, list) else []

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
        "notification_summary": {
            "categories": notify_summary.get("categories", {}) if notify_summary else {},
            "total_types": notify_summary.get("total_types", 0) if notify_summary else 0,
        },
        "recommendations": {
            "count": recs.get("count", 0) if recs else 0,
            "jobs": recs.get("jobs", []) if recs else [],
            "clusters": recs.get("clusters", {}) if recs else {},
            "agent_eligible": recs.get("agent_eligible_exists", False) if recs else False,
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
        "early_access_roles": _build_early_access_section(early_access, errors),
        "subscription": subscription if subscription else None,
        "due_reminders": {
            "count": reminders_result.get("due_count", 0) if reminders_result else 0,
            "reminders": [r for r in (reminders_result.get("reminders") or []) if r.get("is_due")][:5] if reminders_result else [],
        },
        "stale_applications": {
            "count": stale.get("total", 0) if stale else 0,
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
        "match_quality": match_quality if match_quality else None,
        "competition_overview": _build_competition_section(apps),
        "applied_salary_insights": ab_insights if ab_insights else None,
        "pending_notifications": pending_notifs,
    }

    # Conversion funnel summary (loads applications.json which is already loaded, so run after gather)
    try:
        from naukri_server.tools.insights import _conversion_funnel
        funnel = await _conversion_funnel(days=30)
        if isinstance(funnel, dict) and funnel.get("status") == "success":
            brief["conversion_funnel"] = {
                "total_applied": funnel.get("total_applied", 0),
                "conversion_rate": funnel.get("conversion_rate", 0),
                "dead_zones": funnel.get("dead_zones", [])[:3],
            }
    except Exception:
        pass

    # Enrich top recommendations with fit scores
    if recs and dashboard:
        try:
            from naukri_server.tools.profile import get_cached_profile
            profile_data = await get_cached_profile()
            if profile_data:
                profile_skills = parse_skills(profile_data.get("key_skills", []))
                for job in brief["recommendations"]["jobs"][:3]:
                    try:
                        job_skills = parse_skills(job.get("tags", []))
                        fit = compute_fit_score(
                            job_skills, profile_skills,
                            job.get("experience", ""),
                            profile_data.get("total_experience"),
                        )
                        job["fit_score"] = fit.get("overall_score")
                    except Exception:
                        pass
        except Exception as exc:
            errors.append(f"Fit scoring: {type(exc).__name__}: {exc}")

    brief["recommended_actions"] = _build_recommended_actions(brief)

    if errors:
        brief["status"] = "partial_success"
        brief["errors"] = errors

    return brief
