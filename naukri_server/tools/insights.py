"""Intelligence layer — application insights, cached answer review, salary analysis.

Business logic lives in naukri_server.services.insights_service.
Functions that can be cleanly delegated are re-exported from the service.
_get_taxonomy and _cached_answers remain defined here because tests
patch module-level state (_taxonomy_cache, _cache_lock, _load_cache).
"""

from typing import Optional

from naukri_server import mcp
from naukri_server.interfaces import api_client  # noqa: F401 — test patch target
from naukri_server.cache import _load_cache, _cache_lock  # noqa: F401 — test patch target
from naukri_server.error_handler import handle_tool_action
from naukri_server.config import ENTITY_TAXONOMY_API
from naukri_server.models import validate_action_params

from naukri_server.utils import TtlCache

# Re-exports from service layer (preserves import and patch paths)
from naukri_server.services.insights_service import (  # noqa: F401
    conversion_funnel as _conversion_funnel,
    application_insights as _application_insights,
    salary_position as _salary_position,
    match_quality as _match_quality,
    detect_status_changes as _detect_status_changes,
    get_profile_prompts as _get_profile_prompts,
    parse_salary_str as _parse_salary_str,
)


_taxonomy_cache = TtlCache(86400)  # 24 hours — taxonomy data is static


# ---------------------------------------------------------------------------
# _get_taxonomy and _cached_answers stay here — tests patch _taxonomy_cache,
# _cache_lock, _load_cache on this module.
# ---------------------------------------------------------------------------

async def _get_taxonomy() -> dict:
    """Fetch Naukri's job taxonomy: 37 departments -> 167 role categories -> 1461 roles."""

    async def _fetch():
        data = await api_client.get(ENTITY_TAXONOMY_API, params={
            "appid": "1", "languageId": "1", "formatType": "nested", "srcAppId": "121",
        })
        # Response key varies: raw list, "ENTITY_DEPART-ROLE_CATEG-ROLE", "data", or "entities"
        entities = (
            data if isinstance(data, list)
            else data.get("ENTITY_DEPART-ROLE_CATEG-ROLE",
                          data.get("data", data.get("entities", [])))
        )

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

        return {
            "status": "success",
            "total_departments": len(departments),
            "total_roles": total_roles,
            "departments": departments,
        }

    return await _taxonomy_cache.get(_fetch)


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


# ---------------------------------------------------------------------------
# Insight registry — maps insight_type to async handler(kwargs) -> dict
# ---------------------------------------------------------------------------

def _make_match_analytics_handler(**kw):
    from naukri_server.tools.tracking import _get_match_analytics
    return _get_match_analytics(days=kw.get("days", 30))

def _make_skill_gap_handler(**kw):
    from naukri_server.tools.skill_gap import _skill_gap_analysis
    return _skill_gap_analysis(
        keywords=kw.get("keywords"), use_recommendations=kw.get("use_recommendations", True),
        sample_size=kw.get("sample_size", 20), include_assessments=kw.get("include_assessments", True),
        timeout_seconds=kw.get("timeout_seconds", 120),
    )

def _make_salary_benchmark_handler(**kw):
    from naukri_server.tools.research import _salary_benchmark
    return _salary_benchmark(
        keywords=kw.get("keywords"), location=kw.get("location"),
        sample_size=kw.get("sample_size", 20), freshness=kw.get("freshness"),
        timeout_seconds=kw.get("timeout_seconds", 120),
    )

_VALID_PARAMS_PER_INSIGHT = {
    "applications": {"days"},
    "salary": {"designation"},
    "cached_answers": {"action", "key", "new_answer"},
    "match_analytics": {"days"},
    "match_quality": {"days"},
    "skill_gap": {"keywords", "use_recommendations", "sample_size",
                  "include_assessments", "timeout_seconds"},
    "salary_benchmark": {"keywords", "location", "sample_size",
                         "freshness", "timeout_seconds"},
    "taxonomy": set(),
    "profile_prompts": set(),
    "conversion_funnel": {"days"},
    "status_changes": {"days"},
}

