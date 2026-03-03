"""Intelligence layer — application insights, cached answer review, salary analysis."""

import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get
from naukri_server.cache import _load_cache, _cache_lock
from naukri_server.config import LAKHS_MULTIPLIER, APPLY_MATCH_SCORE_API, ENTITY_TAXONOMY_API
from naukri_server.tools.tracking import _load_json, _applications_lock, APPLICATIONS_FILE
from naukri_server.utils import TtlCache


_taxonomy_cache = TtlCache(86400)  # 24 hours — taxonomy data is static


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _get_taxonomy() -> dict:
    """Fetch Naukri's job taxonomy: 37 departments -> 167 role categories -> 1461 roles."""
    cached = _taxonomy_cache.get("taxonomy")
    if cached:
        return cached

    data = await api_get(ENTITY_TAXONOMY_API)
    entities = data if isinstance(data, list) else data.get("data", data.get("entities", []))

    departments = []
    total_roles = 0
    for dept in (entities if isinstance(entities, list) else []):
        dept_entry = {
            "id": dept.get("id"),
            "label": dept.get("label"),
            "synonyms": dept.get("synonyms", []),
            "role_categories": [],
        }
        for cat in dept.get("child", []):
            cat_entry = {
                "id": cat.get("id"),
                "label": cat.get("label"),
                "roles": [],
            }
            for role in cat.get("child", []):
                cat_entry["roles"].append({
                    "id": role.get("id"),
                    "label": role.get("label"),
                    "synonyms": role.get("synonyms", []),
                })
                total_roles += 1
            dept_entry["role_categories"].append(cat_entry)
        departments.append(dept_entry)

    result = {
        "status": "success",
        "total_departments": len(departments),
        "total_roles": total_roles,
        "departments": departments,
    }
    _taxonomy_cache.set("taxonomy", result)
    return result


async def _application_insights(days: int = 30) -> dict:
    """Analyze application history for patterns and insights."""
    if days < 1:
        return {"status": "error", "message": "days must be >= 1", "error_code": "VALIDATION_ERROR"}
    async with _applications_lock:
        apps = _load_json(APPLICATIONS_FILE)

    if not apps:
        return {"status": "error", "message": "No applications tracked yet. Use naukri_apply or naukri_sync(entity=\"applications\") first.", "error_code": "NOT_FOUND"}

    # Filter by date range
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent = [a for a in apps if (a.get("applied_at") or "") >= cutoff]

    if not recent:
        return {
            "status": "error",
            "message": f"No applications in the last {days} days. Total tracked: {len(apps)}.",
            "total_all_time": len(apps),
            "error_code": "NOT_FOUND",
        }

    # Status breakdown
    status_counts = Counter(a.get("status", "unknown") for a in recent)

    # Company distribution
    company_counts = Counter(a.get("company", "Unknown") for a in recent)
    top_companies = [{"company": c, "count": n} for c, n in company_counts.most_common(10)]

    # Location distribution
    location_counts = Counter()
    for a in recent:
        loc = a.get("location") or (a.get("extra", {}).get("location") if isinstance(a.get("extra"), dict) else None)
        if loc:
            location_counts[loc] += 1
    top_locations = [{"location": l, "count": n} for l, n in location_counts.most_common(10)]

    # Velocity
    actual_days = max(days, 1)
    per_day = round(len(recent) / actual_days, 1)
    per_week = round(per_day * 7, 1)

    # Generate insights
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


async def _cached_answers(action: str = "list", key: Optional[str] = None, new_answer: Optional[str] = None) -> dict:
    """Manage cached screening question answers — list, update, or delete."""
    if action == "list":
        async with _cache_lock:
            cache = _load_cache()

        if not cache:
            return {"status": "error", "message": "No cached answers yet. Apply to jobs to build the cache.", "error_code": "NOT_FOUND"}

        answers = []
        for k, entry in cache.items():
            # Extract question name from key format: "question_name_{options_json}"
            question_name = k
            for sep in ("_{", "_[", "_("):
                idx = k.find(sep)
                if idx > 0:
                    question_name = k[:idx]
                    break

            if isinstance(entry, dict):
                answers.append({
                    "key": k,
                    "question": question_name,
                    "answer": entry.get("answer"),
                    "type": entry.get("questionType"),
                    "cached_at": entry.get("cached_at"),
                })
            else:
                # Legacy format: simple value
                answers.append({
                    "key": k,
                    "question": question_name,
                    "answer": entry,
                    "type": "unknown",
                    "cached_at": None,
                })

        # Sort by cached_at (newest first), None last
        answers.sort(key=lambda a: a.get("cached_at") or 0, reverse=True)

        return {
            "status": "success",
            "total_cached": len(answers),
            "answers": answers,
        }

    elif action == "update":
        if not key or new_answer is None:
            return {"status": "error", "message": "update requires key and new_answer.", "error_code": "VALIDATION_ERROR"}
        from naukri_server.cache import update_cached_answer
        updated = await update_cached_answer(key, new_answer)
        if updated:
            return {"status": "success", "key": key, "new_answer": new_answer, "message": f"Answer updated."}
        return {"status": "error", "message": f"Key '{key}' not found in cache.", "error_code": "NOT_FOUND"}

    elif action == "delete":
        if not key:
            return {"status": "error", "message": "delete requires key.", "error_code": "VALIDATION_ERROR"}
        from naukri_server.cache import delete_cached_answer
        deleted = await delete_cached_answer(key)
        if deleted:
            return {"status": "success", "key": key, "message": f"Cached answer '{key}' deleted."}
        return {"status": "error", "message": f"Key '{key}' not found in cache.", "error_code": "NOT_FOUND"}

    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, update, delete", "error_code": "VALIDATION_ERROR"}


