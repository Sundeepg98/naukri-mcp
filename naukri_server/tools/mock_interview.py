"""Mock interview tools — AI interview topics, sessions, history."""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post
from naukri_server.config import (
    MOCK_INTERVIEW_TOPICS_API, MOCK_INTERVIEW_HISTORY_API, MOCK_INTERVIEW_ROLE_API,
    MOCK_INTERVIEW_OTHER_TOPICS_API, MOCK_INTERVIEW_QUESTION_API,
)
from naukri_server.error_handler import handle_tool_action

_POLL_DELAY = 3            # Seconds between polling attempts
_MAX_POLL_ATTEMPTS = 5     # Max polling iterations


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _get_topics() -> dict:
    """Fetch available mock interview topics and roles."""
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
        "total": len(topics),
        "count": len(topics),
        "page": 1,
        "has_more": False,
        "topics": topics,
        "roles": roles,
    }


async def _get_history() -> dict:
    """Fetch mock interview history."""
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

    total = inner.get("interviewCount", len(interviews))
    return {
        "status": "success",
        "total": total,
        "count": len(interviews),
        "page": 1,
        "has_more": False,
        "interview_count": total,
        "interviews": interviews,
    }


async def _start_interview(job_id: str) -> dict:
    """Start a JD-based mock interview session and return the first question."""
    # Step 1: Create/retrieve test session
    session_resp = await api_post(
        MOCK_INTERVIEW_OTHER_TOPICS_API,
        body={"flowType": "JD", "flowId": str(job_id)},
    )
    session_data = session_resp.get("data", session_resp)

    if session_resp.get("statusCode") not in ("0", 0, None):
        return {"status": "error", "message": session_resp.get("message", "Failed to create interview session"), "error_code": "API_ERROR"}

    topics = session_data.get("topics", [])
    if not topics:
        return {"status": "error", "message": "No interview topics generated for this job.", "error_code": "API_ERROR"}

    topic = topics[0]
    topic_id = str(topic.get("topicId", ""))
    topic_name = topic.get("topicName", "")
    test_meta = topic.get("testMetaData", [{}])
    test_id = str(test_meta[0].get("testId", "")) if test_meta else ""

    if not test_id or not topic_id:
        return {"status": "error", "message": "Could not extract test/topic IDs from session.", "error_code": "API_ERROR"}

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
    for attempt in range(_MAX_POLL_ATTEMPTS):
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

        await asyncio.sleep(_POLL_DELAY)

    return {
        "status": "generating",
        "test_id": test_id,
        "topic_id": topic_id,
        "message": "Questions still generating. Retry naukri_mock_interview(action='start') in ~10 seconds.",
    }


async def _interview_prep(job_id: str) -> dict:
    """Prepare for an interview — job details, company interviews, reviews, mock topics."""
    from naukri_server.tools.jobs import naukri_get_job
    from naukri_server.tools.ambitionbox import _fetch_interviews, _fetch_reviews
    from naukri_server.utils import derive_slug

    if not job_id:
        return {"status": "error", "message": "job_id required for interview prep", "error_code": "VALIDATION_ERROR"}

    # Fetch job details first (need company name for slug)
    job_result = await naukri_get_job(job_id_or_url=job_id)
    if isinstance(job_result, dict) and job_result.get("status") == "error":
        return {"status": "error", "message": f"Failed to fetch job: {job_result.get('message')}", "error_code": "API_ERROR"}

    company = job_result.get("company", "")
    slug = derive_slug(company) if company else None
    errors = []

    # Parallel: fetch interviews + reviews + mock topics
    tasks = []
    if slug:
        tasks.append(_fetch_interviews(slug))   # 0
        tasks.append(_fetch_reviews(slug))       # 1
    else:
        errors.append("No company name — skipping AmbitionBox data")
        tasks.append(asyncio.sleep(0))           # 0 placeholder
        tasks.append(asyncio.sleep(0))           # 1 placeholder

    tasks.append(_get_topics())                  # 2

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Extract interviews
    interviews_data = {}
    if slug and not isinstance(results[0], Exception) and isinstance(results[0], dict) and results[0].get("status") == "success":
        interviews_data = {
            "total_interviews": results[0].get("total_interviews"),
            "overall_difficulty": results[0].get("overall_difficulty"),
            "sample_experiences": results[0].get("interview_experiences", [])[:3],
            "sample_questions": [],
        }
        for exp in results[0].get("interview_experiences", [])[:5]:
            interviews_data["sample_questions"].extend(exp.get("questions", [])[:3])
        interviews_data["sample_questions"] = interviews_data["sample_questions"][:8]
    elif slug:
        errors.append("Interview experiences unavailable")

    # Extract reviews
    reviews_data = {}
    if slug and not isinstance(results[1], Exception) and isinstance(results[1], dict) and results[1].get("status") == "success":
        reviews_data = {
            "overall_rating": results[1].get("overall_rating"),
            "category_ratings": results[1].get("category_ratings"),
        }
    elif slug:
        errors.append("Reviews unavailable")

    # Extract mock topics
    topics_data = []
    if not isinstance(results[2], Exception) and isinstance(results[2], dict) and results[2].get("status") == "success":
        topics_data = results[2].get("topics", [])[:5]

    result = {
        "status": "success",
        "job_id": job_id,
        "job_summary": {
            "title": job_result.get("title"),
            "company": company,
            "salary": job_result.get("salary"),
            "experience": job_result.get("experience"),
            "location": job_result.get("location"),
            "skills": job_result.get("skills", []),
        },
        "company_interviews": interviews_data or None,
        "company_reviews": reviews_data or None,
        "mock_interview_topics": topics_data,
        "preparation_guide": {
            "key_skills": job_result.get("skills", [])[:10],
            "difficulty": interviews_data.get("overall_difficulty", "Unknown"),
            "sample_questions": interviews_data.get("sample_questions", []),
        },
    }
    if errors:
        result["status"] = "partial_success"
        result["errors"] = errors
    return result


