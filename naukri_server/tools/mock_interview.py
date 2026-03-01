"""Mock interview tools — AI interview topics, history, and available roles."""

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
import asyncio
from naukri_server.config import (
    MOCK_INTERVIEW_TOPICS_API, MOCK_INTERVIEW_HISTORY_API, MOCK_INTERVIEW_ROLE_API,
    MOCK_INTERVIEW_OTHER_TOPICS_API, MOCK_INTERVIEW_QUESTION_API,
)


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


@mcp.tool()
async def naukri_start_mock_interview(job_id: str) -> dict:
    """Start an AI mock interview session based on a job posting.

    Creates a JD-based interview session and returns the first question.
    You get 1 free attempt per day. Use naukri_answer_mock_question to answer.

    Args:
        job_id: Naukri job ID to base the interview on (from search/recommendations)

    Returns:
        - {status: "success", test_id, topic_id, topic_name, question, company_details}
        - {status: "generating", test_id, topic_id, message} — questions still generating, retry in 5s
        - {status: "error", message}
    """
    try:
        # Step 1: Create/retrieve test session
        session_resp = await api_post(
            MOCK_INTERVIEW_OTHER_TOPICS_API,
            body={"flowType": "JD", "flowId": str(job_id)},
        )
        session_data = session_resp.get("data", session_resp)

        if session_resp.get("statusCode") not in ("0", 0, None):
            return {"status": "error", "message": session_resp.get("message", "Failed to create interview session")}

        topics = session_data.get("topics", [])
        if not topics:
            return {"status": "error", "message": "No interview topics generated for this job."}

        topic = topics[0]
        topic_id = str(topic.get("topicId", ""))
        topic_name = topic.get("topicName", "")
        test_meta = topic.get("testMetaData", [{}])
        test_id = str(test_meta[0].get("testId", "")) if test_meta else ""

        if not test_id or not topic_id:
            return {"status": "error", "message": "Could not extract test/topic IDs from session."}

        company_details = session_data.get("companyDetails", [])
        company_info = None
        if company_details:
            c = company_details[0]
            company_info = {
                "job_id": c.get("jobId"),
                "company": c.get("companyName"),
                "title": c.get("title"),
            }

        # Step 2: Fetch first question (poll up to 5 times)
        for attempt in range(5):
            q_resp = await api_post(
                MOCK_INTERVIEW_QUESTION_API,
                body={"testId": test_id, "topicId": topic_id, "questionId": "", "answer": ""},
            )
            q_data = q_resp.get("data", q_resp)

            if q_data.get("status") == "DONE":
                question = q_data.get("question", {})
                return {
                    "status": "success",
                    "test_id": test_id,
                    "topic_id": topic_id,
                    "topic_name": topic_name,
                    "question": {
                        "id": str(question.get("id", "")),
                        "text": question.get("question", ""),
                        "order": question.get("questionOrder", 0),
                        "ai_answer": question.get("aiAnswer", ""),
                    },
                    "company_details": company_info,
                }

            await asyncio.sleep(3)

        return {
            "status": "generating",
            "test_id": test_id,
            "topic_id": topic_id,
            "message": "Questions still generating. Retry naukri_start_mock_interview in ~10 seconds.",
        }

    except NaukriAPIError as e:
        return {"status": "error", "message": f"Mock interview API error: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to start mock interview: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_answer_mock_question(
    test_id: str,
    topic_id: str,
    question_id: str,
    answer: str,
) -> dict:
    """Submit your answer to a mock interview question and get the next one.

    After answering all questions (typically 4), the interview completes.

    Args:
        test_id: Test session ID from naukri_start_mock_interview
        topic_id: Topic ID from naukri_start_mock_interview
        question_id: Question ID from the current question
        answer: Your answer text

    Returns:
        - {status: "next_question", question: {id, text, order, ai_answer}}
        - {status: "complete", message}
        - {status: "generating", message} — next question still generating, retry in 5s
        - {status: "error", message}
    """
    try:
        for attempt in range(5):
            resp = await api_post(
                MOCK_INTERVIEW_QUESTION_API,
                body={
                    "testId": str(test_id),
                    "topicId": str(topic_id),
                    "questionId": str(question_id),
                    "answer": answer,
                },
            )
            data = resp.get("data", resp)

            test_status = data.get("testStatus", "")
            if test_status in ("COMPLETE", "COMPLETED"):
                return {"status": "complete", "message": "Mock interview complete! Check naukri_get_mock_interview_history for results."}

            if data.get("status") == "DONE":
                question = data.get("question", {})
                if question.get("id"):
                    return {
                        "status": "next_question",
                        "question": {
                            "id": str(question.get("id", "")),
                            "text": question.get("question", ""),
                            "order": question.get("questionOrder", 0),
                            "ai_answer": question.get("aiAnswer", ""),
                        },
                    }
                # No more questions — complete
                return {"status": "complete", "message": "Mock interview complete! Check naukri_get_mock_interview_history for results."}

            await asyncio.sleep(3)

        return {"status": "generating", "message": "Next question still generating. Retry in ~10 seconds."}

    except NaukriAPIError as e:
        return {"status": "error", "message": f"Mock interview API error: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to answer question: {type(e).__name__}: {e}"}
