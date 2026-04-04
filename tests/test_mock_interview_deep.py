"""Deep unit tests for mock_interview module.

Tests naukri_mock_interview action routing, validation, helper parsing,
polling logic, and error paths. Every test is PURE — no network, no
browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Shared mock data helpers
# ---------------------------------------------------------------------------

def _topics_api_response(topics=None, roles=None):
    """Build a typical topics API response (with data envelope)."""
    if topics is None:
        topics = [
            {"topicId": "101", "topicName": "Python", "status": "active", "testDoneCount": 2, "freeTopic": True},
            {"topicId": "102", "topicName": "System Design", "status": "active", "testDoneCount": 0, "freeTopic": False},
        ]
    if roles is None:
        roles = [
            {"roleId": "r1", "name": "Software Engineer", "active": "Y", "expiryDate": "2026-12-31"},
        ]
    return {
        "statusCode": "0",
        "message": "Successful",
        "data": {"topics": topics, "roleInfo": roles},
    }


def _history_api_response(interviews=None, total=None):
    """Build a typical history API response (with data envelope)."""
    if interviews is None:
        interviews = [
            {"testId": "t1", "topicName": "Python", "score": 85},
            {"testId": "t2", "topicName": "Java", "score": 70},
        ]
    count = len(interviews)
    return {
        "statusCode": "0",
        "message": "Successful",
        "data": {
            "previousInterview": interviews,
            "interviewCount": total if total is not None else count,
        },
    }


def _session_api_response(topic_id="topic1", topic_name="Python", test_id="test1", job_id="12345"):
    """Build a session creation response for _start_interview."""
    return {
        "statusCode": "0",
        "message": "Successful",
        "data": {
            "topics": [
                {
                    "topicId": topic_id,
                    "topicName": topic_name,
                    "testMetaData": [{"testId": test_id}],
                }
            ],
            "companyDetails": [
                {"jobId": job_id, "companyName": "Acme Corp", "title": "Python Developer"},
            ],
        },
    }


def _question_done_response(q_id="q1", q_text="What is GIL?", order=1):
    """Build a DONE question response."""
    return {
        "data": {
            "status": "DONE",
            "question": {
                "id": q_id,
                "question": q_text,
                "questionOrder": order,
                "aiAnswer": "GIL is the Global Interpreter Lock.",
            },
        }
    }


def _question_generating_response():
    """Build a still-generating question response (status not DONE)."""
    return {"data": {"status": "GENERATING"}}


def _question_complete_response():
    """Build a testStatus=COMPLETE response."""
    return {"data": {"testStatus": "COMPLETE"}}


# ---------------------------------------------------------------------------
# Tests: action routing — topics
# ---------------------------------------------------------------------------


class TestActionTopics:
    """naukri_mock_interview(action='topics') routes to _get_topics."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.get", new_callable=AsyncMock)
    async def test_topics_success(self, mock_api_get):
        """Successful topics fetch returns status=success with topics and roles."""
        resp = _topics_api_response()
        mock_api_get.return_value = resp

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="topics")

        assert result["status"] == "success"
        assert result["total"] == 2
        assert result["count"] == 2
        assert result["page"] == 1
        assert result["has_more"] is False
        # Check first topic
        t = result["topics"][0]
        assert t["id"] == "101"
        assert t["name"] == "Python"
        assert t["tests_done"] == 2
        assert t["free"] is True
        # Check role
        r = result["roles"][0]
        assert r["id"] == "r1"
        assert r["name"] == "Software Engineer"
        assert r["active"] == "Y"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.get", new_callable=AsyncMock)
    async def test_topics_parses_data_envelope(self, mock_api_get):
        """Response wrapped in data envelope is unwrapped before parsing topics."""
        # data envelope wraps the list directly (no 'topics' key inside data)
        mock_api_get.side_effect = [
            {"statusCode": "0", "data": [{"topicId": "999", "topicName": "Go", "freeTopic": False}]},
            {"statusCode": "0", "data": {"roleInfo": []}},
        ]

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="topics")

        assert result["status"] == "success"
        assert result["total"] == 1
        assert result["topics"][0]["name"] == "Go"
        assert result["topics"][0]["id"] == "999"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.get", new_callable=AsyncMock)
    async def test_topics_api_error_returns_error(self, mock_api_get):
        """NaukriAPIError from api_get is caught and returned as status=error."""
        from naukri_server.api import NaukriAPIError
        mock_api_get.side_effect = NaukriAPIError(401, "Unauthorized")

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="topics")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 401

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.get", new_callable=AsyncMock)
    async def test_topics_generic_exception_returns_error(self, mock_api_get):
        """Generic exception from api_get is caught and returned as status=error."""
        mock_api_get.side_effect = RuntimeError("connection refused")

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="topics")

        assert result["status"] == "error"
        assert result["error_code"] == "INTERNAL_ERROR"
        assert "RuntimeError" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.get", new_callable=AsyncMock)
    async def test_topics_string_items_in_list(self, mock_api_get):
        """String items in topics list are handled gracefully (name set, no id)."""
        mock_api_get.side_effect = [
            {"statusCode": "0", "data": {"topics": ["Python", "Java"]}},
            {"statusCode": "0", "data": {"roleInfo": []}},
        ]

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="topics")

        assert result["status"] == "success"
        assert result["total"] == 2
        assert result["topics"][0]["name"] == "Python"
        assert result["topics"][0]["status"] == ""


