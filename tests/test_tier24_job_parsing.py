"""Tests for Tier 24: job_parsing module — _parse_job_list() comprehensive coverage.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.tools.job_parsing import _parse_job_list
from naukri_server.config import NAUKRI_BASE, LAKHS_MULTIPLIER


# ---------------------------------------------------------------------------
# Helper fixtures
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
# 1. Basic parsing with all fields populated
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
        jobs = _parse_job_list(jobs_input, limit=3)
        assert len(jobs) == 3

    def test_empty_job_details_list(self):
        jobs = _parse_job_list([], limit=10)
        assert jobs == []


# ---------------------------------------------------------------------------
# 2. Salary string computation
# ---------------------------------------------------------------------------

class TestSalaryParsing:
    def test_salary_from_min_max(self):
        """When label is empty and max > 0, format as X.X-Y.Y LPA."""
        job = _make_job(salaryDetail={"minimumSalary": 500_000, "maximumSalary": 1_000_000, "label": "", "hideSalary": False})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary"] == "5.0-10.0 LPA"
        assert j["salary_min_lakhs"] == 5.0
        assert j["salary_max_lakhs"] == 10.0

    def test_salary_label_fallback(self):
        """When label is present, use it directly."""
        job = _make_job(salaryDetail={"minimumSalary": 0, "maximumSalary": 0, "label": "Not Disclosed", "hideSalary": True})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary"] == "Not Disclosed"

    def test_salary_not_disclosed_when_max_zero(self):
        """When label is empty and max == 0, fall back to 'Not Disclosed'."""
        job = _make_job(salaryDetail={"minimumSalary": 0, "maximumSalary": 0, "label": "", "hideSalary": False})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary"] == "Not Disclosed"

    def test_salary_raw_fields_populated(self):
        """salary_min_raw, salary_max_raw, salary_hidden are set from salaryDetail dict."""
        job = _make_job(salaryDetail={"minimumSalary": 800_000, "maximumSalary": 1_500_000, "label": "", "hideSalary": True})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary_min_raw"] == 800_000
        assert j["salary_max_raw"] == 1_500_000
        assert j["salary_hidden"] is True

    def test_salary_min_lakhs_none_when_zero(self):
        """salary_min_lakhs is None when minimumSalary is 0 (falsy check)."""
        job = _make_job(salaryDetail={"minimumSalary": 0, "maximumSalary": 0, "label": "", "hideSalary": False})
        j = _parse_job_list([job], limit=10)[0]
        assert j["salary_min_lakhs"] is None
        assert j["salary_max_lakhs"] is None


# ---------------------------------------------------------------------------
# 3. Location from placeholders
# ---------------------------------------------------------------------------

class TestLocationParsing:
    def test_location_from_type_location(self):
        """Location is read from placeholder where type == 'location'."""
        job = _make_job(placeholders=[
            {"type": "experience", "label": "2-5 Yrs"},
            {"type": "location", "label": "Mumbai"},
        ])
        j = _parse_job_list([job], limit=10)[0]
        assert j["location"] == "Mumbai"

    def test_location_first_placeholder_fallback(self):
        """If no type='location' placeholder, use first placeholder label."""
        job = _make_job(placeholders=[
            {"type": "experience", "label": "3-6 Yrs"},
            {"type": "salary", "label": "8-12 LPA"},
        ])
        j = _parse_job_list([job], limit=10)[0]
        assert j["location"] == "3-6 Yrs"

    def test_location_none_when_no_placeholders(self):
        """Location is None when placeholders list is empty."""
        job = _make_job(placeholders=[])
        j = _parse_job_list([job], limit=10)[0]
        assert j["location"] is None


# ---------------------------------------------------------------------------
# 4. Tags from tagsAndSkills
# ---------------------------------------------------------------------------

class TestTagsParsing:
    def test_tags_from_comma_separated_string(self):
        job = _make_job(tagsAndSkills="Python, Django, REST API")
        j = _parse_job_list([job], limit=10)[0]
        assert j["tags"] == ["Python", "Django", "REST API"]

    def test_tags_from_list_type(self):
        """When tagsAndSkills is already a list, use it directly."""
        job = _make_job(tagsAndSkills=["Python", "Go", "Kubernetes"])
        j = _parse_job_list([job], limit=10)[0]
        assert j["tags"] == ["Python", "Go", "Kubernetes"]

    def test_tags_empty_string(self):
        """Empty string tagsAndSkills produces empty tags list."""
        job = _make_job(tagsAndSkills="")
        j = _parse_job_list([job], limit=10)[0]
        assert j["tags"] == []


# ---------------------------------------------------------------------------
# 5. URL construction
# ---------------------------------------------------------------------------

class TestURLConstruction:
    def test_url_with_jdurl(self):
        """URL is NAUKRI_BASE + jdURL when jdURL is present."""
        job = _make_job(jdURL="/job-listings-se-acme-JOB123")
        j = _parse_job_list([job], limit=10)[0]
        assert j["url"] == f"{NAUKRI_BASE}/job-listings-se-acme-JOB123"

    def test_url_fallback_job_id(self):
        """URL falls back to NAUKRI_BASE/job-listings-{jobId} when jdURL is absent."""
        job = _make_job()
        del job["jdURL"]
        j = _parse_job_list([job], limit=10)[0]
        assert j["url"] == f"{NAUKRI_BASE}/job-listings-JOB123"

    def test_jd_url_field(self):
        """jd_url field mirrors the raw jdURL value."""
        job = _make_job(jdURL="/job-listings-se-acme-JOB123")
        j = _parse_job_list([job], limit=10)[0]
        assert j["jd_url"] == "/job-listings-se-acme-JOB123"


# ---------------------------------------------------------------------------
# 6. AmbitionBox rating
# ---------------------------------------------------------------------------

class TestAmbitionBoxData:
    def test_rating_from_rating_key(self):
        """company_rating reads Rating key first."""
        job = _make_job(ambitionBoxData={"Rating": 4.1, "AggregateRating": 3.9, "ReviewsCount": 100})
        j = _parse_job_list([job], limit=10)[0]
        assert j["company_rating"] == 4.1
        assert j["company_reviews_count"] == 100

    def test_rating_aggregate_fallback(self):
        """Falls back to AggregateRating when Rating key is absent."""
        job = _make_job(ambitionBoxData={"AggregateRating": 3.8, "ReviewsCount": 50})
        j = _parse_job_list([job], limit=10)[0]
        assert j["company_rating"] == 3.8

    def test_rating_none_when_missing(self):
        """company_rating is None when ambitionBoxData is absent."""
        job = _make_job()
        del job["ambitionBoxData"]
        j = _parse_job_list([job], limit=10)[0]
        assert j["company_rating"] is None


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
        """hiringFor of empty string becomes None (via 'or None')."""
        job = _make_job(hiringFor="")
        j = _parse_job_list([job], limit=10)[0]
        assert j["hiring_for"] is None


# ---------------------------------------------------------------------------
# 8. Diversity tag
# ---------------------------------------------------------------------------

class TestDiversityTag:
    def test_diversity_tag_present(self):
        job = _make_job(diversityTagText="Women-friendly")
        j = _parse_job_list([job], limit=10)[0]
        assert j["diversity_tag"] == "Women-friendly"

    def test_diversity_tag_none_when_absent(self):
        job = _make_job()
        del job["diversityTagText"]
        j = _parse_job_list([job], limit=10)[0]
        assert j["diversity_tag"] is None


# ---------------------------------------------------------------------------
# 9. Vacancy typo fallback
# ---------------------------------------------------------------------------

class TestVacancyFallback:
    def test_vacancy_from_vacancy_key(self):
        job = _make_job(vacancy=5)
        j = _parse_job_list([job], limit=10)[0]
        assert j["vacancies"] == 5

    def test_vacancy_from_vacany_typo(self):
        """'vacany' (typo) is used when 'vacancy' is absent."""
        job = _make_job()
        del job["vacancy"]
        job["vacany"] = 7
        j = _parse_job_list([job], limit=10)[0]
        assert j["vacancies"] == 7

    def test_vacancy_none_when_both_absent(self):
        job = _make_job()
        del job["vacancy"]
        j = _parse_job_list([job], limit=10)[0]
        assert j["vacancies"] is None


# ---------------------------------------------------------------------------
# 10. work_mode from wfhType fallback
# ---------------------------------------------------------------------------

class TestWorkModeFallback:
    def test_work_mode_from_workMode(self):
        job = _make_job(workMode="Hybrid")
        j = _parse_job_list([job], limit=10)[0]
        assert j["work_mode"] == "Hybrid"

    def test_work_mode_from_wfhType(self):
        """wfhType is used when workMode is absent."""
        job = _make_job()
        del job["workMode"]
        job["wfhType"] = "Remote"
        j = _parse_job_list([job], limit=10)[0]
        assert j["work_mode"] == "Remote"

    def test_work_mode_none_when_both_absent(self):
        job = _make_job()
        del job["workMode"]
        j = _parse_job_list([job], limit=10)[0]
        assert j["work_mode"] is None


# =====================================================================
# From test_tier21.py — bulk job fetch
# =====================================================================

class TestBulkFetchJobs:
    """Tests for _bulk_fetch_jobs."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.post", new_callable=AsyncMock)
    async def test_successful_bulk_fetch(self, mock_post):
        """Successful bulk fetch with multiple job IDs."""
        mock_post.return_value = {
            "jobDetails": [
                {
                    "jobId": "111",
                    "title": "Python Dev",
                    "companyName": "Acme",
                    "salaryDetail": {"minimumSalary": 500000, "maximumSalary": 1000000, "label": "5-10 LPA"},
                    "placeholders": [{"type": "location", "label": "Bangalore"}],
                },
                {
                    "jobId": "222",
                    "title": "Java Dev",
                    "companyName": "Globex",
                    "salaryDetail": {"minimumSalary": 600000, "maximumSalary": 1200000, "label": "6-12 LPA"},
                    "placeholders": [{"type": "location", "label": "Mumbai"}],
                },
            ]
        }
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        result = await _bulk_fetch_jobs(job_ids=["111", "222"])
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["jobs"][0]["job_id"] == "111"
        assert result["jobs"][1]["job_id"] == "222"

    @pytest.mark.asyncio
    async def test_empty_job_ids_returns_validation_error(self):
        """Empty job_ids returns validation error."""
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        result = await _bulk_fetch_jobs(job_ids=[])
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.post", new_callable=AsyncMock)
    async def test_caps_at_20_job_ids(self, mock_post):
        """More than 20 job IDs are capped at 20."""
        mock_post.return_value = {"jobDetails": []}
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        ids = [str(i) for i in range(30)]
        result = await _bulk_fetch_jobs(job_ids=ids)
        # Check that api_post was called with only 20 IDs
        call_body = mock_post.call_args[0][1]
        assert len(call_body["jobIds"]) == 20

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.post", new_callable=AsyncMock)
    async def test_invalid_ids_empty_job_details(self, mock_post):
        """Invalid IDs silently dropped — empty jobDetails yields count=0."""
        mock_post.return_value = {"jobDetails": []}
        from naukri_server.tools.jobs import _bulk_fetch_jobs
        result = await _bulk_fetch_jobs(job_ids=["invalid_id"])
        assert result["status"] == "success"
        assert result["count"] == 0
        assert result["jobs"] == []