async def _answer_question(test_id: str, topic_id: str, question_id: str, answer: str) -> dict:
    """Submit an answer and get the next question or completion status."""
    for attempt in range(_MAX_POLL_ATTEMPTS):
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
            return {"status": "complete", "message": "Mock interview complete! Use action='history' to see results."}

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
            return {"status": "complete", "message": "Mock interview complete! Use action='history' to see results."}

        await asyncio.sleep(_POLL_DELAY)

    return {"status": "generating", "message": "Next question still generating. Retry in ~10 seconds."}


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_mock_interview(
    action: str = "topics",
    job_id: Optional[str] = None,
    test_id: Optional[str] = None,
    topic_id: Optional[str] = None,
    question_id: Optional[str] = None,
    answer: Optional[str] = None,
) -> dict:
    """Unified AI mock interview — topics, history, start session, answer questions, interview prep.

    Actions:
      - "topics": Get available mock interview topics and completion status
      - "history": Get previous mock interview history, scores, and feedback
      - "start": Start a JD-based mock interview (requires job_id)
      - "answer": Submit an answer to a mock interview question (requires test_id, topic_id, question_id, answer)
      - "prep": Prepare for an interview — job details, company interviews/reviews, mock topics (requires job_id)

    Args:
        action: "topics" | "history" | "start" | "answer" | "prep"
        job_id: Required for start/prep — Naukri job ID to base the interview on
        test_id: Required for answer — test session ID from start
        topic_id: Required for answer — topic ID from start
        question_id: Required for answer — question ID from the current question
        answer: Required for answer — your answer text

    Returns:
        - topics: {status, total, count, page, has_more, topics: [{name, status, id, tests_done, free}], roles: [{...}]}
        - history: {status, total, count, page, has_more, interview_count, interviews: [{...}]}
        - start: {status, test_id, topic_id, topic_name, question, company_details}
        - answer: {status: "next_question"|"complete"|"generating", ...}
        - prep: {status, job_id, job_summary, company_interviews, company_reviews, mock_interview_topics, preparation_guide}
        - {status: "error", message} on failure
    """
    # -- topics --------------------------------------------------------------
    if action == "topics":
        return await handle_tool_action(_get_topics, "mock_interview.topics")

    # -- history -------------------------------------------------------------
    elif action == "history":
        return await handle_tool_action(_get_history, "mock_interview.history")

    # -- start ---------------------------------------------------------------
    elif action == "start":
        if not job_id:
            return {"status": "error", "message": "start requires job_id.", "error_code": "VALIDATION_ERROR"}
        return await handle_tool_action(lambda: _start_interview(job_id), "mock_interview.start")

    # -- prep ----------------------------------------------------------------
    elif action == "prep":
        if not job_id:
            return {"status": "error", "message": "prep requires job_id.", "error_code": "VALIDATION_ERROR"}
        return await handle_tool_action(lambda: _interview_prep(job_id), "mock_interview.prep")

    # -- answer --------------------------------------------------------------
    elif action == "answer":
        if not test_id or not topic_id or not question_id or not answer:
            return {"status": "error", "message": "answer requires test_id, topic_id, question_id, and answer.", "error_code": "VALIDATION_ERROR"}
        return await handle_tool_action(lambda: _answer_question(test_id, topic_id, question_id, answer), "mock_interview.answer")

    # -- unknown action ------------------------------------------------------
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: topics, history, start, answer, prep", "error_code": "VALIDATION_ERROR"}
