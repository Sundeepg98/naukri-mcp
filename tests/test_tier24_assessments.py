"""Tests for Tier 24: assessments module — listing, completeness, routing.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.api import NaukriAPIError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_assessment(**overrides) -> dict:
    base = {
        "skill": "Python",
        "level": "Expert",
        "badge": "https://cdn.naukri.com/badge/python.png",
        "results": {
            "score": 85,
            "rank": 120,
            "accuracy": 90.0,
            "status": "PASS",
        },
        "maxMarks": 100,
        "passingMarks": 60,
        "testId": "T001",
    }
    base.update(overrides)
    return base


def _make_dashboard_response(assessments=None, extras=None) -> dict:
    """Build a mock DASHBOARD_API response."""
    board = {
        "pc": 78,
        "totalSearchAppearancesCount": 200,
        "profileViewCount": 15,
        "isPaidUser": False,
        "isPremium": True,
        "isAIResumeEligible": True,
        "eligibleFlagForAIMockInterview": False,
        "jbSearchStatus": {"data": [{"value": "Actively looking"}]},
        "assessments": assessments or [],
    }
    if extras:
        board.update(extras)
    return {"dashBoard": board}


# ---------------------------------------------------------------------------
# 1. _list_assessments — populated dashboard
# ---------------------------------------------------------------------------

class TestListAssessments:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments.api_client.get", new_callable=AsyncMock)
    async def test_populated_dashboard(self, mock_get):
        mock_get.return_value = _make_dashboard_response(
            assessments=[_make_assessment(), _make_assessment(skill="SQL", level="Intermediate")]
        )
        from naukri_server.tools.assessments import _list_assessments
        result = await _list_assessments()

        assert result["status"] == "success"
        assert result["total"] == 2
        assert result["count"] == 2
        assert result["page"] == 1
        assert result["has_more"] is False
        assert result["assessments"][0]["skill"] == "Python"
        assert result["assessments"][0]["score"] == 85
        assert result["assessments"][0]["level"] == "Expert"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments.api_client.get", new_callable=AsyncMock)
    async def test_empty_assessments(self, mock_get):
        """Empty assessments list returns success with total=0."""
        mock_get.return_value = _make_dashboard_response(assessments=[])
        from naukri_server.tools.assessments import _list_assessments
        result = await _list_assessments()

        assert result["status"] == "success"
        assert result["total"] == 0
        assert result["assessments"] == []

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments.api_client.get", new_callable=AsyncMock)
    async def test_level_as_dict_fallback(self, mock_get):
        """When level is a dict, extracts 'name' key."""
        mock_get.return_value = _make_dashboard_response(
            assessments=[_make_assessment(level={"name": "Advanced", "code": "ADV"})]
        )
        from naukri_server.tools.assessments import _list_assessments
        result = await _list_assessments()
        assert result["assessments"][0]["level"] == "Advanced"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments.api_client.get", new_callable=AsyncMock)
    async def test_badge_as_dict_fallback(self, mock_get):
        """When badge is a dict, extracts 'imgStr' key."""
        mock_get.return_value = _make_dashboard_response(
            assessments=[_make_assessment(badge={"imgStr": "https://cdn.naukri.com/b.png", "alt": "badge"})]
        )
        from naukri_server.tools.assessments import _list_assessments
        result = await _list_assessments()
        assert result["assessments"][0]["badge_url"] == "https://cdn.naukri.com/b.png"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments.api_client.get", new_callable=AsyncMock)
    async def test_pagination_fields(self, mock_get):
        """Pagination fields page=1 and has_more=False are always set."""
        mock_get.return_value = _make_dashboard_response(assessments=[_make_assessment()])
        from naukri_server.tools.assessments import _list_assessments
        result = await _list_assessments()
        assert result["page"] == 1
        assert result["has_more"] is False


# ---------------------------------------------------------------------------
# 2. _get_profile_completeness
# ---------------------------------------------------------------------------

class TestGetProfileCompleteness:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments.api_client.get", new_callable=AsyncMock)
    async def test_normal_parsing(self, mock_get):
        mock_get.return_value = _make_dashboard_response()
        from naukri_server.tools.assessments import _get_profile_completeness
        result = await _get_profile_completeness()

        assert result["status"] == "success"
        assert result["completeness_percent"] == 78
        assert result["search_appearances"] == 200
        assert result["recruiter_views"] == 15
        assert result["is_paid"] is False
        assert result["is_premium"] is True
        assert result["ai_resume_eligible"] is True
        assert result["mock_interview_eligible"] is False
        assert result["job_search_status"] == "Actively looking"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments.api_client.get", new_callable=AsyncMock)
    async def test_empty_jb_search_status(self, mock_get):
        """Empty jbSearchStatus.data produces empty string job_search_status."""
        data = _make_dashboard_response()
        data["dashBoard"]["jbSearchStatus"] = {"data": []}
        mock_get.return_value = data
        from naukri_server.tools.assessments import _get_profile_completeness
        result = await _get_profile_completeness()
        assert result["job_search_status"] == ""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments.api_client.get", new_callable=AsyncMock)
    async def test_jb_search_status_as_non_dict(self, mock_get):
        """Non-dict jbSearchStatus is coerced to string."""
        data = _make_dashboard_response()
        data["dashBoard"]["jbSearchStatus"] = "Looking"
        mock_get.return_value = data
        from naukri_server.tools.assessments import _get_profile_completeness
        result = await _get_profile_completeness()
        assert result["job_search_status"] == "Looking"


# ---------------------------------------------------------------------------
# 3. _assessments_dispatch — action routing
# ---------------------------------------------------------------------------

class TestAssessmentsDispatch:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock)
    async def test_list_action_routes(self, mock_list):
        mock_list.return_value = {"status": "success", "assessments": []}
        from naukri_server.tools.assessments import _assessments_dispatch
        result = await _assessments_dispatch(action="list")
        assert result["status"] == "success"
        mock_list.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments._get_profile_completeness", new_callable=AsyncMock)
    async def test_completeness_action_routes(self, mock_completeness):
        mock_completeness.return_value = {"status": "success", "completeness_percent": 90}
        from naukri_server.tools.assessments import _assessments_dispatch
        result = await _assessments_dispatch(action="completeness")
        assert result["status"] == "success"
        assert result["completeness_percent"] == 90
        mock_completeness.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_action_returns_validation_error(self):
        from naukri_server.tools.assessments import _assessments_dispatch
        result = await _assessments_dispatch(action="delete")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock)
    async def test_list_naukri_api_error(self, mock_list):
        mock_list.side_effect = NaukriAPIError(403, "Forbidden")
        from naukri_server.tools.assessments import _assessments_dispatch
        result = await _assessments_dispatch(action="list")
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert result["http_status"] == 403

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock)
    async def test_list_generic_exception(self, mock_list):
        mock_list.side_effect = RuntimeError("unexpected error")
        from naukri_server.tools.assessments import _assessments_dispatch
        result = await _assessments_dispatch(action="list")
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert "RuntimeError" in result["message"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.assessments._get_profile_completeness", new_callable=AsyncMock)
    async def test_completeness_naukri_api_error(self, mock_comp):
        mock_comp.side_effect = NaukriAPIError(401, "Unauthorized")
        from naukri_server.tools.assessments import _assessments_dispatch
        result = await _assessments_dispatch(action="completeness")
        assert result["status"] == "error"
        assert result["http_status"] == 401


# =====================================================================
# From test_consolidation.py — assessments action routing & validation
# =====================================================================

class TestAssessmentsConsolidation:
    """Tests for naukri_server.tools.assessments._assessments_dispatch (via naukri_assessments alias)."""

    @pytest.mark.asyncio
    async def test_invalid_action(self):
        from naukri_server.tools.assessments import naukri_assessments
        result = await naukri_assessments(action="invalid")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_list_routes_to_helper(self):
        from naukri_server.tools.assessments import naukri_assessments
        with patch("naukri_server.tools.assessments._list_assessments", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "assessments": []}
            result = await naukri_assessments(action="list")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_completeness_routes_to_helper(self):
        from naukri_server.tools.assessments import naukri_assessments
        with patch("naukri_server.tools.assessments._get_profile_completeness", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "completeness_percent": 85}
            result = await naukri_assessments(action="completeness")
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"
