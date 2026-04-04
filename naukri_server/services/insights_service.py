"""Insights service — business logic for application analytics, salary analysis,
taxonomy, profile prompts, cached answers, and status change detection.

Extracted from tools/insights.py, tools/analytics.py, and tools/skill_gap.py
as part of Phase 4.7 (service-layer extraction).

NOTE: ``get_taxonomy`` and ``cached_answers`` remain *defined* in
``tools/insights.py`` because test suites patch module-level state
(``_taxonomy_cache``, ``_cache_lock``, ``_load_cache``) on that module.
This service re-exports them via lazy import so callers can use either path.
"""

import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional

from naukri_server.interfaces import api_client
from naukri_server.config import (
    LAKHS_MULTIPLIER, APPLY_MATCH_SCORE_API,
    NAUKRI_BASE, CCS_PAGE_API, BROWSER_DOM_SETTLE,
    MATCH_ANALYTICS_API, logger,
)
from naukri_server.scoring import normalize_skill, parse_skills
from naukri_server.domain.application import StatusTransition

__all__ = [
    "conversion_funnel",
    "application_insights",
    "salary_position",
    "match_quality",
    "detect_status_changes",
    "get_taxonomy",
    "get_profile_prompts",
    "cached_answers",
    "parse_salary_str",
    "get_match_analytics",
    "skill_gap_analysis",
]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversion funnel
# ---------------------------------------------------------------------------

async def conversion_funnel(days: int = 30) -> dict:
    """Analyze the application-to-interview conversion funnel.

    Counts total applications, breaks down by status, identifies which
    companies respond most and which are "dead zones" (3+ applies, 0 responses).

    Args:
        days: Number of days to analyze (default 30)

    Returns:
        {status, days, total_applied, funnel, conversion_rate,
         top_responsive_companies, dead_zones}
    """
    if days < 1:
        return {"status": "error", "message": "days must be >= 1", "error_code": "VALIDATION_ERROR"}
    from naukri_server.database import list_all_applications
    apps = await list_all_applications()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent = [a for a in apps if a.get("applied_at", "") >= cutoff]

    total = len(recent)
    if total == 0:
        return {
            "status": "success",
            "days": days,
            "total_applied": 0,
            "funnel": {},
            "conversion_rate": 0,
            "top_responsive_companies": [],
            "dead_zones": [],
        }

    by_status = Counter(a.get("status", "unknown") for a in recent)

    by_company: dict[str, dict] = {}
    for a in recent:
        co = a.get("company", "Unknown")
        if co not in by_company:
            by_company[co] = {"applied": 0, "responded": 0}
        by_company[co]["applied"] += 1
        if a.get("status") in ("interview", "viewed", "shortlisted", "offered"):
            by_company[co]["responded"] += 1

    responsive = sorted(
        [{"company": k, **v, "rate": round(v["responded"] / v["applied"] * 100)}
         for k, v in by_company.items() if v["applied"] >= 2],
        key=lambda x: -x["rate"]
    )

    interviews = by_status.get("interview", 0)

    return {
        "status": "success",
        "days": days,
        "total_applied": total,
        "funnel": dict(by_status),
        "conversion_rate": round(interviews / total * 100, 1) if total else 0,
        "top_responsive_companies": responsive[:10],
        "dead_zones": [c for c in responsive if c["rate"] == 0 and c["applied"] >= 3],
    }


# ---------------------------------------------------------------------------
# Application insights
# ---------------------------------------------------------------------------

