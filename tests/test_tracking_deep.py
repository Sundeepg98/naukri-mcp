"""Deep tests for naukri_server.tools.tracking — application detail enrichment fields,
filter_info local filtering, company rating parsing.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier22.py, tier22_tracking_edge.py, tier23.py.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# 1. _get_application_detail — Tier 22 enrichment fields
# ---------------------------------------------------------------------------

class TestApplicationDetailEnrichment:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_total_applicants_extracted(self, mock_api):
        mock_api.return_value = {
            "totalApplicants": 884,
            "jobDetails": {"jobTitle": "SDE", "company": "X"},
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J123")
        assert result["status"] == "success"
        assert result["total_applicants"] == 884

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_star_rating_extracted(self, mock_api):
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "Acme"},
            "starRating": 4, "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J123")
        assert result["star_rating"] == 4

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_apply_flow_type_extracted(self, mock_api):
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "Acme"},
            "applyFlowType": "agentApply", "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J456")
        assert result["apply_flow_type"] == "agentApply"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_job_activity_fields_extracted(self, mock_api):
        mock_api.return_value = {
            "jobActivity": 12, "jobActivityDate": "2026-02-28",
            "jobDetails": {"jobTitle": "Dev", "company": "Y"}, "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J456")
        assert result["job_activity"] == 12
        assert result["job_activity_date"] == "2026-02-28"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_is_crawled_extracted(self, mock_api):
        mock_api.return_value = {
            "isCrawled": True,
            "jobDetails": {"jobTitle": "Eng", "company": "Z"}, "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J789")
        assert result["is_crawled"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_none_fields_stripped_from_result(self, mock_api):
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "S"}, "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J003")
        assert result["status"] == "success"
        for absent_key in ("total_applicants", "star_rating", "apply_flow_type",
                           "job_activity", "is_crawled", "company_rating"):
            assert absent_key not in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_total_applicants_missing_graceful(self, mock_api):
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "T"}, "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J004")
        assert result["status"] == "success"
        assert "total_applicants" not in result


# ---------------------------------------------------------------------------
# 2. Company rating parsing
# ---------------------------------------------------------------------------

class TestCompanyRatingParsing:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_company_rating_from_companyRating(self, mock_api):
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "TechCorp"},
            "companyRating": {"Rating": 4.2, "ReviewsCount": 500}, "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J789")
        assert result["company_rating"]["rating"] == 4.2
        assert result["company_rating"]["reviews"] == 500

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_company_rating_missing(self, mock_api):
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "TechCorp"}, "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J999")
        assert "company_rating" not in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_company_rating_from_ambitionbox_data(self, mock_api):
        """Falls through to ambitionBoxData when companyRating is absent."""
        mock_api.return_value = {
            "ambitionBoxData": {"Rating": 3.8, "ReviewsCount": 200},
            "jobDetails": {"jobTitle": "SDE", "company": "Q"}, "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J001")
        assert result["company_rating"]["rating"] == 3.8
        assert result["company_rating"]["reviews"] == 200

    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_client.get", new_callable=AsyncMock)
    async def test_company_rating_lowercase_keys(self, mock_api):
        mock_api.return_value = {
            "companyRating": {"rating": 4.1, "reviewsCount": 350},
            "jobDetails": {"jobTitle": "SDE", "company": "R"}, "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail
        result = await _get_application_detail("J002")
        assert result["company_rating"]["rating"] == 4.1
        assert result["company_rating"]["reviews"] == 350


# ---------------------------------------------------------------------------
# 3. _list_applications — filter_info local filtering (tier 23)
# ---------------------------------------------------------------------------

class TestFilterInfoLocal:
    def _make_apps(self):
        return [
            {"job_id": "J1", "status": "applied", "applied_at": "2026-01-01T00:00:00+00:00",
             "recruiter_active": True, "source": "naukri_sync"},
            {"job_id": "J2", "status": "applied", "applied_at": "2026-01-02T00:00:00+00:00",
             "job_activity": 5, "source": "naukri_sync"},
            {"job_id": "J3", "status": "applied", "applied_at": "2026-01-03T00:00:00+00:00",
             "source": "manual"},
        ]

    def _make_status_counts(self):
        return {"applied": 3}

    @pytest.mark.asyncio
    async def test_filter_info_1_recruiter_active(self):
        apps = self._make_apps()
        with patch("naukri_server.database.list_applications", new_callable=AsyncMock,
                    return_value=(apps, len(apps))), \
             patch("naukri_server.database.count_applications_by_status", new_callable=AsyncMock,
                    return_value=self._make_status_counts()):
            from naukri_server.tools.tracking import _list_applications
            result = await _list_applications(filter_info=1)
        assert result["count"] == 2
        returned_ids = {a["job_id"] for a in result["applications"]}
        assert "J1" in returned_ids
        assert "J2" in returned_ids
        assert "J3" not in returned_ids

    @pytest.mark.asyncio
    async def test_filter_info_2_naukri_sync(self):
        apps = self._make_apps()
        with patch("naukri_server.database.list_applications", new_callable=AsyncMock,
                    return_value=(apps, len(apps))), \
             patch("naukri_server.database.count_applications_by_status", new_callable=AsyncMock,
                    return_value=self._make_status_counts()):
            from naukri_server.tools.tracking import _list_applications
            result = await _list_applications(filter_info=2)
        assert result["count"] == 2
        returned_ids = {a["job_id"] for a in result["applications"]}
        assert "J3" not in returned_ids

    @pytest.mark.asyncio
    async def test_filter_info_3_external(self):
        apps = self._make_apps()
        with patch("naukri_server.database.list_applications", new_callable=AsyncMock,
                    return_value=(apps, len(apps))), \
             patch("naukri_server.database.count_applications_by_status", new_callable=AsyncMock,
                    return_value=self._make_status_counts()):
            from naukri_server.tools.tracking import _list_applications
            result = await _list_applications(filter_info=3)
        assert result["count"] == 1
        assert result["applications"][0]["job_id"] == "J3"

    @pytest.mark.asyncio
    async def test_filter_info_none_no_filter(self):
        apps = self._make_apps()
        with patch("naukri_server.database.list_applications", new_callable=AsyncMock,
                    return_value=(apps, len(apps))), \
             patch("naukri_server.database.count_applications_by_status", new_callable=AsyncMock,
                    return_value=self._make_status_counts()):
            from naukri_server.tools.tracking import _list_applications
            result = await _list_applications(filter_info=None)
        assert result["count"] == 3