def _parse_salary_str(salary_str: str) -> tuple[float | None, float | None]:
    """Parse salary strings like '10-15 Lacs', '₹10L - ₹15L', 'Not disclosed' into (min, max) in LPA."""
    if not salary_str or not isinstance(salary_str, str):
        return None, None
    s = salary_str.lower().strip()
    if "not disclosed" in s or "confidential" in s:
        return None, None

    # Match patterns like "10-15", "10.5 - 15.5", "₹10L - ₹15L"
    nums = re.findall(r'(\d+(?:\.\d+)?)', s)
    if not nums:
        return None, None

    vals = [float(n) for n in nums[:2]]

    # If values are in thousands (e.g., 1000000), convert to LPA
    factor = 1
    if any(v > 200 for v in vals):
        factor = 1 / LAKHS_MULTIPLIER  # Convert to lakhs

    if len(vals) == 2:
        return round(vals[0] * factor, 1), round(vals[1] * factor, 1)
    elif len(vals) == 1:
        return round(vals[0] * factor, 1), round(vals[0] * factor, 1)
    return None, None


async def _salary_position(designation: Optional[str] = None) -> dict:
    """Analyze salary positioning across applied jobs."""
    async with _applications_lock:
        apps = _load_json(APPLICATIONS_FILE)

    if not apps:
        return {"status": "error", "message": "No applications tracked yet.", "error_code": "NOT_FOUND"}

    if designation:
        keyword = designation.lower()
        apps = [a for a in apps if keyword in (a.get("title") or "").lower()]

    # Extract salary midpoints
    salaries = []
    for app in apps:
        sal_str = app.get("salary") or app.get("salary_range") or ""
        s_min, s_max = _parse_salary_str(sal_str)
        if s_min is not None and s_max is not None:
            midpoint = (s_min + s_max) / 2
            salaries.append({"min": s_min, "max": s_max, "mid": midpoint, "raw": sal_str})

    if not salaries:
        return {
            "status": "error",
            "message": f"No salary data found in {len(apps)} applications. Most jobs may have 'Not disclosed' salary.",
            "error_code": "NOT_FOUND",
        }

    # Sort by midpoint
    salaries.sort(key=lambda s: s["mid"])
    mids = [s["mid"] for s in salaries]
    median = mids[len(mids) // 2]

    # Distribution buckets (5 LPA intervals)
    buckets = Counter()
    for s in salaries:
        bucket_start = int(s["mid"] // 5) * 5
        bucket_label = f"{bucket_start}-{bucket_start + 5} LPA"
        buckets[bucket_label] += 1

    distribution = [{"range": r, "count": c} for r, c in sorted(buckets.items(), key=lambda x: x[0])]

    # Insights
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


async def _match_quality(days: int = 7) -> dict:
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
    data = await api_get(f"{APPLY_MATCH_SCORE_API}?days={days}")

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
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_insights(
    insight_type: str = "applications",
    action: Optional[str] = None,
    key: Optional[str] = None,
    new_answer: Optional[str] = None,
    days: int = 30,
    designation: Optional[str] = None,
    # skill_gap / salary_benchmark params
    keywords: Optional[str] = None,
    use_recommendations: bool = True,
    sample_size: int = 20,
    include_assessments: bool = True,
    timeout_seconds: int = 120,
    location: Optional[str] = None,
    freshness: Optional[int] = None,
) -> dict:
    """Unified intelligence layer — application insights, salary analysis, cached answers.

    Insight types:
      - "applications": Analyze application history for patterns (velocity, status, companies)
      - "salary": Analyze salary positioning across applied jobs
      - "cached_answers": Manage cached screening question answers (list/update/delete)
      - "match_analytics": Match-score analytics for recent applications (distribution + per-field breakdowns)
      - "match_quality": Aggregate apply-match quality — how well recent applications matched your profile (uses days param, default 7)
      - "skill_gap": Analyze skill gaps between your profile and market demand
      - "salary_benchmark": Benchmark your salary against market for a given role
      - "taxonomy": Get Naukri's job taxonomy hierarchy (37 departments → 167 role categories → 1461 roles) with synonyms. Cached for 24h.

    Args:
        insight_type: "applications" | "salary" | "cached_answers" | "match_analytics" | "match_quality" | "skill_gap" | "salary_benchmark" | "taxonomy"
        action: For cached_answers only — "list" | "update" | "delete" (default "list")
        key: For cached_answers update/delete — the cache key
        new_answer: For cached_answers update — the new answer value
        days: For applications/match_quality — analyze last N days (default 30 for applications, 7 for match_quality)
        designation: For salary — filter by job title keyword (optional)
        keywords: For skill_gap/salary_benchmark — search keywords (required for salary_benchmark; required for skill_gap if use_recommendations is False)
        use_recommendations: For skill_gap — use personalized recommendations (default True)
        sample_size: For skill_gap/salary_benchmark — number of jobs to analyze (default 20, max 50)
        include_assessments: For skill_gap — fetch assessments and boost passed-skill frequency (default True)
        timeout_seconds: For skill_gap/salary_benchmark — max seconds before timeout (default 120)
        location: For salary_benchmark — city to filter (e.g., "Bangalore"). None = all India.
        freshness: For salary_benchmark — posted within N days (default None). None = no filter.

    Returns:
        - applications: {status, period_days, total_applications, status_breakdown, velocity, top_companies, insights}
        - salary: {status, total_with_salary, salary_range: {min, max, median}, distribution, insights}
        - cached_answers list: {status, total_cached, answers: [{key, question, answer, type, cached_at}]}
        - cached_answers update: {status, key, new_answer, message}
        - cached_answers delete: {status, key, message}
        - match_analytics: {status, days, total_applies, complete_match, high_match, medium_match, low_match, field_breakdown, user_details}
        - match_quality: {status, days, total_applies, complete_match, high_match, medium_match, low_match, field_breakdown}
        - skill_gap: {status, jobs_analyzed, skill_gaps, strong_skills, assessments_used}
        - salary_benchmark: {status, jobs_sampled, jobs_with_salary, salary_aggregate, your_positioning, salary_by_company}
        - taxonomy: {status, total_departments, total_roles, departments: [{id, label, synonyms, role_categories: [{id, label, roles: [{id, label, synonyms}]}]}]}
        - {status: "error", message} on failure
    """
    # ── applications ──────────────────────────────────────────────────
    if insight_type == "applications":
        try:
            return await _application_insights(days=days)
        except Exception as e:
            return {"status": "error", "message": f"Failed to analyze applications: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── salary ────────────────────────────────────────────────────────
    elif insight_type == "salary":
        try:
            return await _salary_position(designation=designation)
        except Exception as e:
            return {"status": "error", "message": f"Failed to analyze salary position: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── cached_answers ────────────────────────────────────────────────
    elif insight_type == "cached_answers":
        try:
            return await _cached_answers(action=action or "list", key=key, new_answer=new_answer)
        except Exception as e:
            return {"status": "error", "message": f"Failed to manage cached answers: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── match_analytics ───────────────────────────────────────────────
    elif insight_type == "match_analytics":
        from naukri_server.tools.tracking import _get_match_analytics
        try:
            return await _get_match_analytics(days=days)
        except Exception as e:
            return {"status": "error", "message": f"Match analytics failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── match_quality ────────────────────────────────────────────────
    elif insight_type == "match_quality":
        try:
            return await _match_quality(days=days)
        except Exception as e:
            return {"status": "error", "message": f"Match quality failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── skill_gap ───────────────────────────────────────────────────
    elif insight_type == "skill_gap":
        from naukri_server.tools.skill_gap import _skill_gap_analysis
        try:
            return await _skill_gap_analysis(
                keywords=keywords, use_recommendations=use_recommendations,
                sample_size=sample_size, include_assessments=include_assessments,
                timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            return {"status": "error", "message": f"Skill gap analysis failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── salary_benchmark ─────────────────────────────────────────────
    elif insight_type == "salary_benchmark":
        if not keywords:
            return {"status": "error", "message": "salary_benchmark requires keywords.", "error_code": "VALIDATION_ERROR"}
        from naukri_server.tools.research import _salary_benchmark
        try:
            return await _salary_benchmark(
                keywords=keywords, location=location,
                sample_size=sample_size, freshness=freshness,
                timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            return {"status": "error", "message": f"Salary benchmark failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── taxonomy ──────────────────────────────────────────────────────
    elif insight_type == "taxonomy":
        try:
            return await _get_taxonomy()
        except Exception as e:
            return {"status": "error", "message": f"Taxonomy fetch failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── unknown insight_type ──────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown insight_type '{insight_type}'. Use: applications, salary, cached_answers, match_analytics, match_quality, skill_gap, salary_benchmark, taxonomy", "error_code": "VALIDATION_ERROR"}