async def application_insights(days: int = 30) -> dict:
    """Analyze application history for patterns and insights."""
    if days < 1:
        return {"status": "error", "message": "days must be >= 1", "error_code": "VALIDATION_ERROR"}
    from naukri_server.database import list_all_applications
    apps = await list_all_applications()

    if not apps:
        return {"status": "error", "message": "No applications tracked yet. Use naukri_apply or naukri_sync(entity=\"applications\") first.", "error_code": "NOT_FOUND"}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent = [a for a in apps if (a.get("applied_at") or "") >= cutoff]

    if not recent:
        return {
            "status": "error",
            "message": f"No applications in the last {days} days. Total tracked: {len(apps)}.",
            "total_all_time": len(apps),
            "error_code": "NOT_FOUND",
        }

    status_counts = Counter(a.get("status", "unknown") for a in recent)

    company_counts = Counter(a.get("company", "Unknown") for a in recent)
    top_companies = [{"company": c, "count": n} for c, n in company_counts.most_common(10)]

    location_counts = Counter()
    for a in recent:
        loc = a.get("location") or (a.get("extra", {}).get("location") if isinstance(a.get("extra"), dict) else None)
        if loc:
            location_counts[loc] += 1
    top_locations = [{"location": l, "count": n} for l, n in location_counts.most_common(10)]

    actual_days = max(days, 1)
    per_day = round(len(recent) / actual_days, 1)
    per_week = round(per_day * 7, 1)

    insights = []
    if len(recent) > 0:
        insights.append(f"{len(recent)} applications in {days} days ({per_day}/day)")
    if status_counts.get("already_applied", 0) > 0:
        dup_pct = round(status_counts["already_applied"] / len(recent) * 100)
        insights.append(f"{dup_pct}% were duplicate applications (already applied)")
    if top_companies and top_companies[0]["count"] > 2:
        insights.append(f"Most-applied company: {top_companies[0]['company']} ({top_companies[0]['count']} times)")
    if status_counts.get("error", 0) > 0:
        err_pct = round(status_counts["error"] / len(recent) * 100)
        insights.append(f"{err_pct}% of applications had errors")

    return {
        "status": "success",
        "period_days": days,
        "total_applications": len(recent),
        "total_all_time": len(apps),
        "status_breakdown": dict(status_counts),
        "velocity": {"per_day": per_day, "per_week": per_week},
        "top_companies": top_companies,
        "top_locations": top_locations if top_locations else None,
        "insights": insights,
    }


# ---------------------------------------------------------------------------
# Salary helpers
# ---------------------------------------------------------------------------

def parse_salary_str(salary_str: str) -> tuple[float | None, float | None]:
    """Parse salary strings like '10-15 Lacs', '₹10L - ₹15L', 'Not disclosed' into (min, max) in LPA."""
    from naukri_server.domain.salary import Salary
    s = Salary.from_string(salary_str)
    return s.min_lakhs, s.max_lakhs


