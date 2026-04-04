"""Deep tests for naukri_server.tools.job_parsing — _parse_job_list comprehensive coverage
including salary, location, tags, URL, AmbitionBox, consultant, diversity, vacancy, work_mode.

Also includes _bulk_fetch_jobs, _get_job_v1 tests from jobs module.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier24_job_parsing.py.
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.tools.job_parsing import _parse_job_list
from naukri_server.config import NAUKRI_BASE


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_job(**overrides) -> dict:
    """Return a minimal job dict with all common fields populated."""
    base = {
        "jobId": "JOB123",
        "title": "Software Engineer",
        "companyName": "Acme Corp",
        "salaryDetail": {
            "minimumSalary": 1_000_000,
            "maximumSalary": 2_000_000,
            "label": "",
            "hideSalary": False,
        },
        "placeholders": [{"type": "location", "label": "Bangalore"}],
        "minimumExperience": 2,
        "maximumExperience": 5,
        "isApplied": False,
        "createdDate": "2026-03-01",
        "tagsAndSkills": "Python, Django, REST API",
        "jdURL": "/job-listings-software-engineer-acme-JOB123",
        "vacancy": 3,
        "workMode": "Work from office",
        "ambitionBoxData": {"Rating": 4.2, "ReviewsCount": 500},
        "consultant": False,
        "hiringFor": None,
        "clientTitleString": None,
        "diversityTagText": None,
        "experienceText": "2-5 Yrs",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Basic parsing
# ---------------------------------------------------------------------------

class TestBasicParsing:
    def test_basic_all_fields(self):
        jobs = _parse_job_list([_make_job()], limit=10)
        assert len(jobs) == 1
        j = jobs[0]
        assert j["job_id"] == "JOB123"
        assert j["title"] == "Software Engineer"
        assert j["company"] == "Acme Corp"
        assert j["location"] == "Bangalore"
        assert j["experience"] == "2-5 Yrs"
        assert j["experience_min"] == 2
        assert j["experience_max"] == 5
        assert j["is_applied"] is False
        assert j["posted_date"] == "2026-03-01"
        assert j["vacancies"] == 3
        assert j["work_mode"] == "Work from office"

    def test_limit_respected(self):
        jobs_input = [_make_job(jobId=f"J{i}") for i in range(10)]
        assert len(_parse_job_list(jobs_input, limit=3)) == 3

    def test_empty_job_details_list(self):
        assert _parse_job_list([], limit=10) == []


# ---------------------------------------------------------------------------
# 2. Salary parsing
# ---------------------------------------------------------------------------

class TestSalaryParsing:
    def test_salary_from_min_max(self):
        job = _make_job(salaryDetail={"minimumSalary": 500_000, "maximumSalary": 1_000_000, "label": "", "hideSalary": False})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary"] == "5.0-10.0 LPA"
        assert j["salary_min_lakhs"] == 5.0
        assert j["salary_max_lakhs"] == 10.0

    def test_salary_label_fallback(self):
        job = _make_job(salaryDetail={"minimumSalary": 0, "maximumSalary": 0, "label": "Not Disclosed", "hideSalary": True})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary"] == "Not Disclosed"

    def test_salary_not_disclosed_when_max_zero(self):
        job = _make_job(salaryDetail={"minimumSalary": 0, "maximumSalary": 0, "label": "", "hideSalary": False})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary"] == "Not Disclosed"

    def test_salary_raw_fields_populated(self):
        job = _make_job(salaryDetail={"minimumSalary": 800_000, "maximumSalary": 1_500_000, "label": "", "hideSalary": True})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary_min_raw"] == 800_000
        assert j["salary_max_raw"] == 1_500_000
        assert j["salary_hidden"] is True

    def test_salary_min_lakhs_none_when_zero(self):
        job = _make_job(salaryDetail={"minimumSalary": 0, "maximumSalary": 0, "label": "", "hideSalary": False})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary_min_lakhs"] is None
        assert j["salary_max_lakhs"] is None


# ---------------------------------------------------------------------------
# 3. Location parsing
# ---------------------------------------------------------------------------

class TestLocationParsing:
    def test_location_from_type_location(self):
        job = _make_job(placeholders=[
            {"type": "experience", "label": "2-5 Yrs"},
            {"type": "location", "label": "Mumbai"},
        ])
        assert _parse_job_list([job], limit=10)[0]["location"] == "Mumbai"

    def test_location_first_placeholder_fallback(self):
        job = _make_job(placeholders=[
            {"type": "experience", "label": "3-6 Yrs"},
            {"type": "salary", "label": "8-12 LPA"},
        ])
        assert _parse_job_list([job], limit=10)[0]["location"] == "3-6 Yrs"

    def test_location_none_when_no_placeholders(self):
        job = _make_job(placeholders=[])
        assert _parse_job_list([job], limit=10)[0]["location"] is None


# ---------------------------------------------------------------------------
# 4. Tags parsing
# ---------------------------------------------------------------------------

class TestTagsParsing:
    def test_tags_from_comma_separated_string(self):
        job = _make_job(tagsAndSkills="Python, Django, REST API")
        assert _parse_job_list([job], limit=10)[0]["tags"] == ["Python", "Django", "REST API"]

    def test_tags_from_list_type(self):
        job = _make_job(tagsAndSkills=["Python", "Go", "Kubernetes"])
        assert _parse_job_list([job], limit=10)[0]["tags"] == ["Python", "Go", "Kubernetes"]

    def test_tags_empty_string(self):
        job = _make_job(tagsAndSkills="")
        assert _parse_job_list([job], limit=10)[0]["tags"] == []


# ---------------------------------------------------------------------------
# 5. URL construction
# ---------------------------------------------------------------------------

class TestURLConstruction:
    def test_url_with_jdurl(self):
        job = _make_job(jdURL="/job-listings-se-acme-JOB123")
        assert _parse_job_list([job], limit=10)[0]["url"] == f"{NAUKRI_BASE}/job-listings-se-acme-JOB123"

    def test_url_fallback_job_id(self):
        job = _make_job()
        del job["jdURL"]
        assert _parse_job_list([job], limit=10)[0]["url"] == f"{NAUKRI_BASE}/job-listings-JOB123"

    def test_jd_url_field(self):
        job = _make_job(jdURL="/job-listings-se-acme-JOB123")
        assert _parse_job_list([job], limit=10)[0]["jd_url"] == "/job-listings-se-acme-JOB123"


# ---------------------------------------------------------------------------
# 6. AmbitionBox data
# ---------------------------------------------------------------------------

class TestAmbitionBoxData:
    def test_rating_from_rating_key(self):
        job = _make_job(ambitionBoxData={"Rating": 4.1, "AggregateRating": 3.9, "ReviewsCount": 100})
        j = _parse_job_list([job], limit=10)[0]
        assert j["company_rating"] == 4.1
        assert j["company_reviews_count"] == 100

    def test_rating_aggregate_fallback(self):
        job = _make_job(ambitionBoxData={"AggregateRating": 3.8, "ReviewsCount": 50})
        assert _parse_job_list([job], limit=10)[0]["company_rating"] == 3.8

    def test_rating_none_when_missing(self):
        job = _make_job()
        del job["ambitionBoxData"]
        assert _parse_job_list([job], limit=10)[0]["company_rating"] is None


# ---------------------------------------------------------------------------
# 7. Consultant / hiring_for / client fields
# ---------------------------------------------------------------------------

class TestConsultantFields:
    def test_consultant_flag(self):
        job = _make_job(consultant=True, hiringFor="TechCorp", clientTitleString="TechCorp Ltd")
        j = _parse_job_list([job], limit=10)[0]
        assert j["is_consultant"] is True
        assert j["hiring_for"] == "TechCorp"
        assert j["client_company"] == "TechCorp Ltd"

    def test_hiring_for_none_when_empty(self):
        job = _make_job(hiringFor="")
        assert _parse_job_list([job], limit=10)[0]["hiring_for"] is None


# ---------------------------------------------------------------------------
# 8. Diversity tag
# ---------------------------------------------------------------------------

class TestDiversityTag:
    def test_diversity_tag_present(self):
        job = _make_job(diversityTagText="Women-friendly")
        assert _parse_job_list([job], limit=10)[0]["diversity_tag"] == "Women-friendly"

    def test_diversity_tag_none_when_absent(self):
        job = _make_job()
        del job["diversityTagText"]
        assert _parse_job_list([job], limit=10)[0]["diversity_tag"] is None


# ---------------------------------------------------------------------------
# 9. Vacancy fallback (vacancy vs vacany typo)
# ---------------------------------------------------------------------------

class TestVacancyFallback:
    def test_vacancy_from_vacancy_key(self):
        assert _parse_job_list([_make_job(vacancy=5)], limit=10)[0]["vacancies"] == 5

    def test_vacancy_from_vacany_typo(self):
        job = _make_job()
        del job["vacancy"]
        job["vacany"] = 7
        assert _parse_job_list([job], limit=10)[0]["vacancies"] == 7

    def test_vacancy_none_when_both_absent(self):
        job = _make_job()
        del job["vacancy"]
        assert _parse_job_list([job], limit=10)[0]["vacancies"] is None


# ---------------------------------------------------------------------------
# 10. work_mode from wfhType fallback
# ---------------------------------------------------------------------------

class TestWorkModeFallback:
    def test_work_mode_from_workMode(self):
        assert _parse_job_list([_make_job(workMode="Hybrid")], limit=10)[0]["work_mode"] == "Hybrid"

    def test_work_mode_from_wfhType(self):
        job = _make_job()
        del job["workMode"]
        job["wfhType"] = "Remote"
        assert _parse_job_list([job], limit=10)[0]["work_mode"] == "Remote"

    def test_work_mode_none_when_both_absent(self):
        job = _make_job()
        del job["workMode"]
        assert _parse_job_list([job], limit=10)[0]["work_mode"] is None


# ---------------------------------------------------------------------------
# 11. Enrichment fields together
# ---------------------------------------------------------------------------

class TestJobListParserEnrichment:
    def test_all_enrichment_fields_together(self):
        jobs_raw = [{
            "jobId": "42", "title": "Full Stack Dev", "companyName": "TechCorp",
            "hiringFor": "Amazon", "diversityTagText": "LGBTQ+ Friendly",
            "experienceText": "2-4 Yrs",
            "salaryDetail": {"minimumSalary": 800000, "maximumSalary": 1600000, "label": "8-16 LPA", "hideSalary": False},
            "placeholders": [{"type": "location", "label": "Hyderabad"}],
        }]
        j = _parse_job_list(jobs_raw, 10)[0]
        assert j["hiring_for"] == "Amazon"
        assert j["diversity_tag"] == "LGBTQ+ Friendly"
        assert j["experience_text"] == "2-4 Yrs"
        assert j["salary_min_raw"] == 800000
        assert j["salary_hidden"] is False

    def test_empty_salary_detail_still_adds_raw_fields(self):
        jobs_raw = [{"jobId": "1", "salaryDetail": {}}]
        j = _parse_job_list(jobs_raw, 10)[0]
        assert j["salary_min_raw"] is None
        assert j["salary_max_raw"] is None
        assert j["salary_hidden"] is False


# ---------------------------------------------------------------------------
# 12. _bulk_fetch_jobs
# ---------------------------------------------------------------------------

class TestBulkFetchJobs:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.post", new_callable=AsyncMock)
    async def test_successful_bulk_fetch(self, mock_post):
        mock_post.return_value = {
            "jobDetails": [
                {"jobId": "111", "title": "Python Dev", "companyName": "Acme",
                 "salaryDetail": {"minimumSalary": 500000, "maximumSalary": 1000000, "label": "5-10 LPA"},
                 "placeholders": [{"type": "location", "label": "Bangalore"}]},
                {"jobId": "222", "title": "Java Dev", "companyName": "Globex",
                 "salaryDetail": {"minimumSalary": 600000, "maximumSalary": 1200000, "label": "6-12 LPA"},
                 "placeholders": [{"type": "location", "label": "Mumbai"}]},
            ]
        }
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        result = await _bulk_fetch_jobs(job_ids=["111", "222"])
        assert result["status"] == "success"
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_empty_job_ids_returns_validation_error(self):
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        result = await _bulk_fetch_jobs(job_ids=[])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.post", new_callable=AsyncMock)
    async def test_caps_at_20_job_ids(self, mock_post):
        mock_post.return_value = {"jobDetails": []}
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        result = await _bulk_fetch_jobs(job_ids=[str(i) for i in range(30)])
        call_body = mock_post.call_args[0][1]
        assert len(call_body["jobIds"]) == 20


# ---------------------------------------------------------------------------
# 13. _get_job_v1
# ---------------------------------------------------------------------------

class TestGetJobV1:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_walk_in_fields_extracted(self, mock_get):
        mock_get.return_value = {
            "job": {
                "jobId": "55555", "post": "Marketing Manager", "companyName": "BigCorp",
                "isWalkIn": True, "walkinTime": "10:00 AM - 4:00 PM",
                "walkinVenue": "Building A, Floor 3, Bangalore",
                "walkingDateFrom": "2026-03-10", "walkingDateTo": "2026-03-12",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="55555")
        assert result["is_walk_in"] is True
        assert result["walkin_time"] == "10:00 AM - 4:00 PM"
        assert result["walkin_venue"] == "Building A, Floor 3, Bangalore"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_contact_fields_extracted(self, mock_get):
        mock_get.return_value = {
            "job": {
                "jobId": "66666", "post": "Recruiter", "companyName": "HireCo",
                "contactName": "Jane Smith", "email": "jane@hireco.com",
                "tel": "+91-9876543210", "CONTDESIG": "HR Manager",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="66666")
        assert result["contact_name"] == "Jane Smith"
        assert result["contact_email"] == "jane@hireco.com"
        assert result["contact_phone"] == "+91-9876543210"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_metrics_extracted(self, mock_get):
        mock_get.return_value = {
            "job": {"jobId": "77777", "post": "Data Scientist", "companyName": "DataCo",
                    "jdViews": 1500, "jdApplies": 200, "noOfVacancy": 5}
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="77777")
        assert result["jd_views"] == 1500
        assert result["jd_applies"] == 200
        assert result["vacancy"] == 5

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_none_values_stripped(self, mock_get):
        mock_get.return_value = {
            "job": {"jobId": "88888", "post": "QA Engineer", "companyName": "TestCo"}
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="88888")
        assert result["status"] == "success"
        assert "contact_email" not in result
        assert "walkin_time" not in result

    @pytest.mark.asyncio
    async def test_missing_job_id_returns_validation_error(self):
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_is_expired_and_closing_date(self, mock_get):
        mock_get.return_value = {
            "job": {"jobId": "33333", "post": "Expired Role", "companyName": "OldCo",
                    "isExpiredJob": True, "closingDate": "2025-12-31"}
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="33333")
        assert result["is_expired"] is True
        assert result["closing_date"] == "2025-12-31"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_is_consultant_field(self, mock_get):
        mock_get.return_value = {
            "job": {"jobId": "44444", "post": "Contractor", "companyName": "StaffCo", "cons": "y"}
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="44444")
        assert result["is_consultant"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_work_mode_mapping(self, mock_get):
        mock_get.return_value = {
            "job": {"jobId": "11111", "post": "Remote Dev", "companyName": "RemoteCo", "wfhType": "2"}
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="11111")
        assert result["work_mode"] == "remote"


# ---------------------------------------------------------------------------
# 14. Bulk fetch via unified tool
# ---------------------------------------------------------------------------

class TestBulkFetchJobsViaUnifiedTool:
    @pytest.mark.asyncio
    async def test_bulk_without_job_ids_returns_error(self):
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="bulk")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_detail_v1_without_job_id_returns_error(self):
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="detail_v1")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
