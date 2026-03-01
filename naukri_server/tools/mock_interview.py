"""Mock interview tools — AI interview topics, history, and available roles."""

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.config import MOCK_INTERVIEW_TOPICS_API, MOCK_INTERVIEW_HISTORY_API, MOCK_INTERVIEW_ROLE_API


@mcp.tool()
@api_tool("Get mock interview topics")
async def naukri_get_mock_interview_topics() -> dict:
    """Get available AI mock interview topics and your completion status.

    Shows which interview topic areas are available and whether you've completed them.

    Returns:
        - {status: "success", count, topics: [{name, status, ...}], roles: [{...}]}
        - {status: "error", message}
    """
    topics_resp = await api_get(MOCK_INTERVIEW_TOPICS_API)
    roles_resp = await api_get(MOCK_INTERVIEW_ROLE_API)

    # Both responses are wrapped: {"statusCode":"0","message":"Successful","data":{...}}
    # Unwrap the "data" envelope first.
    topics_data = topics_resp.get("data", topics_resp) if isinstance(topics_resp, dict) else topics_resp
    roles_data = roles_resp.get("data", roles_resp) if isinstance(roles_resp, dict) else roles_resp

    # Parse topics — data.topics is a list of {topicId, topicName, testDoneCount, freeTopic, ...}
    topics_raw = topics_data if isinstance(topics_data, list) else topics_data.get("topics", [])
    topics = []
    if isinstance(topics_raw, list):
        for t in topics_raw:
            if isinstance(t, dict):
                topics.append({
                    "name": t.get("topicName") or t.get("name") or t.get("topic", ""),
                    "status": t.get("status", ""),
                    "id": t.get("topicId") or t.get("id", ""),
                    "tests_done": t.get("testDoneCount", 0),
                    "free": t.get("freeTopic", False),
                })
            elif isinstance(t, str):
                topics.append({"name": t, "status": ""})

    # Parse roles — data.roleInfo is a list of {roleId, active, expiryDate}
    roles_raw = roles_data if isinstance(roles_data, list) else roles_data.get("roleInfo", roles_data.get("roles", []))
    roles = []
    if isinstance(roles_raw, list):
        for r in roles_raw:
            if isinstance(r, dict):
                roles.append({
                    "name": r.get("name") or r.get("role", ""),
                    "id": r.get("roleId") or r.get("id", ""),
                    "active": r.get("active", ""),
                    "expiry_date": r.get("expiryDate", ""),
                })

    return {
        "status": "success",
        "count": len(topics),
        "topics": topics,
        "roles": roles,
    }


@mcp.tool()
@api_tool("Get mock interview history")
async def naukri_get_mock_interview_history() -> dict:
    """Get your AI mock interview history — previous interviews, scores, and feedback.

    Returns:
        - {status: "success", interview_count, interviews: [{...}]}
        - {status: "error", message}
    """
    # The previousInterview endpoint requires pagination params in the POST body.
    # Sending an empty body {} causes HTTP 500 (server bug — it NPEs on missing page/pageSize).
    # The detailedView query param is also required (400 without it).
    # Discovered via browser request interception: body must be {"page":1,"pageSize":N}.
    data = await api_post(
        MOCK_INTERVIEW_HISTORY_API + "?detailedView=false",
        body={"page": 1, "pageSize": 50},
    )

    # Response is wrapped: {"statusCode":"0","message":"Successful","data":{...}}
    inner = data.get("data", data)
    interviews_raw = inner.get("previousInterview", inner.get("interviews", []))
    interviews = []
    if isinstance(interviews_raw, list):
        for iv in interviews_raw:
            if isinstance(iv, dict):
                interviews.append(iv)

    return {
        "status": "success",
        "interview_count": inner.get("interviewCount", len(interviews)),
        "interviews": interviews,
    }
