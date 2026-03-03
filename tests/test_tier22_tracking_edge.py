"""
Tier 22 edge-case tests for naukri_server/tools/tracking.py.

These tests verify the extraction of Tier 22 enrichment fields added to
_get_application_detail(): totalApplicants, starRating, applyFlowType,
jobActivity, jobActivityDate, isCrawled, and embedded company ratings from
both companyRating and ambitionBoxData keys.

All tests are pure — no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch


class TestTrackingTier22EdgeCases:
    """Edge-case tests for Tier 22 fields in _get_application_detail."""

    # ------------------------------------------------------------------
    # 1. totalApplicants extracted correctly
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
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

    # ------------------------------------------------------------------
    # 2. jobActivity and jobActivityDate extracted correctly
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_job_activity_fields_extracted(self, mock_api):
        mock_api.return_value = {
            "jobActivity": 12,
            "jobActivityDate": "2026-02-28",
            "jobDetails": {"jobTitle": "Dev", "company": "Y"},
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail

        result = await _get_application_detail("J456")

        assert result["status"] == "success"
        assert result["job_activity"] == 12
        assert result["job_activity_date"] == "2026-02-28"

    # ------------------------------------------------------------------
    # 3. isCrawled extracted correctly
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_is_crawled_extracted(self, mock_api):
        mock_api.return_value = {
            "isCrawled": True,
            "jobDetails": {"jobTitle": "Eng", "company": "Z"},
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail

        result = await _get_application_detail("J789")

        assert result["status"] == "success"
        assert result["is_crawled"] is True

    # ------------------------------------------------------------------
    # 4. companyRating falls through to ambitionBoxData (uppercase keys)
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_company_rating_from_ambitionbox_data(self, mock_api):
        # No companyRating key — should fall through to ambitionBoxData
        mock_api.return_value = {
            "ambitionBoxData": {"Rating": 3.8, "ReviewsCount": 200},
            "jobDetails": {"jobTitle": "SDE", "company": "Q"},
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail

        result = await _get_application_detail("J001")

        assert result["status"] == "success"
        assert "company_rating" in result
        assert result["company_rating"]["rating"] == 3.8
        assert result["company_rating"]["reviews"] == 200

    # ------------------------------------------------------------------
    # 5. companyRating with lowercase keys (rating / reviewsCount)
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_company_rating_lowercase_keys(self, mock_api):
        mock_api.return_value = {
            "companyRating": {"rating": 4.1, "reviewsCount": 350},
            "jobDetails": {"jobTitle": "SDE", "company": "R"},
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail

        result = await _get_application_detail("J002")

        assert result["status"] == "success"
        assert "company_rating" in result
        assert result["company_rating"]["rating"] == 4.1
        assert result["company_rating"]["reviews"] == 350

    # ------------------------------------------------------------------
    # 6. None fields stripped — absent Tier 22 fields must not appear
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_none_fields_stripped_from_result(self, mock_api):
        # Minimal payload — no Tier 22 fields at all
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "S"},
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail

        result = await _get_application_detail("J003")

        assert result["status"] == "success"
        # All absent Tier 22 fields must be stripped by the None filter
        for absent_key in (
            "total_applicants",
            "star_rating",
            "apply_flow_type",
            "job_activity",
            "is_crawled",
            "company_rating",
        ):
            assert absent_key not in result, (
                f"Expected '{absent_key}' to be stripped from result, but it was present"
            )

    # ------------------------------------------------------------------
    # 7. Missing totalApplicants handled gracefully (stripped, not KeyError)
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.tracking.api_get", new_callable=AsyncMock)
    async def test_total_applicants_missing_graceful(self, mock_api):
        # No totalApplicants in response
        mock_api.return_value = {
            "jobDetails": {"jobTitle": "SDE", "company": "T"},
            "status": [],
        }
        from naukri_server.tools.tracking import _get_application_detail

        result = await _get_application_detail("J004")

        assert result["status"] == "success"
        assert "total_applicants" not in result