# ---------------------------------------------------------------------------
# Tests: action routing — history
# ---------------------------------------------------------------------------


class TestActionHistory:
    """naukri_mock_interview(action='history') routes to _get_history."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_history_success(self, mock_api_post):
        """History fetch returns status=success with interview list."""
        mock_api_post.return_value = _history_api_response()

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="history")

        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["total"] == 2
        assert result["interview_count"] == 2
        assert result["page"] == 1
        assert result["has_more"] is False
        assert len(result["interviews"]) == 2

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_history_posts_with_pagination_params(self, mock_api_post):
        """History POST is sent with page=1 and pageSize=50 in the body."""
        mock_api_post.return_value = _history_api_response()

        from naukri_server.tools.mock_interview import naukri_mock_interview

        await naukri_mock_interview(action="history")

        mock_api_post.assert_awaited_once()
        call_args = mock_api_post.call_args
        url_arg = call_args[0][0]
        body_arg = call_args[1]["body"]
        assert "detailedView=false" in url_arg
        assert body_arg == {"page": 1, "pageSize": 50}

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_history_api_error(self, mock_api_post):
        """NaukriAPIError from api_post is caught and returned as error dict."""
        from naukri_server.api import NaukriAPIError
        mock_api_post.side_effect = NaukriAPIError(500, "Server Error")

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="history")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 500

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_history_uses_interviews_fallback_key(self, mock_api_post):
        """Falls back to 'interviews' key if 'previousInterview' is absent."""
        mock_api_post.return_value = {
            "statusCode": "0",
            "data": {
                "interviews": [{"testId": "x1"}],
                "interviewCount": 1,
            },
        }

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="history")

        assert result["status"] == "success"
        assert result["count"] == 1
        assert result["interviews"][0]["testId"] == "x1"


# ---------------------------------------------------------------------------
# Tests: action routing — validation errors
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for validation paths — missing required params."""

    @pytest.mark.asyncio
    async def test_start_requires_job_id(self):
        """action='start' without job_id returns VALIDATION_ERROR."""
        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="start")

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_prep_requires_job_id(self):
        """action='prep' without job_id returns VALIDATION_ERROR."""
        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="prep")

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id" in result["message"]

    @pytest.mark.asyncio
    async def test_answer_requires_all_four_params(self):
        """action='answer' missing any of the four params returns VALIDATION_ERROR."""
        from naukri_server.tools.mock_interview import naukri_mock_interview

        # Missing answer
        result = await naukri_mock_interview(
            action="answer", test_id="t1", topic_id="tp1", question_id="q1"
        )
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_answer_missing_test_id(self):
        """action='answer' missing test_id returns VALIDATION_ERROR."""
        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(
            action="answer", topic_id="tp1", question_id="q1", answer="My answer"
        )
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "answer requires" in result["message"]

    @pytest.mark.asyncio
    async def test_answer_missing_topic_id(self):
        """action='answer' missing topic_id returns VALIDATION_ERROR."""
        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(
            action="answer", test_id="t1", question_id="q1", answer="My answer"
        )
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_answer_missing_question_id(self):
        """action='answer' missing question_id returns VALIDATION_ERROR."""
        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(
            action="answer", test_id="t1", topic_id="tp1", answer="My answer"
        )
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        """Unknown action string returns VALIDATION_ERROR with action name in message."""
        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="fly")

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "fly" in result["message"]
        # Hint message should mention valid actions
        assert "topics" in result["message"]


