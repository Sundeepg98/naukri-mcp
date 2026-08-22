"""Daily brief service — pure section builders for the morning dashboard.

Each builder takes already-loaded data (no I/O, no module state) and returns
the section dict that the daily-brief tool plugs into its result.

Kept here (not in tools/daily_brief.py) so the section logic can be tested
without standing up the full 20-tool gather().

DDD note: every dict read in these helpers is on internal-trusted state
(brief sections built by other helpers in this codebase). Anti-corruption
logging via safe_get would be noise here — those keys are owned by us.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "build_early_access_section",
    "build_competition_section",
    "build_recommended_actions",
]


def build_early_access_section(early_access: dict | None, errors: list) -> dict:
    """Build the early_access_roles section with new-role delta tracking.

    Mutates ``errors`` in place if delta detection raises (the rest of the
    brief continues; this is best-effort enrichment).
    """
    # Lazy import — avoids a circular dependency at module-load time.
    from naukri_server.tools.early_access import _detect_new_roles

    section = {
        "count": early_access.get("count", 0) if early_access else 0,
        "roles": early_access.get("roles", []) if early_access else [],
        "newly_posted_count": 0,
        "new_roles": [],
    }
    if early_access and early_access.get("roles"):
        try:
            new_roles, _total = _detect_new_roles(early_access["roles"])
            section["newly_posted_count"] = len(new_roles)
            section["new_roles"] = new_roles[:5]  # Cap at 5 for brief
        except Exception as exc:
            errors.append(f"Early access tracking: {type(exc).__name__}: {exc}")
    return section


def build_competition_section(apps_result: dict | None) -> dict:
    """Bucket tracked applications by applicant count into competition levels.

    Reads ``total_applicants`` from each application entry (populated by prior
    detail/sync calls). Returns a summary dict with counts per bucket and the
    top-3 most competitive jobs.

    Args:
        apps_result: The raw result from _list_applications (or None/error).

    Returns:
        ``{total_with_data, low, medium, high, very_high, average_applicants,
        top_competitive}``
    """
    section: dict[str, Any] = {
        "total_with_data": 0,
        "low": 0,       # 0-50 applicants
        "medium": 0,    # 51-200 applicants
        "high": 0,      # 201-500 applicants
        "very_high": 0, # 501+ applicants
        "average_applicants": None,
        "top_competitive": [],
    }

    if not apps_result:
        return section

    applications = apps_result.get("applications", [])
    if not applications:
        return section

    applicant_counts = []
    competitive_entries = []

    for app in applications:
        raw = app.get("total_applicants")
        if raw is None:
            continue
        try:
            count = int(raw)
        except (ValueError, TypeError):
            continue

        applicant_counts.append(count)
        competitive_entries.append({
            "job_id": app.get("job_id"),
            "title": app.get("title"),
            "company": app.get("company"),
            "total_applicants": count,
        })

        if count <= 50:
            section["low"] += 1
        elif count <= 200:
            section["medium"] += 1
        elif count <= 500:
            section["high"] += 1
        else:
            section["very_high"] += 1

    section["total_with_data"] = len(applicant_counts)

    if applicant_counts:
        section["average_applicants"] = round(sum(applicant_counts) / len(applicant_counts), 1)
        competitive_entries.sort(key=lambda x: x["total_applicants"], reverse=True)
        section["top_competitive"] = competitive_entries[:3]

    return section


def build_recommended_actions(brief: dict) -> list:
    """Synthesize prioritized action recommendations from brief data.

    Verbatim port of the previous tools/daily_brief._build_recommended_actions —
    same checks, same priority order, same output shape.
    """
    actions: list[dict] = []

    # High priority: pending workflow notifications
    pending_notifs = (brief.get("pending_notifications") or [])
    high_priority_notifs = [n for n in pending_notifs if n.get("priority") == "high"]
    if high_priority_notifs:
        actions.append({
            "priority": "high",
            "action": f"Review {len(high_priority_notifs)} high-priority notification(s): {high_priority_notifs[0].get('title', 'notification')}",
            "tool": "naukri_daily_brief() — see pending_notifications",
        })

    # Check for critical probe failures
    try:
        from naukri_server.health import probe_registry
        critical_failures = probe_registry.summary().get("critical_failures", [])
        if critical_failures:
            actions.append({
                "priority": "high",
                "action": f"System health alert: {', '.join(critical_failures[:3])} failing",
                "tool": "naukri_health_check",
            })
    except Exception:
        pass

    # High priority: unread recruiter messages
    inbox = (brief.get("unread_messages") or {})
    if inbox.get("count", 0) > 0:
        actions.append({
            "priority": "high",
            "action": f"Respond to {inbox['count']} unread recruiter message(s)",
            "tool": "naukri_list_inbox(unread_only=True)",
        })

    # High priority: due reminders
    reminders = (brief.get("due_reminders") or {})
    due_count = reminders.get("count", 0)
    if due_count > 0:
        actions.append({
            "priority": "high",
            "action": f"Follow up on {due_count} due reminder(s)",
            "tool": "naukri_list_reminders()",
        })

    # Medium priority: stale applications
    stale = (brief.get("stale_applications") or {})
    stale_count = stale.get("count", 0)
    if stale_count > 0:
        actions.append({
            "priority": "medium",
            "action": f"Review {stale_count} stale application(s)",
            "tool": "naukri_follow_up_priority()",
        })

    # High priority: stale applications with high follow-up priority
    if (brief.get("stale_applications") or {}).get("top_stale"):
        top_stale = brief["stale_applications"]["top_stale"]
        high_priority = [s for s in top_stale if s.get("follow_up_priority", 0) >= 70]
        if high_priority:
            actions.append({
                "priority": "high",
                "action": f"Follow up on {len(high_priority)} high-priority stale application(s) — recruiter showed interest",
                "tool": "naukri_draft_follow_up(job_id='...')",
            })

    # Medium priority: conversion funnel dead zones
    funnel = (brief.get("conversion_funnel") or {})
    dead_zones = funnel.get("dead_zones", [])
    if dead_zones:
        companies = ", ".join(d.get("company", "?") for d in dead_zones[:3])
        actions.append({
            "priority": "medium",
            "action": f"Dead zones detected: {companies} — consider stopping applications there",
            "tool": "naukri_conversion_funnel()",
        })

    # High priority: recruiter search activity (from notification_summary)
    notify = (brief.get("notification_summary") or {})
    recruiter_search = notify.get("categories", {}).get("recruiterSearch", {})
    if recruiter_search.get("count", 0) > 0:
        actions.append({
            "priority": "high",
            "action": f"Recruiters searched for you {recruiter_search['count']} times — keep profile updated",
            "tool": "naukri_search_impressions()",
        })

    # Medium priority: new early access roles
    ea = (brief.get("early_access_roles") or {})
    new_count = ea.get("newly_posted_count", 0)
    if new_count > 0:
        actions.append({
            "priority": "medium",
            "action": f"Check {new_count} new early access role(s)",
            "tool": "naukri_early_access(action='list')",
        })

    # Medium priority: pending assessments
    assessments = (brief.get("assessments") or {})
    pending = assessments.get("pending", 0)
    if pending > 0:
        actions.append({
            "priority": "medium",
            "action": f"Complete {pending} pending assessment(s)",
            "tool": "naukri_get_profile() — check assessments section",
        })

    # Low priority: profile completeness
    profile = brief.get("profile_completeness") or {}
    completeness = profile.get("completeness_percent") or profile.get("completeness", 100)
    if isinstance(completeness, (int, float)) and completeness < 80:
        actions.append({
            "priority": "low",
            "action": f"Improve profile completeness ({completeness}%)",
            "tool": "naukri_audit_profile()",
        })

    # Medium priority: high average competition across recent applications
    competition = (brief.get("competition_overview") or {})
    avg_applicants = competition.get("average_applicants")
    if avg_applicants is not None and avg_applicants > 200:
        avg = int(avg_applicants)
        actions.append({
            "priority": "medium",
            "action": f"Recent applications have high competition (avg {avg} applicants) -- try niche keywords",
            "tool": "naukri_search_jobs(keywords='specific_niche')",
        })

    # Low priority: no recent applications (only when data is present)
    apps = brief.get("todays_applications")
    if apps is not None and apps.get("count", 0) == 0:
        actions.append({
            "priority": "low",
            "action": "Apply to some jobs today — no applications yet",
            "tool": "naukri_apply_top_fits()",
        })

    # Sort by priority
    priority_order = {"high": 0, "medium": 1, "low": 2}
    actions.sort(key=lambda x: priority_order.get(x["priority"], 3))

    return actions
