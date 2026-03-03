"""Tests for Tier 24: job_parsing module — _parse_job_list() comprehensive coverage.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import patch

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
