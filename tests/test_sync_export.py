"""Tests for sync, export, and resume_tailor tools — routing and validation.

Every test is PURE: no network, no browser, no file I/O.
We only exercise validation / routing logic that runs BEFORE any async helper call.
"""

import pytest
from unittest.mock import AsyncMock, patch


# =====================================================================
# 1. naukri_sync — additional routing tests
# =====================================================================

class TestSync:
    """Tests for naukri_server.tools.sync.naukri_sync."""

    @pytest.mark.asyncio
    async def test_sync_invalid_entity(self):
        from naukri_server.tools.sync import naukri_sync
        result = await naukri_sync(entity="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown entity" in result["message"]

    @pytest.mark.asyncio
    async def test_sync_applications_routes_to_helper(self):
        from naukri_server.tools.sync import naukri_sync
        with patch("naukri_server.tools.sync._sync_applications", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "method": "rest_api", "total_remote": 5}
            result = await naukri_sync(entity="applications", days_back=30)
            mock_helper.assert_awaited_once_with(force_browser=False, days_back=30)
            assert result["status"] == "success"
            assert result["method"] == "rest_api"

    @pytest.mark.asyncio
    async def test_sync_applications_force_browser(self):
        from naukri_server.tools.sync import naukri_sync
        with patch("naukri_server.tools.sync._sync_applications", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "method": "browser_intercept"}
            result = await naukri_sync(entity="applications", force_browser=True, days_back=7)
            mock_helper.assert_awaited_once_with(force_browser=True, days_back=7)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_sync_saved_jobs_routes_to_helper(self):
        from naukri_server.tools.sync import naukri_sync
        with patch("naukri_server.tools.sync._sync_saved_jobs", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "method": "rest_api", "total_remote": 3}
            result = await naukri_sync(entity="saved_jobs")
            mock_helper.assert_awaited_once_with(force_browser=False)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_sync_applications_exception_caught(self):
        from naukri_server.tools.sync import naukri_sync
        with patch("naukri_server.tools.sync._sync_applications", new_callable=AsyncMock) as mock_helper:
            mock_helper.side_effect = RuntimeError("connection reset")
            result = await naukri_sync(entity="applications")
            assert result["status"] == "error"
            assert result["error_code"] == "API_ERROR"
            assert "connection reset" in result["message"]

    @pytest.mark.asyncio
    async def test_sync_saved_jobs_exception_caught(self):
        from naukri_server.tools.sync import naukri_sync
        with patch("naukri_server.tools.sync._sync_saved_jobs", new_callable=AsyncMock) as mock_helper:
            mock_helper.side_effect = RuntimeError("timeout")
            result = await naukri_sync(entity="saved_jobs")
            assert result["status"] == "error"
            assert result["error_code"] == "API_ERROR"
            assert "timeout" in result["message"]


# =====================================================================
# 2. naukri_export_data — validation and routing
# =====================================================================

class TestExportData:
    """Tests for naukri_server.tools.export.naukri_export_data."""

    @pytest.mark.asyncio
    async def test_export_invalid_data_type(self):
        from naukri_server.tools.export import naukri_export_data
        result = await naukri_export_data(data_type="resumes")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "resumes" in result["message"]

    @pytest.mark.asyncio
    async def test_export_invalid_format(self):
        from naukri_server.tools.export import naukri_export_data
        result = await naukri_export_data(data_type="applications", format="xml")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "xml" in result["message"]

    @pytest.mark.asyncio
    async def test_export_search_results_requires_keywords(self):
        from naukri_server.tools.export import naukri_export_data
        result = await naukri_export_data(data_type="search_results")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "keywords" in result["message"]

    @pytest.mark.asyncio
    async def test_export_applications_no_file(self):
        """When applications.json does not exist, should return NOT_FOUND."""
        from naukri_server.tools.export import naukri_export_data
        from unittest.mock import MagicMock
        fake_apps_file = MagicMock()
        fake_apps_file.exists.return_value = False
        with patch("naukri_server.tools.export.APPLICATIONS_FILE", fake_apps_file):
            result = await naukri_export_data(data_type="applications")
            assert result["status"] == "error"
            assert result["error_code"] == "NOT_FOUND"
            assert "applications" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_export_saved_jobs_no_file(self):
        """When saved_jobs.json does not exist, should return NOT_FOUND."""
        from naukri_server.tools.export import naukri_export_data
        from unittest.mock import MagicMock
        fake_saved_file = MagicMock()
        fake_saved_file.exists.return_value = False
        with patch("naukri_server.tools.export.SAVED_JOBS_FILE", fake_saved_file):
            result = await naukri_export_data(data_type="saved_jobs")
            assert result["status"] == "error"
            assert result["error_code"] == "NOT_FOUND"
            assert "saved jobs" in result["message"].lower()


# =====================================================================
# 3. naukri_resume_tailor — validation and routing
# =====================================================================

class TestResumeTailor:
    """Tests for naukri_server.tools.resume_tailor.naukri_resume_tailor."""

    @pytest.mark.asyncio
    async def test_resume_tailor_job_fetch_error(self):
        """When the job fetch fails, resume_tailor returns an error."""
        from naukri_server.tools.resume_tailor import naukri_resume_tailor
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_job.return_value = {"status": "error", "message": "Job not found"}
            mock_profile.return_value = {"status": "success", "key_skills": []}
            result = await naukri_resume_tailor(job_id="999999")
            assert result["status"] == "error"
            assert "Failed to fetch job" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_tailor_profile_fetch_error(self):
        """When the profile fetch fails, resume_tailor returns an error."""
        from naukri_server.tools.resume_tailor import naukri_resume_tailor
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_job.return_value = {"status": "success", "title": "SDE", "company": "Acme", "skills": []}
            mock_profile.return_value = {"status": "error", "message": "Not logged in"}
            result = await naukri_resume_tailor(job_id="123456")
            assert result["status"] == "error"
            assert "Failed to fetch profile" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_tailor_job_exception_caught(self):
        """When the job fetch raises an exception, it is caught gracefully."""
        from naukri_server.tools.resume_tailor import naukri_resume_tailor
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_job.side_effect = RuntimeError("network down")
            mock_profile.return_value = {"status": "success", "key_skills": []}
            result = await naukri_resume_tailor(job_id="123456")
            assert result["status"] == "error"
            assert "Failed to fetch job" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_tailor_success_routes(self):
        """When both fetches succeed, resume_tailor returns suggestions."""
        from naukri_server.tools.resume_tailor import naukri_resume_tailor
        with patch("naukri_server.tools.jobs.naukri_get_job", new_callable=AsyncMock) as mock_job, \
             patch("naukri_server.tools.profile.get_cached_profile", new_callable=AsyncMock) as mock_profile:
            mock_job.return_value = {
                "status": "success",
                "title": "Senior Python Developer",
                "company": "Acme Corp",
                "description": "Build scalable microservices using Python, Django, PostgreSQL",
                "skills": ["Python", "Django", "PostgreSQL", "Docker", "Kubernetes"],
            }
            mock_profile.return_value = {
                "status": "success",
                "key_skills": ["Python", "Flask", "MySQL", "AWS"],
                "resume_headline": "Python Developer with 5 years experience",
                "employment": [],
                "summary": "Experienced backend developer",
                "certifications": [],
                "projects": [],
            }
            result = await naukri_resume_tailor(job_id="123456")
            assert result["status"] == "success"
            assert result["job_title"] == "Senior Python Developer"
            assert result["company"] == "Acme Corp"
            assert "suggestions" in result
            suggestions = result["suggestions"]
            # Django, PostgreSQL, Docker, Kubernetes are in job but not in profile
            assert "Django" in suggestions["skills_to_add"]
            assert "PostgreSQL" in suggestions["skills_to_add"]
            assert "Docker" in suggestions["skills_to_add"]
            assert "Kubernetes" in suggestions["skills_to_add"]
            # Python is already in profile, should NOT be in skills_to_add
            assert "Python" not in suggestions["skills_to_add"]


# =====================================================================
# From test_consolidation.py — sync action routing & validation
# =====================================================================

class TestSyncConsolidation:
    """Tests for naukri_server.tools.sync.naukri_sync."""

    @pytest.mark.asyncio
    async def test_invalid_entity(self):
        from naukri_server.tools.sync import naukri_sync
        result = await naukri_sync(entity="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown entity" in result["message"]

    @pytest.mark.asyncio
    async def test_applications_routes_to_helper(self):
        from naukri_server.tools.sync import naukri_sync
        with patch("naukri_server.tools.sync._sync_applications", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "method": "api"}
            result = await naukri_sync(entity="applications", days_back=30)
            mock_helper.assert_awaited_once_with(force_browser=False, days_back=30)
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_saved_jobs_routes_to_helper(self):
        from naukri_server.tools.sync import naukri_sync
        with patch("naukri_server.tools.sync._sync_saved_jobs", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "method": "api"}
            result = await naukri_sync(entity="saved_jobs")
            mock_helper.assert_awaited_once_with(force_browser=False)