_INSIGHT_REGISTRY: dict[str, callable] = {
    "applications":     lambda **kw: _application_insights(days=kw.get("days", 30)),
    "salary":           lambda **kw: _salary_position(designation=kw.get("designation")),
    "cached_answers":   lambda **kw: _cached_answers(action=kw.get("action") or "list", key=kw.get("key"), new_answer=kw.get("new_answer")),
    "match_analytics":  _make_match_analytics_handler,
    "match_quality":    lambda **kw: _match_quality(days=kw.get("days", 30)),
    "skill_gap":        _make_skill_gap_handler,
    "salary_benchmark": _make_salary_benchmark_handler,
    "taxonomy":         lambda **kw: _get_taxonomy(),
    "profile_prompts":  lambda **kw: _get_profile_prompts(),
    "conversion_funnel": lambda **kw: _conversion_funnel(days=kw.get("days", 30)),
    "status_changes":   lambda **kw: _detect_status_changes(days_back=kw.get("days", 30)),
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
    """[Deprecated — use individual insight tools instead: naukri_application_insights, naukri_salary_position, etc.]

    Unified intelligence layer — application insights, salary analysis, cached answers.

    Note: Uses 'insight_type' instead of 'action' because each value selects a different
    analytical lens (applications, salary, skill_gap, taxonomy, etc.) rather than a CRUD
    operation. The tool also has a secondary 'action' param for cached_answers sub-dispatch.

    Insight types:
      - "applications": Analyze application history for patterns (velocity, status, companies)
      - "salary": Analyze salary positioning across applied jobs
      - "cached_answers": Manage cached screening question answers (list/update/delete)
      - "match_analytics": Match-score analytics for recent applications (distribution + per-field breakdowns)
      - "match_quality": Aggregate apply-match quality — how well recent applications matched your profile (uses days param, default 7)
      - "skill_gap": Analyze skill gaps between your profile and market demand
      - "salary_benchmark": Benchmark your salary against market for a given role
      - "taxonomy": Get Naukri's job taxonomy hierarchy (37 departments → 167 role categories → 1461 roles) with synonyms. Cached for 24h.
      - "profile_prompts": Fetch CCS widget state keys to identify pending profile completion actions (salary breakup, locations, etc.). Uses browser cookies.
      - "conversion_funnel": Analyze application-to-interview conversion funnel — status breakdown, company response rates, and dead zones (companies with 3+ applies and 0 responses)
      - "status_changes": Detect application status changes by syncing with Naukri. Categorizes transitions as positive (applied→viewed, viewed→interview, etc.) or neutral. Uses days param for sync window (default 30).

    Args:
        insight_type: "applications" | "salary" | "cached_answers" | "match_analytics" | "match_quality" | "skill_gap" | "salary_benchmark" | "taxonomy" | "profile_prompts" | "conversion_funnel" | "status_changes"
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
        - profile_prompts: {status, source, pending_count, completed_count, pending_prompts: [{field, action, impact, reason}], completed_prompts, all_state_keys, cache_ttl_seconds, widget_sections_count}
        - conversion_funnel: {status, days, total_applied, funnel, conversion_rate, top_responsive_companies: [{company, applied, responded, rate}], dead_zones: [{company, applied, responded, rate}]}
        - status_changes: {status, total_changes, positive_changes, positive: [{job_id, title, old_status, new_status, transition_type}], neutral: [...], sync_method, last_sync}
        - {status: "error", message} on failure
    """
    # ── ISP: warn about params irrelevant to chosen insight_type ────
    _provided = {
        "action": action, "key": key, "new_answer": new_answer,
        "days": days if days != 30 else None, "designation": designation,
        "keywords": keywords, "use_recommendations": use_recommendations if not use_recommendations else None,
        "sample_size": sample_size if sample_size != 20 else None,
        "include_assessments": include_assessments if not include_assessments else None,
        "timeout_seconds": timeout_seconds if timeout_seconds != 120 else None,
        "location": location, "freshness": freshness,
    }
    _unused = validate_action_params(insight_type, _provided, _VALID_PARAMS_PER_INSIGHT)

    # ── Pre-validation ──────────────────────────────────────────────
    if insight_type == "salary_benchmark" and not keywords:
        return {"status": "error", "message": "salary_benchmark requires keywords.", "error_code": "VALIDATION_ERROR"}

    # ── Registry lookup ──────────────────────────────────────────────
    handler = _INSIGHT_REGISTRY.get(insight_type)
    if handler is None:
        return {
            "status": "error",
            "message": f"Unknown insight_type '{insight_type}'. Use: {', '.join(_INSIGHT_REGISTRY)}",
            "error_code": "VALIDATION_ERROR",
        }

    result = await handle_tool_action(
        lambda: handler(
            action=action, key=key, new_answer=new_answer,
            days=days, designation=designation, keywords=keywords,
            use_recommendations=use_recommendations, sample_size=sample_size,
            include_assessments=include_assessments, timeout_seconds=timeout_seconds,
            location=location, freshness=freshness,
        ),
        f"insights.{insight_type}",
    )
    if _unused and isinstance(result, dict):
        result["unused_params"] = _unused
    return result


# ---------------------------------------------------------------------------
# Individual insight tools (preferred over the deprecated consolidated tool)
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_application_insights(days: int = 30) -> dict:
    """Analyze your application history — status breakdown, top companies, response rates.

    Use this to understand application velocity, which companies you've applied to most,
    and how your applications are progressing through different statuses.

    Args:
        days: Analyze applications from last N days (default 30)

    Returns:
        {status, period_days, total_applications, status_breakdown, velocity, top_companies, insights}
    """
    return await handle_tool_action(
        lambda: _application_insights(days=days),
        "insights.applications",
    )


@mcp.tool()
async def naukri_salary_position(designation: Optional[str] = None) -> dict:
    """Analyze salary positioning across your applied jobs.

    Compares salary ranges from jobs you've applied to, showing min/max/median
    and distribution. Useful for understanding your market value positioning.

    Args:
        designation: Filter by job title keyword (optional)

    Returns:
        {status, total_with_salary, salary_range, distribution, insights}
    """
    return await handle_tool_action(
        lambda: _salary_position(designation=designation),
        "insights.salary",
    )


@mcp.tool()
async def naukri_cached_answers(
    action: str = "list",
    key: Optional[str] = None,
    new_answer: Optional[str] = None,
) -> dict:
    """Manage cached screening question answers — list, update, or delete.

    When you apply to jobs, screening question answers are cached for reuse.
    Use this to review what's cached and correct any wrong answers before they're reused.

    Args:
        action: "list" | "update" | "delete" (default "list")
        key: Cache key for update/delete operations
        new_answer: New answer value for update action

    Returns:
        list: {status, total_cached, answers: [{key, question, answer, type, cached_at}]}
        update: {status, key, new_answer, message}
        delete: {status, key, message}
    """
    return await handle_tool_action(
        lambda: _cached_answers(action=action, key=key, new_answer=new_answer),
        "insights.cached_answers",
    )


@mcp.tool()
async def naukri_match_analytics(days: int = 30) -> dict:
    """Analyze match-score distribution across recent applications.

    Shows how well your profile matched the jobs you applied to, with per-field
    breakdowns (skills, experience, location, etc.) to identify weak areas.

    Args:
        days: Analyze applications from last N days (default 30)

    Returns:
        {status, days, total_applies, complete_match, high_match, medium_match, low_match, field_breakdown, user_details}
    """
    return await handle_tool_action(
        lambda: _make_match_analytics_handler(days=days),
        "insights.match_analytics",
    )


@mcp.tool()
async def naukri_match_quality(days: int = 30) -> dict:
    """Aggregate apply-match quality — how well recent applications matched your profile.

    Higher-level summary than match_analytics, focused on overall quality trends
    rather than per-field breakdowns.

    Args:
        days: Analyze applications from last N days (default 30)

    Returns:
        {status, days, total_applies, complete_match, high_match, medium_match, low_match, field_breakdown}
    """
    return await handle_tool_action(
        lambda: _match_quality(days=days),
        "insights.match_quality",
    )


@mcp.tool()
async def naukri_skill_gap(
    keywords: Optional[str] = None,
    use_recommendations: bool = True,
    sample_size: int = 20,
    include_assessments: bool = True,
    timeout_seconds: int = 120,
) -> dict:
    """Analyze skill gaps between your profile and market demand.

    Searches jobs matching your profile or keywords, extracts required skills,
    and compares against your profile skills to find gaps and strengths.

    Args:
        keywords: Search keywords (required if use_recommendations is False)
        use_recommendations: Use personalized recommendations (default True)
        sample_size: Number of jobs to analyze (default 20, max 50)
        include_assessments: Boost passed-skill frequency from assessments (default True)
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        {status, jobs_analyzed, skill_gaps, strong_skills, assessments_used}
    """
    return await handle_tool_action(
        lambda: _make_skill_gap_handler(
            keywords=keywords, use_recommendations=use_recommendations,
            sample_size=sample_size, include_assessments=include_assessments,
            timeout_seconds=timeout_seconds,
        ),
        "insights.skill_gap",
    )


@mcp.tool()
async def naukri_salary_benchmark(
    keywords: str = "",
    location: Optional[str] = None,
    sample_size: int = 20,
    freshness: Optional[int] = None,
    timeout_seconds: int = 120,
) -> dict:
    """Benchmark your salary against market rates for a given role.

    Searches live job postings, extracts salary data, and compares against
    your current salary to show positioning (percentile, above/below market).

    Args:
        keywords: Search keywords for the role (required)
        location: City to filter (e.g., "Bangalore"). None = all India.
        sample_size: Number of jobs to analyze (default 20, max 50)
        freshness: Posted within N days (default None = no filter)
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        {status, jobs_sampled, jobs_with_salary, salary_aggregate, your_positioning, salary_by_company}
    """
    if not keywords:
        return {"status": "error", "message": "salary_benchmark requires keywords.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _make_salary_benchmark_handler(
            keywords=keywords, location=location, sample_size=sample_size,
            freshness=freshness, timeout_seconds=timeout_seconds,
        ),
        "insights.salary_benchmark",
    )


