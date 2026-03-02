"""Tests for sync helper functions — _parse_applied_jobs, _parse_saved_jobs,
_map_naukri_status, _merge_applications, _merge_saved_jobs.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import patch
from datetime import datetime, timezone


# =====================================================================
# 1. _map_naukri_status — status string mapping
# =====================================================================

class TestMapNaukriStatus:
    """Tests for naukri_server.tools.sync._map_naukri_status."""

    def test_applied_status(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status("Applied") == "applied"

    def test_viewed_by_recruiter(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status("Viewed by Recruiter") == "viewed_by_recruiter"

    def test_shortlisted(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status("Shortlisted") == "shortlisted"

    def test_rejected(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status("Rejected") == "rejected"

    def test_not_interested_maps_to_rejected(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status("Not Interested") == "rejected"

    def test_interview(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status("Interview Scheduled") == "interview"

    def test_hired(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status("Hired") == "hired"

    def test_empty_string_defaults_to_applied(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status("") == "applied"

    def test_none_defaults_to_applied(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status(None) == "applied"

    def test_unknown_status_returned_lowered(self):
        from naukri_server.tools.sync import _map_naukri_status
        assert _map_naukri_status("Offer Extended") == "offer extended"


# =====================================================================
# 2. _parse_applied_jobs — API response parsing
# =====================================================================

class TestParseAppliedJobs:
    """Tests for naukri_server.tools.sync._parse_applied_jobs."""

    def test_standard_history_api_response(self):
        from naukri_server.tools.sync import _parse_applied_jobs
        data = {
            "applyDetails": [
                {
                    "jobId": "12345",
                    "jobTitle": "Python Developer",
                    "company": "Acme Corp",
                    "statusMsg": "Applied",
                    "appliedDate": "2026-01-15",
                    "location": "Bangalore",
                    "salary": "10-15 LPA",
                    "jdUrl": "https://naukri.com/job/12345",
                    "applyType": "quick_apply",
                },
            ],
        }
        jobs = _parse_applied_jobs(data)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["job_id"] == "12345"
        assert job["title"] == "Python Developer"
        assert job["company"] == "Acme Corp"
        assert job["status"] == "applied"
        assert job["applied_date"] == "2026-01-15"
        assert job["location"] == "Bangalore"
        assert job["salary"] == "10-15 LPA"
        assert job["url"] == "https://naukri.com/job/12345"
        assert job["apply_type"] == "quick_apply"

    def test_missing_optional_fields(self):
        from naukri_server.tools.sync import _parse_applied_jobs
        data = {
            "applyDetails": [
                {"jobId": "99999"},
            ],
        }
        jobs = _parse_applied_jobs(data)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["job_id"] == "99999"
        assert job["title"] is None
        assert job["company"] is None
        assert job["salary"] is None
        assert job["location"] is None
        assert job["url"] is None

    def test_empty_response(self):
        from naukri_server.tools.sync import _parse_applied_jobs
        assert _parse_applied_jobs({}) == []
        assert _parse_applied_jobs({"applyDetails": []}) == []

    def test_list_input(self):
        from naukri_server.tools.sync import _parse_applied_jobs
        data = [{"jobId": "111"}, {"jobId": "222"}]
        jobs = _parse_applied_jobs(data)
        assert len(jobs) == 2
        assert jobs[0]["job_id"] == "111"
        assert jobs[1]["job_id"] == "222"

    def test_skips_items_without_id(self):
        from naukri_server.tools.sync import _parse_applied_jobs
        data = [
            {"jobId": "111", "jobTitle": "Dev"},
            {"jobTitle": "No ID Job"},       # no job_id → skipped
            {"jobId": "", "jobTitle": "X"},   # empty job_id → skipped
        ]
        jobs = _parse_applied_jobs(data)
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "111"

    def test_status_list_extraction(self):
        """History API returns status as a list of dicts with statusMsg."""
        from naukri_server.tools.sync import _parse_applied_jobs
        data = [
            {
                "jobId": "555",
                "statusMsg": [
                    {"statusMsg": "Shortlisted", "statusDate": "2026-02-01"},
                    {"statusId": 4, "count": 3, "modifiedDate": "2026-02-10"},
                ],
            },
        ]
        jobs = _parse_applied_jobs(data)
        assert len(jobs) == 1
        assert jobs[0]["status"] == "shortlisted"
        assert jobs[0]["view_count"] == 3
        assert jobs[0]["last_viewed"] == "2026-02-10"

    def test_alternate_key_names(self):
        """Parser handles alternate field names like job_id, title, companyName."""
        from naukri_server.tools.sync import _parse_applied_jobs
        data = [
            {
                "job_id": "777",
                "title": "Backend Engineer",
                "companyName": "TechCo",
                "applied_date": "2026-03-01",
                "cityName": "Mumbai",
                "url": "https://naukri.com/job/777",
                "applicationStatus": "Viewed",
            },
        ]
        jobs = _parse_applied_jobs(data)
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "777"
        assert jobs[0]["title"] == "Backend Engineer"
        assert jobs[0]["company"] == "TechCo"
        assert jobs[0]["applied_date"] == "2026-03-01"
        assert jobs[0]["location"] == "Mumbai"
        assert jobs[0]["status"] == "viewed_by_recruiter"


# =====================================================================
# 3. _parse_saved_jobs — saved jobs parsing
# =====================================================================

class TestParseSavedJobs:
    """Tests for naukri_server.tools.sync._parse_saved_jobs."""

    def test_standard_response(self):
        from naukri_server.tools.sync import _parse_saved_jobs
        data = {
            "savedJobs": [
                {
                    "jobId": "10001",
                    "title": "Data Scientist",
                    "companyName": "DataCo",
                    "savedDate": "2026-02-20",
                    "salary": "20-25 LPA",
                    "location": "Hyderabad",
                    "url": "https://naukri.com/job/10001",
                },
            ],
        }
        jobs = _parse_saved_jobs(data)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["job_id"] == "10001"
        assert job["title"] == "Data Scientist"
        assert job["company"] == "DataCo"
        assert job["saved_date"] == "2026-02-20"
        assert job["salary"] == "20-25 LPA"
        assert job["location"] == "Hyderabad"
        assert job["url"] == "https://naukri.com/job/10001"

    def test_missing_optional_fields(self):
        from naukri_server.tools.sync import _parse_saved_jobs
        data = {"savedJobs": [{"jobId": "20002"}]}
        jobs = _parse_saved_jobs(data)
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "20002"
        assert jobs[0]["title"] is None
        assert jobs[0]["company"] is None
        assert jobs[0]["salary"] is None

    def test_empty_response(self):
        from naukri_server.tools.sync import _parse_saved_jobs
        assert _parse_saved_jobs({}) == []
        assert _parse_saved_jobs({"savedJobs": []}) == []


# =====================================================================
# 4. _merge_applications — merge remote into local
# =====================================================================

class TestMergeApplications:
    """Tests for naukri_server.tools.sync._merge_applications."""

    _FIXED_NOW = "2026-03-02T12:00:00+00:00"

    def _run_merge(self, local_apps, remote_jobs):
        from naukri_server.tools.sync import _merge_applications
        with patch("naukri_server.tools.sync.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            return _merge_applications(local_apps, remote_jobs)

    def test_new_entries_added(self):
        local = []
        remote = [
            {"job_id": "100", "title": "Dev", "company": "Co", "status": "applied"},
            {"job_id": "200", "title": "QA", "company": "Co2", "status": "applied"},
        ]
        stats = self._run_merge(local, remote)
        assert stats["new_added"] == 2
        assert stats["updated"] == 0
        assert stats["unchanged"] == 0
        assert len(local) == 2
        assert local[0]["job_id"] == "100"
        assert local[0]["source"] == "naukri_sync"
        assert local[1]["job_id"] == "200"

    def test_existing_entry_updated(self):
        local = [
            {"job_id": "100", "title": "Dev", "company": "Co", "status": "applied"},
        ]
        remote = [
            {"job_id": "100", "title": "Dev", "company": "Co", "status": "shortlisted"},
        ]
        stats = self._run_merge(local, remote)
        assert stats["updated"] == 1
        assert stats["new_added"] == 0
        assert local[0]["status"] == "shortlisted"
        assert "last_synced" in local[0]

    def test_existing_entry_unchanged(self):
        local = [
            {"job_id": "100", "title": "Dev", "company": "Co", "status": "applied"},
        ]
        remote = [
            {"job_id": "100", "title": "Dev", "company": "Co", "status": "applied"},
        ]
        stats = self._run_merge(local, remote)
        assert stats["unchanged"] == 1
        assert stats["updated"] == 0
        # last_synced should NOT be set when unchanged
        assert "last_synced" not in local[0]

    def test_local_only_counted(self):
        local = [
            {"job_id": "100", "title": "Dev", "company": "Co", "status": "applied"},
            {"job_id": "200", "title": "QA", "company": "Co2", "status": "applied"},
        ]
        remote = [
            {"job_id": "100", "title": "Dev", "company": "Co", "status": "applied"},
        ]
        stats = self._run_merge(local, remote)
        assert stats["local_only"] == 1  # job_id 200 is local-only

    def test_deduplication_no_double_append(self):
        """Running merge twice with the same remote data should not duplicate."""
        local = []
        remote = [{"job_id": "100", "title": "Dev", "company": "Co", "status": "applied"}]
        self._run_merge(local, remote)
        assert len(local) == 1
        # Merge again with same remote — should update, not re-add
        stats = self._run_merge(local, remote)
        assert len(local) == 1
        assert stats["new_added"] == 0
        assert stats["unchanged"] == 1

    def test_fills_missing_local_fields(self):
        """When local entry is missing salary/location, merge fills them from remote."""
        local = [
            {"job_id": "100", "title": "Dev", "company": "Co", "status": "applied"},
        ]
        remote = [
            {"job_id": "100", "title": "Dev", "company": "Co", "status": "applied",
             "salary": "15 LPA", "location": "Pune", "url": "https://naukri.com/job/100"},
        ]
        stats = self._run_merge(local, remote)
        # Fields are filled but status didn't change, so it counts as unchanged
        assert stats["unchanged"] == 1
        assert local[0]["salary"] == "15 LPA"
        assert local[0]["location"] == "Pune"
        assert local[0]["url"] == "https://naukri.com/job/100"


# =====================================================================
# 5. _merge_saved_jobs — merge remote saved jobs into local
# =====================================================================

class TestMergeSavedJobs:
    """Tests for naukri_server.tools.sync._merge_saved_jobs."""

    _FIXED_NOW = "2026-03-02T12:00:00+00:00"

    def _run_merge(self, local_saved, remote_jobs):
        from naukri_server.tools.sync import _merge_saved_jobs
        with patch("naukri_server.tools.sync.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            return _merge_saved_jobs(local_saved, remote_jobs)

    def test_new_saved_jobs_added(self):
        local = []
        remote = [
            {"job_id": "500", "title": "ML Engineer", "company": "AI Inc", "saved_date": "2026-02-01"},
            {"job_id": "600", "title": "DevOps", "company": "Cloud Co", "saved_date": "2026-02-05"},
        ]
        stats = self._run_merge(local, remote)
        assert stats["new_added"] == 2
        assert stats["already_local"] == 0
        assert len(local) == 2
        assert local[0]["source"] == "naukri_sync"
        assert local[0]["saved_at"] == "2026-02-01"

    def test_already_local_counted(self):
        local = [
            {"job_id": "500", "title": "ML Engineer", "company": "AI Inc"},
        ]
        remote = [
            {"job_id": "500", "title": "ML Engineer", "company": "AI Inc"},
        ]
        stats = self._run_merge(local, remote)
        assert stats["already_local"] == 1
        assert stats["new_added"] == 0
        assert len(local) == 1  # no duplicates

    def test_local_only_counted(self):
        local = [
            {"job_id": "500", "title": "ML Engineer", "company": "AI Inc"},
            {"job_id": "700", "title": "Fullstack", "company": "WebCo"},
        ]
        remote = [
            {"job_id": "500", "title": "ML Engineer", "company": "AI Inc"},
        ]
        stats = self._run_merge(local, remote)
        assert stats["local_only"] == 1  # job_id 700 is local-only

    def test_deduplication_no_double_append(self):
        """Merging same remote data twice should not duplicate entries."""
        local = []
        remote = [{"job_id": "500", "title": "ML Eng", "company": "AI"}]
        self._run_merge(local, remote)
        assert len(local) == 1
        stats = self._run_merge(local, remote)
        assert len(local) == 1
        assert stats["new_added"] == 0
        assert stats["already_local"] == 1
