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
    except Exception as e:
        # NOT a fabricated success. This used to return
        # {"status": "success", "count": 0, "insights": []} on ANY exception --
        # a crash wearing a measurement, and the purest form of the defect the
        # section builders above were also committing.
        logger.warning("AB applied insights failed: %s: %s", type(e).__name__, e)
        return {"status": "error", "message": "%s: %s" % (type(e).__name__, e),
                "error_code": "API_ERROR"}


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
async def naukri_daily_brief(explain: bool = False) -> dict:
    """Get your morning job-hunting dashboard in a single call.

    Runs 20 checks in parallel plus a post-gather conversion funnel analysis:
    unread messages, notifications, new recommendations, recruiter activity,
    profile activity level, today's applications, dashboard stats, early access
    roles, subscription status, due reminders, stale applications, job alerts,
    profile completeness, saved jobs count, search impressions, assessment status,
    match quality, unified notify summary, AmbitionBox salary insights, and
    pending workflow notifications.

    Args:
        explain: attach the arithmetic behind the fit scores on the top
            recommendations -- weights, the skills/experience components before
            bonuses, the bonus cap, the verdict band, and the scoring_hash.
            Off by default, and most firmly so here: this is the one call meant
            to be read whole every morning, and the block is verbose.

    Returns:
        - {status: "success", unread_messages, notifications, notification_summary,
           recommendations (with clusters, agent_eligible; each scored job also
           carries `explain` when explain=True), recruiter_activity,
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

    # A SECTION WHOSE FETCH FAILED IS None, NEVER A ZERO.
    #
    # Every section below used to read `x.get(k, 0) if x else 0`, so a fetch
    # that RAISED produced `{"count": 0}` / `{"profile_views": 0}` -- a hard
    # zero indistinguishable from a real measurement of nothing. Measured
    # 2026-08-22: with the session expired all 14 sub-fetches failed and the
    # brief still reported profile_views 0, recommendations 0, unread 0. The
    # `errors` list named all fourteen, but that is a separate channel most
    # callers never join, and the zeros are what gets read.
    #
    # None cannot be mistaken for a measurement. build_recommended_actions
    # reads every section through `or {}` for exactly this reason.
    def _section(source, build):
        return build(source) if source is not None else None

    brief = {
        "status": "success",
        "date": today,
        "unread_messages": _section(inbox, lambda d: {
            "count": d.get("count", 0),
            "messages": d.get("messages", []),
        }),
        "notifications": _section(notifs, lambda d: {
            "count": d.get("count", 0),
            "items": d.get("notifications", []),
        }),
        "notification_summary": _section(notify_summary, lambda d: {
            "categories": d.get("categories", {}),
            "total_types": d.get("total_types", 0),
        }),
        "recommendations": _section(recs, lambda d: {
            "count": d.get("count", 0),
            "jobs": d.get("jobs", []),
            "clusters": d.get("clusters", {}),
            "agent_eligible": d.get("agent_eligible_exists", False),
        }),
        "recruiter_activity": _section(recruiter, lambda d: {
            "total": d.get("total_actions", 0),
            "change": d.get("percentage_change"),
            "recent": d.get("activities", []),
        }),
        # Stays "UNKNOWN" rather than None: unlike a 0, the string already SAYS
        # it is not a measurement, which is the whole point of this change.
        "activity_level": activity.get("level", "UNKNOWN") if activity else "UNKNOWN",
        "todays_applications": _section(apps, lambda d: {
            "count": d.get("count", 0),
            "applications": d.get("applications", []),
        }),
        "dashboard": _section(dashboard, lambda d: {
            "profile_views": d.get("profile_views", 0),
            "total_matches": d.get("total_matches", 0),
            "unread_invites": d.get("unread_invites", 0),
        }),
        "early_access_roles": _build_early_access_section(early_access, errors),
        "subscription": subscription if subscription else None,
        "due_reminders": _section(reminders_result, lambda d: {
            "count": d.get("due_count", 0),
            "reminders": [r for r in (d.get("reminders") or []) if r.get("is_due")][:5],
        }),
        "stale_applications": _section(stale, lambda d: {
            "count": d.get("total", 0),
            "top_stale": d.get("stale_applications", [])[:3],
        }),
        "job_alerts": _section(alerts, lambda d: {
            "triggered_count": len(d.get("alerts", [])),
            "alerts": d.get("alerts", [])[:3],
        }),
        "profile_completeness": completeness if completeness else None,
        "saved_jobs": _section(saved, lambda d: {"total": d.get("total", 0)}),
        "search_impressions": impressions if impressions else None,
        "assessments": _section(assessments_result, lambda d: {
            "total": len(d.get("assessments", [])),
            "pending": pending_count,
        }),
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
                            explain=explain,
                        )
                        job["fit_score"] = fit.get("overall_score")
                        # Absent entirely when the flag is off -- `fit` has no
                        # such key then, so nothing is added.
                        if "explain" in fit:
                            job["explain"] = fit["explain"]
                    except Exception:
                        pass
        except Exception as exc:
            errors.append(f"Fit scoring: {type(exc).__name__}: {exc}")

    brief["recommended_actions"] = _build_recommended_actions(brief)

    if errors:
        brief["status"] = "partial_success"
        brief["errors"] = errors

    return brief
