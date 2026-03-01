"""Assessment tools — skill test scores and profile completeness metrics."""

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.config import DASHBOARD_API


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _list_assessments() -> dict:
    """Fetch skill assessment results from the dashboard API."""
    data = await api_get(DASHBOARD_API)
    dashboard = data.get("dashBoard", data)
    assessments_raw = dashboard.get("assessments", [])

    assessments = []
    for a in assessments_raw:
        if not isinstance(a, dict):
            continue
        results = a.get("results", {})
        if not isinstance(results, dict):
            results = {}
        level = a.get("level", "")
        if isinstance(level, dict):
            level = level.get("name", "")
        badge_url = a.get("badge", "")
        if isinstance(badge_url, dict):
            badge_url = badge_url.get("imgStr", "")
        assessments.append({
            "skill": a.get("skill", ""),
            "level": level,
            "badge_url": badge_url,
            "score": results.get("score"),
            "rank": results.get("rank"),
            "accuracy": results.get("accuracy"),
            "status": results.get("status", ""),
            "max_marks": a.get("maxMarks"),
            "passing_marks": a.get("passingMarks"),
            "test_id": a.get("testId"),
        })

    return {
        "status": "success",
        "total": len(assessments),
        "count": len(assessments),
        "page": 1,
        "has_more": False,
        "assessments": assessments,
    }


async def _get_profile_completeness() -> dict:
    """Fetch profile completeness score and key metrics from the dashboard API."""
    data = await api_get(DASHBOARD_API)
    dashboard = data.get("dashBoard", data)

    # Extract job search status
    jb_status = dashboard.get("jbSearchStatus", {})
    if isinstance(jb_status, dict):
        status_data = jb_status.get("data", [])
        job_search = status_data[0].get("value", "") if status_data else ""
    else:
        job_search = str(jb_status)

    return {
        "status": "success",
        "completeness_percent": dashboard.get("pc"),
        "search_appearances": dashboard.get("totalSearchAppearancesCount"),
        "recruiter_views": dashboard.get("profileViewCount"),
        "is_paid": dashboard.get("isPaidUser", False),
        "is_premium": dashboard.get("isPremium", False),
        "ai_resume_eligible": dashboard.get("isAIResumeEligible", False),
        "mock_interview_eligible": dashboard.get("eligibleFlagForAIMockInterview", False),
        "job_search_status": job_search,
    }


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_assessments(action: str = "list") -> dict:
    """Unified assessment & profile completeness tool — skill tests and profile metrics.

    Actions:
      - "list": Get your Naukri skill assessment results — DoSelect test scores for
        SQL, Python, JavaScript, AWS, Linux, HTML, etc.
      - "completeness": Get your profile completeness score and key metrics — completion
        percentage, search appearances, recruiter views, and service eligibility flags.

    Args:
        action: "list" | "completeness"

    Returns:
        - list: {status, total, count, page, has_more, assessments: [{skill, level, badge_url, score, rank, accuracy, status, max_marks, passing_marks, test_id}]}
        - completeness: {status, completeness_percent, search_appearances, recruiter_views, is_paid, is_premium, ai_resume_eligible, mock_interview_eligible, job_search_status}
        - {status: "error", message} on failure
    """
    # ── list ───────────────────────────────────────────────────────────
    if action == "list":
        try:
            return await _list_assessments()
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Get assessments failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── completeness ──────────────────────────────────────────────────
    elif action == "completeness":
        try:
            return await _get_profile_completeness()
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Get profile completeness failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── unknown action ────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, completeness", "error_code": "VALIDATION_ERROR"}
