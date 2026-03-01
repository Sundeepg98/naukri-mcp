"""Intelligence layer — application insights, cached answer review, salary analysis."""

import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Optional

from naukri_server import mcp
from naukri_server.cache import _load_cache, _cache_lock
from naukri_server.config import LAKHS_MULTIPLIER
from naukri_server.tools.tracking import _load_json, _applications_lock, APPLICATIONS_FILE


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _application_insights(days: int = 30) -> dict:
    """Analyze application history for patterns and insights."""
    if days < 1:
        return {"status": "error", "message": "days must be >= 1", "error_code": "VALIDATION_ERROR"}
    async with _applications_lock:
        apps = _load_json(APPLICATIONS_FILE)

    if not apps:
        return {"status": "no_data", "message": "No applications tracked yet. Use naukri_apply or naukri_sync(entity=\"applications\") first."}

    # Filter by date range
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent = [a for a in apps if (a.get("applied_at") or "") >= cutoff]

    if not recent:
        return {
            "status": "no_data",
            "message": f"No applications in the last {days} days. Total tracked: {len(apps)}.",
            "total_all_time": len(apps),
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
            return {"status": "no_data", "message": "No cached answers yet. Apply to jobs to build the cache."}

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
        return {"status": "no_data", "message": "No applications tracked yet."}

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
            "status": "no_data",
            "message": f"No salary data found in {len(apps)} applications. Most jobs may have 'Not disclosed' salary.",
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
) -> dict:
    """Unified intelligence layer — application insights, salary analysis, cached answers.

    Insight types:
      - "applications": Analyze application history for patterns (velocity, status, companies)
      - "salary": Analyze salary positioning across applied jobs
      - "cached_answers": Manage cached screening question answers (list/update/delete)

    Args:
        insight_type: "applications" | "salary" | "cached_answers"
        action: For cached_answers only — "list" | "update" | "delete" (default "list")
        key: For cached_answers update/delete — the cache key
        new_answer: For cached_answers update — the new answer value
        days: For applications — analyze last N days (default 30)
        designation: For salary — filter by job title keyword (optional)

    Returns:
        - applications: {status, period_days, total_applications, status_breakdown, velocity, top_companies, insights}
        - salary: {status, total_with_salary, salary_range: {min, max, median}, distribution, insights}
        - cached_answers list: {status, total_cached, answers: [{key, question, answer, type, cached_at}]}
        - cached_answers update: {status, key, new_answer, message}
        - cached_answers delete: {status, key, message}
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

    # ── unknown insight_type ──────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown insight_type '{insight_type}'. Use: applications, salary, cached_answers", "error_code": "API_ERROR"}
