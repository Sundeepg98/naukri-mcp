"""Unit tests for naukri_server.domain.parsed_job — applied/saved job factories.

Also exercises naukri_server.domain.application_detail.from_api_response,
which is a sibling factory and shares the same defensive-parsing pattern.

All tests are PURE: no network, no browser, no file I/O.
"""

import pytest

from naukri_server.domain.parsed_job import (
    APPLIED_ROOT_KEYS,
    SAVED_ROOT_KEYS,
    find_job_array,
    parse_applied_job,
    parse_saved_job,
)
from naukri_server.domain.application_detail import from_api_response


def _identity_status(s):
    """Status mapper that returns the input unchanged (test-friendly default)."""
    return s


# ---------------------------------------------------------------------------
# 1. find_job_array — root key extraction
# ---------------------------------------------------------------------------

class TestFindJobArray:
    def test_raw_list_returned_as_is(self):
        result = find_job_array([{"jobId": "1"}], APPLIED_ROOT_KEYS, "test")
        assert result == [{"jobId": "1"}]

    def test_finds_first_matching_key(self):
        data = {"applyDetails": [{"jobId": "1"}], "appliedJobs": [{"jobId": "2"}]}
        result = find_job_array(data, APPLIED_ROOT_KEYS, "test")
        # APPLIED_ROOT_KEYS lists applyDetails first → that wins
        assert result == [{"jobId": "1"}]

    def test_falls_back_to_later_keys(self):
        data = {"results": [{"jobId": "5"}]}
        result = find_job_array(data, APPLIED_ROOT_KEYS, "test")
        assert result == [{"jobId": "5"}]

    def test_returns_empty_when_no_keys_match(self):
        data = {"random_field": "value"}
        result = find_job_array(data, APPLIED_ROOT_KEYS, "test")
        assert result == []

    def test_returns_empty_for_non_dict_non_list(self):
        result = find_job_array("not a dict", APPLIED_ROOT_KEYS, "test")
        assert result == []

    def test_ignores_non_list_values_under_known_keys(self):
        """Key matches but value is dict — falls through to next key."""
        data = {"applyDetails": {"x": 1}, "appliedJobs": [{"jobId": "ok"}]}
        result = find_job_array(data, APPLIED_ROOT_KEYS, "test")
        assert result == [{"jobId": "ok"}]


# ---------------------------------------------------------------------------
# 2. parse_applied_job — happy path + defensive parsing
# ---------------------------------------------------------------------------