# =====================================================================
# From test_tier21.py — V1 job detail
# =====================================================================

class TestGetJobV1:
    """Tests for _get_job_v1."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_walk_in_fields_extracted(self, mock_get):
        """Walk-in fields are extracted: is_walk_in, walkin_time, walkin_venue."""
        mock_get.return_value = {
            "job": {
                "jobId": "55555",
                "post": "Marketing Manager",
                "companyName": "BigCorp",
                "isWalkIn": True,
                "walkinTime": "10:00 AM - 4:00 PM",
                "walkinVenue": "Building A, Floor 3, Bangalore",
                "walkingDateFrom": "2026-03-10",
                "walkingDateTo": "2026-03-12",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="55555")
        assert result["status"] == "success"
        assert result["is_walk_in"] is True
        assert result["walkin_time"] == "10:00 AM - 4:00 PM"
        assert result["walkin_venue"] == "Building A, Floor 3, Bangalore"
        assert result["walkin_date_from"] == "2026-03-10"
        assert result["walkin_date_to"] == "2026-03-12"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_contact_fields_extracted(self, mock_get):
        """Contact fields are extracted: contact_name, contact_email, contact_phone."""
        mock_get.return_value = {
            "job": {
                "jobId": "66666",
                "post": "Recruiter",
                "companyName": "HireCo",
                "contactName": "Jane Smith",
                "email": "jane@hireco.com",
                "tel": "+91-9876543210",
                "CONTDESIG": "HR Manager",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="66666")
        assert result["contact_name"] == "Jane Smith"
        assert result["contact_email"] == "jane@hireco.com"
        assert result["contact_phone"] == "+91-9876543210"
        assert result["contact_designation"] == "HR Manager"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_metrics_extracted(self, mock_get):
        """Metrics are extracted: jd_views, jd_applies, vacancy."""
        mock_get.return_value = {
            "job": {
                "jobId": "77777",
                "post": "Data Scientist",
                "companyName": "DataCo",
                "jdViews": 1500,
                "jdApplies": 200,
                "noOfVacancy": 5,
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="77777")
        assert result["jd_views"] == 1500
        assert result["jd_applies"] == 200
        assert result["vacancy"] == 5

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_none_values_stripped(self, mock_get):
        """None values are stripped from the result."""
        mock_get.return_value = {
            "job": {
                "jobId": "88888",
                "post": "QA Engineer",
                "companyName": "TestCo",
                # Many fields missing = None
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="88888")
        assert result["status"] == "success"
        # Falsy None fields should be absent (stripped)
        assert "contact_email" not in result
        assert "contact_phone" not in result
        assert "walkin_time" not in result
        assert "walkin_venue" not in result
        assert "vacancy" not in result

    @pytest.mark.asyncio
    async def test_missing_job_id_returns_validation_error(self):
        """Missing job_id returns validation error."""
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_salary_hidden_field(self, mock_get):
        """salary_hidden is True when showSal == 'n'."""
        mock_get.return_value = {
            "job": {
                "jobId": "99999",
                "post": "Secret Agent",
                "companyName": "MI6",
                "showSal": "n",
                "minSal": 0,
                "maxSal": 0,
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="99999")
        assert result["salary_hidden"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_work_mode_mapping(self, mock_get):
        """wfhType maps to human-readable work_mode."""
        mock_get.return_value = {
            "job": {
                "jobId": "11111",
                "post": "Remote Dev",
                "companyName": "RemoteCo",
                "wfhType": "2",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="11111")
        assert result["work_mode"] == "remote"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_hiring_for_field(self, mock_get):
        """hiringFor field is extracted when present."""
        mock_get.return_value = {
            "job": {
                "jobId": "12121",
                "post": "Consultant",
                "companyName": "ConsultCo",
                "hiringFor": "Google India",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="12121")
        assert result["hiring_for"] == "Google India"


# =====================================================================
# From test_tier21.py — job list parser enrichment
# =====================================================================

class TestJobListParserEnrichment:
    """Tests for enrichment fields in _parse_job_list."""

    def test_hiring_for_field(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1", "hiringFor": "Microsoft"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["hiring_for"] == "Microsoft"

    def test_hiring_for_none_when_missing(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["hiring_for"] is None

    def test_diversity_tag(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1", "diversityTagText": "Women in Tech"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["diversity_tag"] == "Women in Tech"

    def test_diversity_tag_none_when_missing(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["diversity_tag"] is None

    def test_experience_text(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1", "experienceText": "3-5 Yrs"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["experience_text"] == "3-5 Yrs"

    def test_experience_text_none_when_missing(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{"jobId": "1"}]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["experience_text"] is None

    def test_salary_min_raw_from_salary_detail(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "1",
            "salaryDetail": {
                "minimumSalary": 500000,
                "maximumSalary": 1200000,
                "label": "5-12 LPA",
                "hideSalary": False,
            },
        }]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["salary_min_raw"] == 500000
        assert result[0]["salary_max_raw"] == 1200000
        assert result[0]["salary_hidden"] is False

    def test_salary_hidden_true(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "1",
            "salaryDetail": {
                "minimumSalary": 0,
                "maximumSalary": 0,
                "hideSalary": True,
            },
        }]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["salary_hidden"] is True

    def test_salary_hidden_defaults_to_false(self):
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "1",
            "salaryDetail": {
                "minimumSalary": 300000,
                "maximumSalary": 600000,
            },
        }]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["salary_hidden"] is False

    def test_all_enrichment_fields_together(self):
        """All Tier 21 enrichment fields present in a single job item."""
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "42",
            "title": "Full Stack Dev",
            "companyName": "TechCorp",
            "hiringFor": "Amazon",
            "diversityTagText": "LGBTQ+ Friendly",
            "experienceText": "2-4 Yrs",
            "salaryDetail": {
                "minimumSalary": 800000,
                "maximumSalary": 1600000,
                "label": "8-16 LPA",
                "hideSalary": False,
            },
            "placeholders": [{"type": "location", "label": "Hyderabad"}],
        }]
        result = _parse_job_list(jobs_raw, 10)
        j = result[0]
        assert j["hiring_for"] == "Amazon"
        assert j["diversity_tag"] == "LGBTQ+ Friendly"
        assert j["experience_text"] == "2-4 Yrs"
        assert j["salary_min_raw"] == 800000
        assert j["salary_max_raw"] == 1600000
        assert j["salary_hidden"] is False

    def test_empty_salary_detail_still_adds_raw_fields(self):
        """Even an empty salaryDetail dict adds the raw fields."""
        from naukri_server.tools.job_parsing import _parse_job_list
        jobs_raw = [{
            "jobId": "1",
            "salaryDetail": {},
        }]
        result = _parse_job_list(jobs_raw, 10)
        assert result[0]["salary_min_raw"] is None
        assert result[0]["salary_max_raw"] is None
        assert result[0]["salary_hidden"] is False


# =====================================================================
# From test_tier21.py — bulk fetch via unified tool & V1 edge cases
# =====================================================================

class TestBulkFetchJobsViaUnifiedTool:
    """Tests for bulk action routed through naukri_jobs."""

    @pytest.mark.asyncio
    async def test_bulk_without_job_ids_returns_error(self):
        """bulk action without job_ids returns validation error."""
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="bulk")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_detail_v1_without_job_id_returns_error(self):
        """detail_v1 action without job_id returns validation error."""
        from naukri_server.tools.jobs import naukri_jobs
        result = await naukri_jobs(action="detail_v1")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"


class TestJobV1IsExpired:
    """Test additional V1-unique fields."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_is_expired_and_closing_date(self, mock_get):
        mock_get.return_value = {
            "job": {
                "jobId": "33333",
                "post": "Expired Role",
                "companyName": "OldCo",
                "isExpiredJob": True,
                "closingDate": "2025-12-31",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="33333")
        assert result["is_expired"] is True
        assert result["closing_date"] == "2025-12-31"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.jobs.api_client.get", new_callable=AsyncMock)
    async def test_is_consultant_field(self, mock_get):
        mock_get.return_value = {
            "job": {
                "jobId": "44444",
                "post": "Contractor",
                "companyName": "StaffCo",
                "cons": "y",
            }
        }
        from naukri_server.tools.jobs import _get_job_v1
        result = await _get_job_v1(job_id="44444")
        assert result["is_consultant"] is True