# ---------------------------------------------------------------------------
# Tests: action routing — start
# ---------------------------------------------------------------------------


class TestActionStart:
    """Tests for action='start' — session creation and polling."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_start_success_first_attempt(self, mock_api_post):
        """Start succeeds when question is DONE on the first polling attempt."""
        session_resp = _session_api_response()
        question_resp = _question_done_response()
        mock_api_post.side_effect = [session_resp, question_resp]

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="start", job_id="12345")

        assert result["status"] == "success"
        assert result["test_id"] == "test1"
        assert result["topic_id"] == "topic1"
        assert result["topic_name"] == "Python"
        assert result["question"]["id"] == "q1"
        assert result["question"]["text"] == "What is GIL?"
        assert result["question"]["order"] == 1
        assert result["company_details"]["company"] == "Acme Corp"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.asyncio.sleep", new_callable=AsyncMock)
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_start_polls_until_done(self, mock_api_post, mock_sleep):
        """Start retries up to 5 times and returns success when DONE on 3rd attempt."""
        session_resp = _session_api_response()
        gen1 = _question_generating_response()
        gen2 = _question_generating_response()
        done = _question_done_response(q_id="q99", q_text="What is a decorator?", order=2)
        mock_api_post.side_effect = [session_resp, gen1, gen2, done]

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="start", job_id="12345")

        assert result["status"] == "success"
        assert result["question"]["id"] == "q99"
        # sleep should have been called twice (once per generating response)
        assert mock_sleep.await_count == 2

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.asyncio.sleep", new_callable=AsyncMock)
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_start_timeout_returns_generating(self, mock_api_post, mock_sleep):
        """Start returns status=generating after 5 failed polling attempts."""
        session_resp = _session_api_response()
        gen = _question_generating_response()
        # Session call + 5 generating poll calls
        mock_api_post.side_effect = [session_resp] + [gen] * 5

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="start", job_id="12345")

        assert result["status"] == "generating"
        assert result["test_id"] == "test1"
        assert result["topic_id"] == "topic1"
        assert "Retry" in result["message"]
        # Must have slept exactly 5 times (one per poll attempt)
        assert mock_sleep.await_count == 5

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_start_session_api_error_code(self, mock_api_post):
        """Non-zero statusCode on session creation returns error."""
        mock_api_post.return_value = {
            "statusCode": "1",
            "message": "Job not found",
            "data": {},
        }

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="start", job_id="99999")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Job not found" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_start_no_topics_returns_error(self, mock_api_post):
        """Empty topics list in session response returns error."""
        mock_api_post.return_value = {
            "statusCode": "0",
            "data": {"topics": [], "companyDetails": []},
        }

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="start", job_id="12345")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "No interview topics" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_start_missing_test_id_returns_error(self, mock_api_post):
        """Missing testId in testMetaData returns error."""
        mock_api_post.return_value = {
            "statusCode": "0",
            "data": {
                "topics": [
                    {"topicId": "tp1", "topicName": "Go", "testMetaData": [{}]},
                ],
                "companyDetails": [],
            },
        }

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="start", job_id="12345")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "test/topic IDs" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_start_naukri_api_error_caught(self, mock_api_post):
        """NaukriAPIError during _start_interview is caught and returned as error."""
        from naukri_server.api import NaukriAPIError
        mock_api_post.side_effect = NaukriAPIError(403, "Forbidden")

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="start", job_id="12345")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 403


# ---------------------------------------------------------------------------
# Tests: action routing — answer
# ---------------------------------------------------------------------------


class TestActionAnswer:
    """Tests for action='answer' — submitting answers and polling."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_answer_returns_next_question(self, mock_api_post):
        """Submitting an answer returns the next question when status=DONE and question.id present."""
        mock_api_post.return_value = _question_done_response(q_id="q2", q_text="Explain decorators.")

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(
            action="answer",
            test_id="t1",
            topic_id="tp1",
            question_id="q1",
            answer="GIL prevents threads.",
        )

        assert result["status"] == "next_question"
        assert result["question"]["id"] == "q2"
        assert result["question"]["text"] == "Explain decorators."

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_answer_test_status_complete(self, mock_api_post):
        """testStatus=COMPLETE terminates the session with status=complete."""
        mock_api_post.return_value = _question_complete_response()

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(
            action="answer",
            test_id="t1",
            topic_id="tp1",
            question_id="q5",
            answer="Final answer.",
        )

        assert result["status"] == "complete"
        assert "complete" in result["message"].lower()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_answer_done_no_question_id_complete(self, mock_api_post):
        """DONE status with no question.id means no more questions — returns complete."""
        mock_api_post.return_value = {
            "data": {
                "status": "DONE",
                "question": {"id": None, "question": "", "questionOrder": 0, "aiAnswer": ""},
            }
        }

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(
            action="answer",
            test_id="t1",
            topic_id="tp1",
            question_id="q5",
            answer="Final answer.",
        )

        assert result["status"] == "complete"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.asyncio.sleep", new_callable=AsyncMock)
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_answer_polls_until_done(self, mock_api_post, mock_sleep):
        """Polls up to 5 times and returns next_question when DONE arrives on 2nd attempt."""
        gen = _question_generating_response()
        done = _question_done_response(q_id="q3", q_text="What is async/await?")
        mock_api_post.side_effect = [gen, done]

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(
            action="answer",
            test_id="t1",
            topic_id="tp1",
            question_id="q2",
            answer="Some answer.",
        )

        assert result["status"] == "next_question"
        assert result["question"]["id"] == "q3"
        assert mock_sleep.await_count == 1

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.asyncio.sleep", new_callable=AsyncMock)
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_answer_timeout_returns_generating(self, mock_api_post, mock_sleep):
        """Returns status=generating after 5 failed polling attempts."""
        gen = _question_generating_response()
        mock_api_post.side_effect = [gen] * 5

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(
            action="answer",
            test_id="t1",
            topic_id="tp1",
            question_id="q2",
            answer="Some answer.",
        )

        assert result["status"] == "generating"
        assert "Retry" in result["message"]
        assert mock_sleep.await_count == 5

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview.api_client.post", new_callable=AsyncMock)
    async def test_answer_naukri_api_error(self, mock_api_post):
        """NaukriAPIError during answer is caught and returned as error."""
        from naukri_server.api import NaukriAPIError
        mock_api_post.side_effect = NaukriAPIError(500, "Internal error")

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(
            action="answer",
            test_id="t1",
            topic_id="tp1",
            question_id="q1",
            answer="test",
        )

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 500