class TestParseAppliedJob:
    def test_happy_path(self):
        item = {
            "jobId": "JOB123",
            "jobTitle": "Senior Developer",
            "company": "Acme Corp",
            "statusMsg": "applied",
            "appliedDate": "2026-01-01",
            "salary": "20-30 LPA",
            "location": "Bengaluru",
            "jdUrl": "https://naukri.com/job/JOB123",
        }
        result = parse_applied_job(item, _identity_status)
        assert result is not None
        assert result["job_id"] == "JOB123"
        assert result["title"] == "Senior Developer"
        assert result["company"] == "Acme Corp"
        assert result["status"] == "applied"
        assert result["applied_date"] == "2026-01-01"
        assert result["salary"] == "20-30 LPA"
        assert result["location"] == "Bengaluru"
        assert result["url"] == "https://naukri.com/job/JOB123"

    def test_returns_none_for_non_dict(self):
        assert parse_applied_job("not a dict", _identity_status) is None
        assert parse_applied_job(None, _identity_status) is None
        assert parse_applied_job([], _identity_status) is None

    def test_returns_none_when_no_job_id(self):
        result = parse_applied_job({"jobTitle": "x"}, _identity_status)
        assert result is None

    def test_falls_back_to_alt_id_keys(self):
        # job_id (snake_case) instead of jobId
        result = parse_applied_job({"job_id": "X"}, _identity_status)
        assert result is not None
        assert result["job_id"] == "X"

        # plain id
        result = parse_applied_job({"id": "Y"}, _identity_status)
        assert result is not None
        assert result["job_id"] == "Y"

    def test_falls_back_to_alt_title_keys(self):
        result = parse_applied_job({"jobId": "1", "title": "via title"}, _identity_status)
        assert result["title"] == "via title"

        result = parse_applied_job({"jobId": "2", "designation": "via designation"}, _identity_status)
        assert result["title"] == "via designation"

    def test_falls_back_to_alt_company_keys(self):
        result = parse_applied_job({"jobId": "1", "companyName": "Alt"}, _identity_status)
        assert result["company"] == "Alt"

    def test_status_list_flattened_to_first_status_msg(self):
        """When status is a list of dicts, take the first one's statusMsg."""
        item = {
            "jobId": "1",
            "status": [{"statusMsg": "viewed"}, {"statusMsg": "applied"}],
        }
        result = parse_applied_job(item, _identity_status)
        assert result["status"] == "viewed"

    def test_view_count_extracted_from_status_id_4(self):
        """statusId=4 entries provide view_count and last_viewed."""
        item = {
            "jobId": "1",
            "status": [
                {"statusId": 1, "count": 0},
                {"statusId": 4, "count": 7, "modifiedDate": "2026-02-15"},
            ],
        }
        result = parse_applied_job(item, _identity_status)
        assert result["view_count"] == 7
        assert result["last_viewed"] == "2026-02-15"

    def test_view_count_none_when_no_status_4(self):
        item = {"jobId": "1", "status": [{"statusId": 1}]}
        result = parse_applied_job(item, _identity_status)
        assert result["view_count"] is None

    def test_salary_falls_back_to_salary_detail_label(self):
        """salary missing → use salaryDetail.label."""
        item = {"jobId": "1", "salaryDetail": {"label": "10-15 LPA"}}
        result = parse_applied_job(item, _identity_status)
        assert result["salary"] == "10-15 LPA"

    def test_is_open_string_to_bool_conversion(self):
        item = {"jobId": "1", "isOpen": "true"}
        result = parse_applied_job(item, _identity_status)
        assert result["is_open"] is True

        item = {"jobId": "1", "isOpen": "false"}
        result = parse_applied_job(item, _identity_status)
        assert result["is_open"] is False

        item = {"jobId": "1"}  # missing
        result = parse_applied_job(item, _identity_status)
        assert result["is_open"] is None

    def test_status_mapper_is_called(self):
        """The map_status_fn is applied to raw_status."""
        seen = []

        def custom_mapper(raw):
            seen.append(raw)
            return "MAPPED"

        item = {"jobId": "1", "statusMsg": "raw_status"}
        result = parse_applied_job(item, custom_mapper)
        assert result["status"] == "MAPPED"
        assert "raw_status" in seen


# ---------------------------------------------------------------------------
# 3. parse_saved_job — happy path + defensive parsing
# ---------------------------------------------------------------------------

class TestParseSavedJob:
    def test_happy_path(self):
        item = {
            "jobId": "S1",
            "title": "Backend Engineer",
            "companyName": "FooCo",
            "savedDate": "2026-03-01",
            "salary": "15 LPA",
            "location": "Remote",
            "url": "https://naukri.com/saved/S1",
        }
        result = parse_saved_job(item)
        assert result is not None
        assert result["job_id"] == "S1"
        assert result["title"] == "Backend Engineer"
        assert result["company"] == "FooCo"
        assert result["saved_date"] == "2026-03-01"
        assert result["salary"] == "15 LPA"
        assert result["location"] == "Remote"
        assert result["url"] == "https://naukri.com/saved/S1"

    def test_returns_none_for_non_dict(self):
        assert parse_saved_job(None) is None
        assert parse_saved_job("not dict") is None

    def test_returns_none_when_no_job_id(self):
        assert parse_saved_job({"title": "x"}) is None

    def test_falls_back_to_alt_keys(self):
        # title via jobTitle
        result = parse_saved_job({"jobId": "1", "jobTitle": "alt"})
        assert result["title"] == "alt"

        # company via "company"
        result = parse_saved_job({"jobId": "1", "company": "via company"})
        assert result["company"] == "via company"

        # saved_date via createdDate
        result = parse_saved_job({"jobId": "1", "createdDate": "2026-01-01"})
        assert result["saved_date"] == "2026-01-01"

        # location via cityName
        result = parse_saved_job({"jobId": "1", "cityName": "Mumbai"})
        assert result["location"] == "Mumbai"

        # url via jdUrl
        result = parse_saved_job({"jobId": "1", "jdUrl": "https://naukri.com/x"})
        assert result["url"] == "https://naukri.com/x"


# ---------------------------------------------------------------------------
# 4. application_detail.from_api_response — full ApplicationDetail factory
# ---------------------------------------------------------------------------

