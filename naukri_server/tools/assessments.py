"""Assessment tools — skill test scores and profile completeness metrics."""

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.config import DASHBOARD_API


@mcp.tool()
async def naukri_get_assessments() -> dict:
    """Get your Naukri skill assessment results — DoSelect test scores for
    SQL, Python, JavaScript, AWS, Linux, HTML, etc.

    Returns:
        - {status: "success", count, assessments: [{skill, level, badge, score, rank, accuracy, status, max_marks, passing_marks, test_id}]}
        - {status: "error", message}
    """
    try:
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
            "count": len(assessments),
            "assessments": assessments,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get assessments: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_profile_completeness() -> dict:
    """Get your profile completeness score and key metrics — completion percentage,
    search appearances, recruiter views, and service eligibility flags.

    Returns:
        - {status: "success", completeness_percent, search_appearances, recruiter_views, is_paid, is_premium, ai_resume_eligible, mock_interview_eligible, job_search_status}
        - {status: "error", message}
    """
    try:
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
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get profile completeness: {type(e).__name__}: {e}"}
