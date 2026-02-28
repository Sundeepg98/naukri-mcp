"""Mock interview tools — AI interview topics, history, and available roles."""

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import MOCK_INTERVIEW_TOPICS_API, MOCK_INTERVIEW_HISTORY_API, MOCK_INTERVIEW_ROLE_API


@mcp.tool()
async def naukri_get_mock_interview_topics() -> dict:
    """Get available AI mock interview topics and your completion status.

    Shows which interview topic areas are available and whether you've completed them.

    Returns:
        - {status: "success", count, topics: [{name, status, ...}], roles: [{...}]}
        - {status: "error", message}
    """
    try:
        topics_data = await api_get(MOCK_INTERVIEW_TOPICS_API)
        roles_data = await api_get(MOCK_INTERVIEW_ROLE_API)

        # Parse topics
        topics_raw = topics_data if isinstance(topics_data, list) else topics_data.get("topics", topics_data.get("data", []))
        topics = []
        if isinstance(topics_raw, list):
            for t in topics_raw:
                if isinstance(t, dict):
                    topics.append({
                        "name": t.get("name") or t.get("topic", ""),
                        "status": t.get("status", ""),
                        "id": t.get("id") or t.get("topicId", ""),
                    })
                elif isinstance(t, str):
                    topics.append({"name": t, "status": ""})

        # Parse roles
        roles_raw = roles_data if isinstance(roles_data, list) else roles_data.get("roleInfo", roles_data.get("roles", []))
        roles = []
        if isinstance(roles_raw, list):
            for r in roles_raw:
                if isinstance(r, dict):
                    roles.append({
                        "name": r.get("name") or r.get("role", ""),
                        "id": r.get("id") or r.get("roleId", ""),
                    })

        return {
            "status": "success",
            "count": len(topics),
            "topics": topics,
            "roles": roles,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get mock interview topics: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_mock_interview_history() -> dict:
    """Get your AI mock interview history — previous interviews, scores, and feedback.

    Returns:
        - {status: "success", interview_count, interviews: [{...}]}
        - {status: "error", message}
    """
    try:
        data = await api_post(
            MOCK_INTERVIEW_HISTORY_API + "?detailedView=false",
            body={},
        )

        interviews_raw = data.get("previousInterview", data.get("interviews", []))
        interviews = []
        if isinstance(interviews_raw, list):
            for iv in interviews_raw:
                if isinstance(iv, dict):
                    interviews.append(iv)

        return {
            "status": "success",
            "interview_count": data.get("interviewCount", len(interviews)),
            "interviews": interviews,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get mock interview history: {type(e).__name__}: {e}"}
