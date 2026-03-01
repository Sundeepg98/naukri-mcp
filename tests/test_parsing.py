"""Tests for naukri_server.tools.job_parsing — shared job list parser."""

from naukri_server.tools.job_parsing import _parse_job_list


class TestParseJobList:
    def _make_job(self, **overrides):
        """Create a minimal mock job API response."""
        base = {
            "jobId": "123456789",
            "title": "Python Developer",
            "companyName": "Test Corp",
            "salaryDetail": {"label": "10-15 LPA", "minimumSalary": 1000000, "maximumSalary": 1500000},
            "placeholders": [{"type": "location", "label": "Bangalore"}],
            "minimumExperience": 3,
            "maximumExperience": 5,
            "isApplied": False,
            "createdDate": "2026-03-01",
            "tagsAndSkills": "Python, Django, REST API",
        }
        base.update(overrides)
        return base

    def test_full_parse(self):
        jobs = _parse_job_list([self._make_job()], limit=10)
        assert len(jobs) == 1
        j = jobs[0]
        assert j["job_id"] == "123456789"
        assert j["title"] == "Python Developer"
        assert j["company"] == "Test Corp"
        assert j["salary"] == "10-15 LPA"
        assert j["location"] == "Bangalore"
        assert "python" in [t.lower() for t in j["tags"]]

    def test_minimal_job(self):
        """Gracefully handle missing optional fields."""
        minimal = {"jobId": "999", "title": "Test"}
        jobs = _parse_job_list([minimal], limit=10)
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "999"
        assert jobs[0]["salary"] == "Not Disclosed"

    def test_consultant_job(self):
        job = self._make_job(consultant=True, clientTitleString="Real Corp", clientGroupId="456")
        jobs = _parse_job_list([job], limit=10)
        assert jobs[0]["is_consultant"] is True
        assert jobs[0]["client_company"] == "Real Corp"

    def test_limit_respected(self):
        many_jobs = [self._make_job(jobId=str(i)) for i in range(20)]
        jobs = _parse_job_list(many_jobs, limit=5)
        assert len(jobs) == 5

    def test_salary_not_disclosed(self):
        job = self._make_job(salaryDetail={"label": "", "minimumSalary": 0, "maximumSalary": 0})
        jobs = _parse_job_list([job], limit=10)
        assert jobs[0]["salary"] == "Not Disclosed"

    def test_jd_url_preferred(self):
        job = self._make_job(jdURL="/job/python-developer-123456789")
        jobs = _parse_job_list([job], limit=10)
        assert "python-developer" in jobs[0]["url"]