@mcp.tool()
async def naukri_taxonomy() -> dict:
    """Get Naukri's job taxonomy hierarchy — 37 departments, 167 role categories, 1461 roles.

    Use this to discover valid role IDs, department IDs, and synonyms when constructing
    search queries or understanding how Naukri classifies jobs. Cached for 24h.

    Returns:
        {status, total_departments, total_roles, departments: [{id, label, synonyms, role_categories}]}
    """
    return await handle_tool_action(
        lambda: _get_taxonomy(),
        "insights.taxonomy",
    )


@mcp.tool()
async def naukri_profile_prompts() -> dict:
    """Fetch pending profile completion actions from Naukri's CCS widget.

    Identifies what profile sections need attention (salary breakup, preferred locations,
    resume headline, etc.) and their impact on profile visibility. Uses browser cookies.

    Returns:
        {status, source, pending_count, completed_count, pending_prompts, completed_prompts, all_state_keys}
    """
    return await handle_tool_action(
        lambda: _get_profile_prompts(),
        "insights.profile_prompts",
    )


@mcp.tool()
async def naukri_conversion_funnel(days: int = 30) -> dict:
    """Analyze application-to-interview conversion funnel.

    Shows status breakdown, company response rates, and dead zones (companies
    with 3+ applications and 0 responses) to optimize where you apply next.

    Args:
        days: Analyze applications from last N days (default 30)

    Returns:
        {status, days, total_applied, funnel, conversion_rate, top_responsive_companies, dead_zones}
    """
    return await handle_tool_action(
        lambda: _conversion_funnel(days=days),
        "insights.conversion_funnel",
    )


@mcp.tool()
async def naukri_status_changes(days: int = 30) -> dict:
    """Detect application status changes by syncing with Naukri.

    Compares local application records against Naukri's server data to find
    status transitions (applied->viewed, viewed->shortlisted, etc.).

    Args:
        days: Sync window in days (default 30)

    Returns:
        {status, total_changes, positive_changes, positive, neutral, sync_method, last_sync}
    """
    return await handle_tool_action(
        lambda: _detect_status_changes(days_back=days),
        "insights.status_changes",
    )
