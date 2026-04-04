"""Application intelligence service — derived insights from tracking data.

Extracted from tracking.py to achieve single-responsibility:
  - tracking.py: CRUD operations + MCP dispatch
  - This module: follow-up scoring, interview prep, recruiter CRM, draft messages

Functions here compute derived intelligence FROM application data but do NOT
own the data layer (locks, JSON persistence, or MCP dispatch).
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from naukri_server.config import logger, APPLICATIONS_FILE
from naukri_server.utils import load_json_with_backup


# ---------------------------------------------------------------------------
# Data access helpers (thin wrappers — avoid circular import with tracking)
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> list:
    return load_json_with_backup(path, logger)


def _get_applications_lock():
    """Lazily fetch the applications lock from tracking to avoid circular import."""
    from naukri_server.tools.tracking import _applications_lock
    return _applications_lock


# ---------------------------------------------------------------------------
# Follow-up priority scoring
# ---------------------------------------------------------------------------

def _compute_follow_up_priority(app: dict) -> int:
    """Score how worthwhile it is to follow up on this application (0-100)."""
    priority = 50  # baseline

    # Recruiter engaged = strong signal
    job_activity = app.get("job_activity", 0)
    if isinstance(job_activity, (int, float)) and job_activity > 0:
        priority += 20

    # High match score = worth pursuing
    ars = app.get("ars_score")
    if isinstance(ars, (int, float)):
        if ars >= 70:
            priority += 15
        elif ars >= 50:
            priority += 10
        elif ars >= 30:
            priority += 5

    # Company rating boost
    rating = app.get("company_rating")
    if isinstance(rating, dict):
        rating = rating.get("AggregateRating")
    if rating:
        try:
            r = float(rating)
            if r >= 4.0:
                priority += 10
            elif r >= 3.5:
                priority += 5
        except (ValueError, TypeError):
            pass

    # Recency decay
    try:
        applied = datetime.fromisoformat(
            app.get("applied_at", "").replace("+00:00", "+00:00")
        )
        days = (datetime.now(timezone.utc) - applied).days
        if days > 60:
            priority -= 20
        elif days > 45:
            priority -= 10
    except Exception:
        pass

    return max(0, min(100, priority))


# ---------------------------------------------------------------------------
# Safe external fetchers (wrappers that never raise)
# ---------------------------------------------------------------------------

async def _safe_fetch_company_intel(company):
    try:
        from naukri_server.tools.ambitionbox import naukri_company_intel
        return await naukri_company_intel(company=company, intel_type="interviews")
    except Exception:
        return None


async def _safe_fetch_mock_topics():
    try:
        from naukri_server.tools.mock_interview import naukri_mock_interview
        return await naukri_mock_interview(action="topics")
    except Exception:
        return None


async def _safe_fetch_fit_score(job_id):
    try:
        from naukri_server.tools.smart_apply import naukri_smart_apply
        return await naukri_smart_apply(job_id=job_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Interview prep
# ---------------------------------------------------------------------------

async def _interview_prep(job_id: str) -> dict:
    """Generate interview prep package for a specific job application."""
    lock = _get_applications_lock()
    async with lock:
        apps = _load_json(APPLICATIONS_FILE)
    app = next((a for a in apps if str(a.get("job_id")) == str(job_id)), None)
    if not app:
        return {"status": "error", "message": f"No application found for job {job_id}", "error_code": "NOT_FOUND"}

    company = app.get("company", "")
    title = app.get("title", "")

    # Parallel fetch: company intel + mock interview topics + fit assessment
    results = await asyncio.gather(
        _safe_fetch_company_intel(company),
        _safe_fetch_mock_topics(),
        _safe_fetch_fit_score(job_id),
        return_exceptions=True,
    )

    company_intel = results[0] if not isinstance(results[0], Exception) else None
    mock_topics = results[1] if not isinstance(results[1], Exception) else None
    fit_data = results[2] if not isinstance(results[2], Exception) else None

    prep = {
        "status": "success",
        "job_id": job_id,
        "company": company,
        "title": title,
        "applied_at": app.get("applied_at"),
        "ars_score": app.get("ars_score"),
    }

    if company_intel and isinstance(company_intel, dict) and company_intel.get("status") == "success":
        prep["company_rating"] = company_intel.get("overall_rating")
        prep["interview_difficulty"] = company_intel.get("difficulty_breakdown")
        prep["sample_questions"] = company_intel.get("interview_experiences", [])[:3]

    if mock_topics and isinstance(mock_topics, dict) and mock_topics.get("status") == "success":
        prep["mock_topics"] = mock_topics.get("topics", [])[:5]

    if fit_data and isinstance(fit_data, dict) and fit_data.get("status") == "success":
        prep["matched_skills"] = fit_data.get("fit_assessment", {}).get("skill_match", {}).get("matched", [])
        prep["missing_skills"] = fit_data.get("fit_assessment", {}).get("skill_match", {}).get("missing", [])

    return prep


# ---------------------------------------------------------------------------
# Draft follow-up message
# ---------------------------------------------------------------------------

async def _draft_follow_up(job_id: str) -> dict:
    """Generate a follow-up message draft for a stale application."""
    lock = _get_applications_lock()
    async with lock:
        apps = _load_json(APPLICATIONS_FILE)

    app = next((a for a in apps if str(a.get("job_id")) == str(job_id)), None)
    if not app:
        return {"status": "error", "message": f"No application found for job {job_id}", "error_code": "NOT_FOUND"}

    company = app.get("company", "the company")
    title = app.get("title", "the position")
    applied_at = app.get("applied_at", "")

    # Parse date for message
    try:
        applied_date = datetime.fromisoformat(applied_at.replace("+00:00", "")).strftime("%B %d, %Y")
    except Exception:
        applied_date = "recently"

    # Calculate days since applied
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(applied_at)
        days_ago = delta.days
    except Exception:
        days_ago = 0

    draft = (
        f"Hi,\n\n"
        f"I applied for the {title} position at {company} on {applied_date} "
        f"({days_ago} days ago). I remain very interested in this opportunity "
        f"and would love to discuss how my experience aligns with the role.\n\n"
        f"Would you be available for a brief conversation? I'm happy to work "
        f"around your schedule.\n\n"
        f"Thank you for your time.\n"
        f"Best regards"
    )

    return {
        "status": "success",
        "job_id": job_id,
        "company": company,
        "title": title,
        "days_since_applied": days_ago,
        "draft_message": draft,
        "suggested_subject": f"Follow-up: {title} Application at {company}",
    }


# ---------------------------------------------------------------------------
# Recruiter history / CRM
# ---------------------------------------------------------------------------

async def _recruiter_history() -> dict:
    """Aggregate per-recruiter communication history from applications + inbox."""
    lock = _get_applications_lock()
    async with lock:
        apps = _load_json(APPLICATIONS_FILE)

    # Group by company
    by_company: dict = {}
    for a in apps:
        co = a.get("company", "Unknown")
        if co not in by_company:
            by_company[co] = {
                "company": co,
                "applications": 0,
                "statuses": [],
                "first_applied": None,
                "last_applied": None,
                "has_response": False,
            }

        entry = by_company[co]
        entry["applications"] += 1
        entry["statuses"].append(a.get("status", "unknown"))

        applied_at = a.get("applied_at", "")
        if not entry["first_applied"] or applied_at < entry["first_applied"]:
            entry["first_applied"] = applied_at
        if not entry["last_applied"] or applied_at > entry["last_applied"]:
            entry["last_applied"] = applied_at

        if a.get("status") in ("interview", "viewed", "shortlisted", "offered"):
            entry["has_response"] = True

    # Sort by most applications first
    companies = sorted(by_company.values(), key=lambda x: -x["applications"])

    return {
        "status": "success",
        "total_companies": len(companies),
        "responsive_count": sum(1 for c in companies if c["has_response"]),
        "unresponsive_count": sum(1 for c in companies if not c["has_response"]),
        "companies": companies[:20],
    }
