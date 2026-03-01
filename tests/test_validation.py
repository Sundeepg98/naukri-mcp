"""Tests for naukri_server.validation — response validators."""

from naukri_server.validation import (
    validate_job_list, validate_company_list, validate_profile,
    validate_job_detail, validate_salary_data, validate_review_data,
)


class TestValidateJobList:
    def test_valid_list(self):
        jobs = [{"job_id": "1", "title": "Dev", "company": "Corp"}]
        warnings = validate_job_list(jobs, 1, "test")
        assert warnings == []

    def test_empty_with_total(self):
        warnings = validate_job_list([], 5, "test")
        assert any("empty" in w for w in warnings)

    def test_missing_fields(self):
        jobs = [{"job_id": "1"}]
        warnings = validate_job_list(jobs, 1, "test")
        assert any("title" in w for w in warnings)


class TestValidateProfile:
    def test_valid_profile(self):
        profile = {"name": "John", "total_experience": "5 years", "current_location": "Bangalore",
                    "skills_with_experience": [{"skill": "Python"}], "employment": [{}]}
        assert validate_profile(profile) == []

    def test_missing_critical(self):
        warnings = validate_profile({"skills_with_experience": [], "employment": []})
        assert any("critical" in w.lower() for w in warnings)


class TestValidateJobDetail:
    def test_valid_detail(self):
        assert validate_job_detail({"title": "Dev", "company": "Corp", "description": "..."}) == []

    def test_missing_fields(self):
        warnings = validate_job_detail({})
        assert len(warnings) > 0


class TestValidateSalaryData:
    def test_valid_salary(self):
        data = {"avg_salary": 1500000, "salaries": [{"designation": "SDE", "salary": 1500000}]}
        assert validate_salary_data(data) == []

    def test_empty_data(self):
        warnings = validate_salary_data({})
        assert any("missing" in w for w in warnings)


class TestValidateReviewData:
    def test_valid_reviews(self):
        data = {"review_count": 1, "reviews": [{"title": "Good", "likes": "Culture"}]}
        assert validate_review_data(data) == []

    def test_count_mismatch(self):
        warnings = validate_review_data({"review_count": 5, "reviews": []})
        assert any("empty" in w for w in warnings)