async def salary_position(designation: Optional[str] = None) -> dict:
    """Analyze salary positioning across applied jobs."""
    from naukri_server.database import list_all_applications
    apps = await list_all_applications()

    if not apps:
        return {"status": "error", "message": "No applications tracked yet.", "error_code": "NOT_FOUND"}

    if designation:
        keyword = designation.lower()
        apps = [a for a in apps if keyword in (a.get("title") or "").lower()]

    salaries = []
    for app in apps:
        sal_str = app.get("salary") or app.get("salary_range") or ""
        s_min, s_max = parse_salary_str(sal_str)
        if s_min is not None and s_max is not None:
            midpoint = (s_min + s_max) / 2
            salaries.append({"min": s_min, "max": s_max, "mid": midpoint, "raw": sal_str})

    if not salaries:
        return {
            "status": "error",
            "message": f"No salary data found in {len(apps)} applications. Most jobs may have 'Not disclosed' salary.",
            "error_code": "NOT_FOUND",
        }

    salaries.sort(key=lambda s: s["mid"])
    mids = [s["mid"] for s in salaries]
    median = mids[len(mids) // 2]

    buckets = Counter()
    for s in salaries:
        bucket_start = int(s["mid"] // 5) * 5
        bucket_label = f"{bucket_start}-{bucket_start + 5} LPA"
        buckets[bucket_label] += 1

    distribution = [{"range": r, "count": c} for r, c in sorted(buckets.items(), key=lambda x: x[0])]

    insights = []
    all_mins = [s["min"] for s in salaries]
    all_maxs = [s["max"] for s in salaries]
    insights.append(f"Salary range across applications: {min(all_mins)}-{max(all_maxs)} LPA")
    insights.append(f"Median target: {median} LPA")
    if distribution:
        top_bucket = max(distribution, key=lambda d: d["count"])
        insights.append(f"Most applications target {top_bucket['range']} ({top_bucket['count']} jobs)")

    return {
        "status": "success",
        "total_with_salary": len(salaries),
        "total_applications": len(apps),
        "salary_range": {
            "min": min(all_mins),
            "max": max(all_maxs),
            "median": median,
        },
        "distribution": distribution,
        "insights": insights,
    }


# ---------------------------------------------------------------------------
# Match quality
# ---------------------------------------------------------------------------

async def match_quality(days: int = 7) -> dict:
    """Aggregate apply-match quality — how well recent applications matched your profile.

    Args:
        days: Number of days to analyze (default 7)

    Returns:
        - {status: "success", days, total_applies, complete_match, high_match, medium_match,
           low_match, field_breakdown}
        - {status: "error", message}
    """
    if days < 1:
        return {"status": "error", "message": "days must be >= 1", "error_code": "VALIDATION_ERROR"}
    data = await api_client.get(f"{APPLY_MATCH_SCORE_API}?days={days}")

    field_breakdown = data.get("relevantFieldMatch", {})
    formatted_fields = {}
    for field_name, field_data in field_breakdown.items():
        if isinstance(field_data, dict):
            formatted_fields[field_name] = {
                "count": field_data.get("count"),
                "percent": field_data.get("percent"),
            }
        else:
            formatted_fields[field_name] = field_data

    return {
        "status": "success",
        "days": days,
        "total_applies": data.get("totalApplies"),
        "complete_match": data.get("completeMatch"),
        "high_match": data.get("highMatch"),
        "medium_match": data.get("mediumMatch"),
        "low_match": data.get("lowMatch"),
        "field_breakdown": formatted_fields,
    }


# ---------------------------------------------------------------------------
# Browser-based: profile prompts
# ---------------------------------------------------------------------------

def _get_browser():
    """Lazy import to avoid circular imports and enable test patching."""
    from naukri_server.browser import browser
    return browser


async def get_profile_prompts() -> dict:
    """Fetch CCS widget state keys by intercepting Naukri's dashboard CCS call.

    Navigates to the homepage and captures the CCS response that Naukri's
    own JavaScript makes (with proper cookies/headers). This avoids the
    issue of page.evaluate(fetch) not having the right auth context.
    """
    from naukri_server.browser import page_goto
    ccs_pattern = CCS_PAGE_API
    captured = {}

    async def _on_response(response):
        if ccs_pattern in response.url and response.status == 200:
            try:
                captured["data"] = await response.json()
            except Exception:
                pass

    async with _get_browser().page_pool.acquire() as page:
        page.on("response", _on_response)
        try:
            await page_goto(page, f"{NAUKRI_BASE}/mnjuser/homepage")
            for _ in range(10):
                if captured.get("data"):
                    break
                await asyncio.sleep(BROWSER_DOM_SETTLE)
        finally:
            page.remove_listener("response", _on_response)

    data = captured.get("data", {})

    if not data or data.get("error"):
        return {
            "status": "error",
            "message": f"CCS fetch failed: {data.get('error', 'empty response') if data else 'empty response'}",
            "error_code": "BROWSER_ERROR",
        }

    states = data.get("states", {})

    PROMPT_MAP = {
        "t2536_click_add_salary_breakup": {
            "field": "salary_breakup",
            "action": "Add detailed salary breakup",
            "impact": "high",
            "reason": "Recruiters filter heavily by CTC range",
        },
        "t2906_click_add_locality": {
            "field": "preferred_locations",
            "action": "Add preferred work locations",
            "impact": "high",
            "reason": "Location-filtered searches will miss you",
        },
        "t2772_click_complete": {
            "field": "profile_completion",
            "action": "Complete remaining profile sections",
            "impact": "medium",
            "reason": "Higher completeness = better ranking",
        },
        "t4170_click_submit": {
            "field": "profile_data",
            "action": "Submit pending profile updates",
            "impact": "medium",
            "reason": "Unsaved changes aren't visible to recruiters",
        },
        "t2683_view": {
            "field": "iti_trades",
            "action": "Add ITI/trades data if applicable",
            "impact": "low",
            "reason": "Relevant for apprenticeship/trades roles only",
        },
    }

    pending = []
    completed = []
    for key, value in states.items():
        prompt_info = PROMPT_MAP.get(key)
        if prompt_info:
            if value == 0:
                pending.append(prompt_info)
            else:
                completed.append({"field": prompt_info["field"], "status": "done"})

    impact_order = {"high": 0, "medium": 1, "low": 2}
    pending.sort(key=lambda p: impact_order.get(p.get("impact", "low"), 3))

    return {
        "status": "success",
        "source": "ccs_widget",
        "pending_count": len(pending),
        "completed_count": len(completed),
        "pending_prompts": pending,
        "completed_prompts": completed,
        "all_state_keys": states,
        "cache_ttl_seconds": data.get("ttl"),
        "widget_sections_count": len(data.get("sections", [])),
    }


# ---------------------------------------------------------------------------
# Status change detection
# ---------------------------------------------------------------------------

async def detect_status_changes(days_back: int = 30) -> dict:
    """Detect application status changes by syncing with Naukri and comparing.

    Runs a quick sync to get latest statuses, then categorizes any detected
    transitions as positive (forward movement) or neutral.

    Args:
        days_back: Number of days to sync (default 30)

    Returns:
        {status, total_changes, positive_changes, positive, neutral, sync_method, last_sync}
    """
    from naukri_server.tools.sync import _sync_applications

    sync_result = await _sync_applications(days_back=days_back)

    if sync_result.get("status") == "error":
        return sync_result

    changes = sync_result.get("status_changes", [])

    positive = []
    neutral = []

    for change in changes:
        old = (change.get("old_status") or "").lower()
        new = (change.get("new_status") or "").lower()
        transition = StatusTransition(old_status=old, new_status=new)
        change["transition_type"] = transition.transition_type
        if transition.is_positive:
            positive.append(change)
        else:
            neutral.append(change)

    return {
        "status": "success",
        "total_changes": len(changes),
        "positive_changes": len(positive),
        "positive": positive,
        "neutral": neutral,
        "sync_method": sync_result.get("method"),
        "last_sync": sync_result.get("last_sync"),
    }


# ---------------------------------------------------------------------------
# Taxonomy & cached answers — delegated to tools/insights.py
# (Tests patch module-level state on tools/insights, so the definitions
#  must stay there. We re-export here for unified service access.)
# ---------------------------------------------------------------------------

def get_taxonomy():
    """Re-export: delegates to tools/insights._get_taxonomy (lazy import)."""
    from naukri_server.tools.insights import _get_taxonomy
    return _get_taxonomy()


def cached_answers(action="list", key=None, new_answer=None):
    """Re-export: delegates to tools/insights._cached_answers (lazy import)."""
    from naukri_server.tools.insights import _cached_answers
    return _cached_answers(action=action, key=key, new_answer=new_answer)


# ---------------------------------------------------------------------------
# Match analytics (from analytics.py)
# ---------------------------------------------------------------------------

async def get_match_analytics(days: int = 7) -> dict:
    """Get match-score analytics for recent job applications — overall match distribution and per-field breakdowns.

    Args:
        days: Number of days of application history to analyze (default 7)

    Returns:
        - {status: "success", days, total_applies, complete_match, high_match, medium_match,
           low_match, field_breakdown, user_details}
        - {status: "error", message}
    """
    _logger.info("Fetching match analytics for last %d days", days)
    if days < 1:
        return {"status": "error", "message": "days must be >= 1", "error_code": "VALIDATION_ERROR"}
    data = await api_client.get(MATCH_ANALYTICS_API, params={"days": str(days)})

    return {
        "status": "success",
        "days": days,
        "total_applies": data.get("totalApplies"),
        "complete_match": data.get("completeMatch"),
        "high_match": data.get("highMatch"),
        "medium_match": data.get("mediumMatch"),
        "low_match": data.get("lowMatch"),
        "field_breakdown": data.get("relevantFieldMatch"),
        "user_details": data.get("userDetails"),
    }


# ---------------------------------------------------------------------------
# Skill gap analysis (from skill_gap.py)
# ---------------------------------------------------------------------------

async def skill_gap_analysis(
    keywords: Optional[str] = None,
    use_recommendations: bool = True,
    sample_size: int = 20,
    include_assessments: bool = True,
    timeout_seconds: int = 120,
) -> dict:
    """Analyze skill gaps across multiple jobs to find your most common missing skills.

    Fetches N jobs (via recommendations or search) and your profile in parallel,
    then computes which skills you're missing most often. Helps prioritize upskilling.
    When include_assessments is True, passed Naukri skill assessments boost the
    frequency of matched skills by 2x (they appear more valuable to recruiters).

    Args:
        keywords: Search keywords (required if use_recommendations is False)
        use_recommendations: If True, use personalized recommendations; if False, use search (default True)
        sample_size: Number of jobs to analyze (default 20, max 50)
        include_assessments: If True, fetch assessments and boost passed-skill frequency by 2x (default True)
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        - {status: "success", jobs_analyzed, skill_gaps, [{skill, frequency, percentage,
           sample_jobs}], strong_skills: [{skill, frequency, percentage, assessment_passed?}],
           assessments_used: int}
        - {status: "error", message}
    """
    logger.info("Skill gap analysis: keywords=%s, use_recommendations=%s, sample_size=%d", keywords, use_recommendations, sample_size)
    if not use_recommendations and not keywords:
        return {"status": "error", "message": "keywords is required when use_recommendations is False.", "error_code": "VALIDATION_ERROR"}

    sample_size = min(sample_size, 50)

    async def _do_work() -> dict:
        from naukri_server.tools.search import naukri_search_jobs, naukri_get_recommendations
        from naukri_server.tools.profile import get_cached_profile
        from naukri_server.tools.assessments import _list_assessments

        if use_recommendations:
            jobs_coro = naukri_get_recommendations(limit=sample_size)
        else:
            jobs_coro = naukri_search_jobs(keywords=keywords, limit=sample_size)

        coros = [jobs_coro, get_cached_profile()]
        if include_assessments:
            coros.append(_list_assessments())

        gather_results = await asyncio.gather(*coros, return_exceptions=True)

        jobs_result = gather_results[0]
        profile_result = gather_results[1]
        assessment_result = gather_results[2] if include_assessments else None

        if isinstance(jobs_result, Exception) or jobs_result.get("status") == "error":
            msg = str(jobs_result) if isinstance(jobs_result, Exception) else jobs_result.get("message")
            logger.error("Skill gap analysis failed to fetch jobs: %s", msg)
            return {"status": "error", "message": f"Failed to fetch jobs: {msg}", "error_code": "API_ERROR"}

        if isinstance(profile_result, Exception) or profile_result.get("status") == "error":
            msg = str(profile_result) if isinstance(profile_result, Exception) else profile_result.get("message")
            logger.error("Skill gap analysis failed to fetch profile: %s", msg)
            return {"status": "error", "message": f"Failed to fetch profile: {msg}", "error_code": "API_ERROR"}

        jobs = jobs_result.get("jobs", [])
        if not jobs:
            return {"status": "error", "message": "No jobs found to analyze.", "error_code": "NOT_FOUND"}

        profile_skills = parse_skills(profile_result.get("key_skills", []))

        exp_map = {}
        for s in profile_result.get("skills_with_experience", []):
            if isinstance(s, dict):
                name = normalize_skill(s.get("skill", ""))
                years = s.get("experience_years", 0) or 0
                months = s.get("experience_months", 0) or 0
                exp_map[name] = years + months / 12

        passed_skills: set[str] = set()
        if include_assessments and assessment_result is not None:
            if not isinstance(assessment_result, Exception):
                if isinstance(assessment_result, dict) and assessment_result.get("status") == "success":
                    for assessment in assessment_result.get("assessments", []):
                        status = (assessment.get("status") or "").lower()
                        if status in ("passed", "completed"):
                            skill_name = (assessment.get("skill") or "").strip()
                            if skill_name:
                                passed_skills.add(normalize_skill(skill_name))
                else:
                    logger.warning("Assessments fetch failed (non-fatal): %s", assessment_result)
            else:
                logger.warning("Assessments fetch raised (non-fatal): %s", assessment_result)

        missing_counter = Counter()
        matched_counter = Counter()
        missing_jobs = {}

        for job in jobs:
            job_skills = parse_skills(job.get("tags") or job.get("skills") or [])
            if not job_skills:
                continue

            matched = job_skills & profile_skills
            missing = job_skills - profile_skills
            job_label = f"{job.get('title', '?')} @ {job.get('company', '?')}"

            for skill in missing:
                missing_counter[skill] += 1
                if skill not in missing_jobs:
                    missing_jobs[skill] = []
                if len(missing_jobs[skill]) < 5:
                    missing_jobs[skill].append(job_label)

            for skill in matched:
                matched_counter[skill] += 1

        jobs_analyzed = len(jobs)

        if passed_skills:
            for skill in list(matched_counter):
                if normalize_skill(skill) in passed_skills:
                    matched_counter[skill] *= 2

        skill_gaps = []
        for skill, freq in missing_counter.most_common(30):
            skill_gaps.append({
                "skill": skill,
                "frequency": freq,
                "percentage": round(freq / jobs_analyzed * 100),
                "sample_jobs": missing_jobs.get(skill, []),
            })

        strong_skills = []
        for skill, freq in matched_counter.most_common(20):
            entry = {
                "skill": skill,
                "frequency": freq,
                "percentage": round(freq / jobs_analyzed * 100),
            }
            if normalize_skill(skill) in passed_skills:
                entry["assessment_passed"] = True
            strong_skills.append(entry)

        for skill_entry in strong_skills:
            skill_name = normalize_skill(skill_entry["skill"])
            skill_entry["your_experience_years"] = round(exp_map.get(skill_name, 0), 1)

        return {
            "status": "success",
            "jobs_analyzed": jobs_analyzed,
            "assessments_used": len(passed_skills),
            "skill_gaps": skill_gaps,
            "strong_skills": strong_skills,
        }

    try:
        return await asyncio.wait_for(_do_work(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return {"status": "partial_success", "message": f"Timed out after {timeout_seconds}s", "error_code": "TIMEOUT"}
