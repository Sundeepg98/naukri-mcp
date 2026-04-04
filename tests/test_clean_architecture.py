"""Tests for Clean Architecture abstractions — gateways and use cases.

Every test is PURE: no network, no browser, no file I/O.
We mock _load_json and _save_json as needed.
"""

import asyncio

import pytest
from unittest.mock import patch


# =====================================================================
# 1. ApplicationGateway — abstract, cannot instantiate
# =====================================================================

class TestApplicationGatewayAbstract:
    """ApplicationGateway is abstract and cannot be instantiated directly."""

    def test_cannot_instantiate_abstract_gateway(self):
        from naukri_server.gateways import ApplicationGateway

        with pytest.raises(TypeError):
            ApplicationGateway()


# =====================================================================
# 2. JsonFileApplicationGateway.list_applications — filter by status
# =====================================================================

class TestJsonFileGatewayListApplications:
    """list_applications filters correctly by status."""

    @pytest.mark.asyncio
    async def test_list_filters_by_status(self):
        from naukri_server.gateways import JsonFileApplicationGateway

        apps = [
            {"job_id": "1", "status": "applied", "applied_at": "2026-01-01"},
            {"job_id": "2", "status": "rejected", "applied_at": "2026-01-02"},
            {"job_id": "3", "status": "applied", "applied_at": "2026-01-03"},
        ]
        lock = asyncio.Lock()
        gw = JsonFileApplicationGateway(file_path="/fake/path.json", lock=lock)

        with patch("naukri_server.tools.tracking._load_json", return_value=apps):
            result = await gw.list_applications(status="applied")

        assert len(result) == 2
        assert all(a["status"] == "applied" for a in result)


# =====================================================================
# 3. JsonFileApplicationGateway.get_application — correct match
# =====================================================================

class TestJsonFileGatewayGetApplication:
    """get_application returns the correct app by job_id."""

    @pytest.mark.asyncio
    async def test_get_returns_correct_app(self):
        from naukri_server.gateways import JsonFileApplicationGateway

        apps = [
            {"job_id": "100", "title": "SDE", "company": "Acme"},
            {"job_id": "200", "title": "QA", "company": "Beta"},
        ]
        lock = asyncio.Lock()
        gw = JsonFileApplicationGateway(file_path="/fake/path.json", lock=lock)

        with patch("naukri_server.tools.tracking._load_json", return_value=apps):
            result = await gw.get_application("200")

        assert result is not None
        assert result["job_id"] == "200"
        assert result["title"] == "QA"
        assert result["company"] == "Beta"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self):
        from naukri_server.gateways import JsonFileApplicationGateway

        lock = asyncio.Lock()
        gw = JsonFileApplicationGateway(file_path="/fake/path.json", lock=lock)

        with patch("naukri_server.tools.tracking._load_json", return_value=[]):
            result = await gw.get_application("999")

        assert result is None


# =====================================================================
# 4. SearchJobsCommand — correct defaults
# =====================================================================

class TestSearchJobsCommand:
    """SearchJobsCommand has correct default values."""

    def test_defaults(self):
        from naukri_server.use_cases import SearchJobsCommand

        cmd = SearchJobsCommand(keywords="python developer")

        assert cmd.keywords == "python developer"
        assert cmd.location is None
        assert cmd.experience is None
        assert cmd.salary_min is None
        assert cmd.limit == 20

    def test_custom_values(self):
        from naukri_server.use_cases import SearchJobsCommand

        cmd = SearchJobsCommand(
            keywords="react",
            location="Bangalore",
            experience=3,
            salary_min=1000000,
            limit=10,
        )

        assert cmd.keywords == "react"
        assert cmd.location == "Bangalore"
        assert cmd.experience == 3
        assert cmd.salary_min == 1000000
        assert cmd.limit == 10