# ---------------------------------------------------------------------------
# Tests: action routing — prep
# ---------------------------------------------------------------------------


class TestActionPrep:
    """Tests for action='prep' — parallel fetch of job/interviews/reviews/topics.

    _interview_prep uses local imports inside the function body:
      from naukri_server.tools.jobs import naukri_get_job
      from naukri_server.tools.ambitionbox import _fetch_interviews, _fetch_reviews
      from naukri_server.utils import derive_slug

    Therefore we patch at the SOURCE modules, not at mock_interview.
    """

    def _mock_job_result(self, company="Acme Corp"):
        return {
            "status": "success",
            "title": "Senior Python Dev",
            "company": company,
            "salary": "20-30 LPA",
            "experience": "5-8 years",
            "location": "Bangalore",
            "skills": ["Python", "Django", "Docker"],
        }

    def _mock_interviews_result(self):
        return {
            "status": "success",
            "total_interviews": 50,
            "overall_difficulty": "Moderate",
            "interview_experiences": [
                {"questions": ["Tell me about yourself", "System design"], "likes": "Good"},
                {"questions": ["OOP concepts"], "likes": "Structured"},
            ],
        }

    def _mock_reviews_result(self):
        return {
            "status": "success",
            "overall_rating": 4.1,
            "category_ratings": {"Work Life Balance": 3.8},
        }

    def _mock_topics_result(self):
        return {
            "status": "success",
            "topics": [
                {"name": "Python", "id": "101"},
                {"name": "System Design", "id": "102"},
                {"name": "Django", "id": "103"},
                {"name": "Docker", "id": "104"},
                {"name": "REST APIs", "id": "105"},
                {"name": "Extra", "id": "106"},  # beyond top-5
            ],
        }

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview._get_topics", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    @patch("naukri_server.utils.derive_slug")
    async def test_prep_full_success(
        self, mock_slug, mock_get_job, mock_interviews, mock_reviews, mock_topics
    ):
        """Full prep returns status=success with all sections populated."""
        mock_slug.return_value = "acme-corp"
        mock_get_job.return_value = self._mock_job_result()
        mock_interviews.return_value = self._mock_interviews_result()
        mock_reviews.return_value = self._mock_reviews_result()
        mock_topics.return_value = self._mock_topics_result()

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="prep", job_id="12345")

        assert result["status"] == "success"
        assert result["job_id"] == "12345"

        # Job summary
        js = result["job_summary"]
        assert js["title"] == "Senior Python Dev"
        assert js["company"] == "Acme Corp"
        assert js["salary"] == "20-30 LPA"
        assert "Python" in js["skills"]

        # Company interviews
        ci = result["company_interviews"]
        assert ci["total_interviews"] == 50
        assert ci["overall_difficulty"] == "Moderate"
        assert len(ci["sample_experiences"]) <= 3  # capped at 3
        assert len(ci["sample_questions"]) <= 8    # capped at 8

        # Company reviews
        cr = result["company_reviews"]
        assert cr["overall_rating"] == 4.1

        # Mock interview topics — capped at top 5
        assert len(result["mock_interview_topics"]) == 5

        # Prep guide
        pg = result["preparation_guide"]
        assert len(pg["key_skills"]) <= 10
        assert pg["difficulty"] == "Moderate"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview._get_topics", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_prep_job_fetch_failure_returns_error(
        self, mock_get_job, mock_interviews, mock_reviews, mock_topics
    ):
        """If naukri_get_job fails, prep returns error immediately."""
        mock_get_job.return_value = {"status": "error", "message": "Job not found"}

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="prep", job_id="99999")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "Failed to fetch job" in result["message"]
        assert "Job not found" in result["message"]
        # Downstream calls should NOT have been made
        mock_interviews.assert_not_awaited()
        mock_reviews.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview._get_topics", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    async def test_prep_no_company_name_skips_ambitionbox(
        self, mock_get_job, mock_interviews, mock_reviews, mock_topics
    ):
        """When company name is empty, AmbitionBox calls are skipped; partial_success returned."""
        mock_get_job.return_value = {
            "status": "success",
            "title": "Dev",
            "company": "",
            "salary": "",
            "experience": "",
            "location": "",
            "skills": [],
        }
        mock_topics.return_value = self._mock_topics_result()

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="prep", job_id="12345")

        assert result["status"] == "partial_success"
        assert "errors" in result
        assert any("No company name" in e for e in result["errors"])
        assert result["company_interviews"] is None
        assert result["company_reviews"] is None
        # Topics still fetched
        assert len(result["mock_interview_topics"]) == 5

    @pytest.mark.asyncio
    @patch("naukri_server.tools.mock_interview._get_topics", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_reviews", new_callable=AsyncMock)
    @patch("naukri_server.tools.ambitionbox._fetch_interviews", new_callable=AsyncMock)
    @patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock)
    @patch("naukri_server.utils.derive_slug")
    async def test_prep_ambitionbox_exceptions_handled(
        self, mock_slug, mock_get_job, mock_interviews, mock_reviews, mock_topics
    ):
        """Exceptions in interviews/reviews parallel calls are caught; partial_success returned."""
        mock_slug.return_value = "acme-corp"
        mock_get_job.return_value = self._mock_job_result()
        mock_interviews.side_effect = RuntimeError("timeout")
        mock_reviews.side_effect = RuntimeError("timeout")
        mock_topics.return_value = self._mock_topics_result()

        from naukri_server.tools.mock_interview import naukri_mock_interview

        result = await naukri_mock_interview(action="prep", job_id="12345")

        # Should not crash; errors are accumulated
        assert result["status"] in ("success", "partial_success")
        assert result["company_interviews"] is None
        assert result["company_reviews"] is None

    @pytest.mark.asyncio
    async def test_prep_missing_job_id_inline_validation(self):
        """_interview_prep called with empty string triggers its own validation error."""
        from naukri_server.tools.mock_interview import _interview_prep

        result = await _interview_prep("")

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "job_id required" in result["message"]