class TestFromApiResponse:
    def test_minimal_happy_path(self):
        data = {
            "jobDetails": {
                "jobTitle": "Dev",
                "company": "Acme",
                "location": "Bengaluru",
                "isOpen": True,
            },
            "status": [],
            "applicationDate": "2026-01-15",
            "currentStatus": "applied",
        }
        result = from_api_response(data, "JOB001")
        assert result["status"] == "success"
        assert result["job_id"] == "JOB001"
        assert result["title"] == "Dev"
        assert result["company"] == "Acme"
        assert result["application_date"] == "2026-01-15"
        assert result["current_status"] == "applied"

    def test_strips_none_values(self):
        """Fields with None value are stripped from the result."""
        data = {"jobDetails": {"jobTitle": "Dev"}}
        result = from_api_response(data, "JOB002")
        # company is None → not in result
        assert "company" not in result
        # status key always present (re-added after strip)
        assert result["status"] == "success"

    def test_status_timeline_parsed(self):
        data = {
            "jobDetails": {"jobTitle": "Dev"},
            "status": [
                {"status": "Applied", "date": "2026-01-01", "isCompleted": True, "stepOrder": 1},
                {"label": "Viewed", "isCurrent": True, "stepOrder": 2},
            ],
        }
        result = from_api_response(data, "JOB003")
        timeline = result["status_timeline"]
        assert len(timeline) == 2
        assert timeline[0]["status"] == "Applied"
        assert timeline[0]["date"] == "2026-01-01"
        assert timeline[0]["is_completed"] is True
        assert timeline[0]["step_order"] == 1
        # Second entry uses 'label' fallback for status
        assert timeline[1]["status"] == "Viewed"
        assert timeline[1]["is_current"] is True

    def test_screening_questions_parsed_from_first_key(self):
        data = {
            "jobDetails": {},
            "screeningQuestions": [
                {"question": "Years of experience?", "answer": "5", "questionId": "q1",
                 "questionType": "text", "isMandatory": True},
            ],
        }
        result = from_api_response(data, "J")
        sqs = result.get("screening_questions")
        assert sqs is not None
        assert len(sqs) == 1
        assert sqs[0]["question"] == "Years of experience?"
        assert sqs[0]["answer"] == "5"
        assert sqs[0]["question_id"] == "q1"
        assert sqs[0]["is_mandatory"] is True

    def test_screening_questions_fallback_keys(self):
        """Falls back through questionnaire / screeningResponses."""
        data = {
            "jobDetails": {},
            "questionnaire": [{"questionText": "Q?", "response": "A"}],
        }
        result = from_api_response(data, "J")
        assert result["screening_questions"][0]["question"] == "Q?"
        assert result["screening_questions"][0]["answer"] == "A"

    def test_no_screening_questions_omitted(self):
        """If screening data missing, key is not in result."""
        data = {"jobDetails": {}}
        result = from_api_response(data, "J")
        assert "screening_questions" not in result

    def test_recruiter_parsed_with_fallbacks(self):
        data = {
            "jobDetails": {},
            "recruiterDetails": {
                "name": "Jane Recruiter",
                "designation": "Senior HR",
                "company": "Recruiter Co",
            },
        }
        result = from_api_response(data, "J")
        rec = result.get("recruiter")
        assert rec is not None
        assert rec["name"] == "Jane Recruiter"
        assert rec["designation"] == "Senior HR"

    def test_recruiter_omitted_when_missing(self):
        data = {"jobDetails": {}}
        result = from_api_response(data, "J")
        assert "recruiter" not in result

    def test_recruiter_alt_keys(self):
        """Tries data.recruiter, jobDetails.recruiterDetails, jobDetails.recruiter in order."""
        data = {
            "jobDetails": {"recruiter": {"recruiterName": "Bob", "title": "Manager"}},
        }
        result = from_api_response(data, "J")
        rec = result["recruiter"]
        assert rec["name"] == "Bob"
        assert rec["designation"] == "Manager"

    def test_company_rating_parsed(self):
        data = {
            "jobDetails": {},
            "companyRating": {"Rating": 4.2, "ReviewsCount": 1500},
        }
        result = from_api_response(data, "J")
        cr = result["company_rating"]
        assert cr["rating"] == 4.2
        assert cr["reviews"] == 1500

    def test_company_rating_omitted_when_missing(self):
        data = {"jobDetails": {}}
        result = from_api_response(data, "J")
        assert "company_rating" not in result

    def test_application_date_fallback_chain(self):
        """applicationDate → appliedDate → applyDate fallback."""
        result = from_api_response(
            {"jobDetails": {}, "appliedDate": "2026-02-01"}, "J"
        )
        assert result["application_date"] == "2026-02-01"

        result = from_api_response(
            {"jobDetails": {}, "applyDate": "2026-03-01"}, "J"
        )
        assert result["application_date"] == "2026-03-01"
