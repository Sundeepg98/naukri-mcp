"""Tests for interview round tracking and offer comparison in naukri_applications.

Every test is PURE: no network, no browser, no file I/O.
We mock _load_json and _save_json to isolate logic.
"""

import pytest
from unittest.mock import patch


class TestAddInterviewRound:
    """Tests for add_round action."""

    @pytest.mark.asyncio
    async def test_add_round_creates_entry(self):
        """Adding a round should persist an entry and return total_rounds."""
        from naukri_server.tools.tracking import _add_interview_round

        captured = {}

        def fake_save(path, data):
            captured["data"] = data

        with patch("naukri_server.tools.tracking._load_json", return_value=[]) as mock_load, \
             patch("naukri_server.tools.tracking._save_json", side_effect=fake_save):
            result = await _add_interview_round(
                job_id="J100", round_type="technical",
                date="2026-04-10T10:00:00", notes="DSA round", status="scheduled",
            )

        assert result["status"] == "success"
        assert result["action"] == "round_added"
        assert result["job_id"] == "J100"
        assert result["round_type"] == "technical"
        assert result["total_rounds"] == 1

        saved = captured["data"]
        assert len(saved) == 1
        assert saved[0]["job_id"] == "J100"
        assert saved[0]["round_type"] == "technical"
        assert saved[0]["date"] == "2026-04-10T10:00:00"
        assert saved[0]["notes"] == "DSA round"
        assert saved[0]["status"] == "scheduled"
        assert "created_at" in saved[0]


class TestListInterviewRounds:
    """Tests for list_rounds action."""

    @pytest.mark.asyncio
    async def test_list_rounds_filters_by_job_id(self):
        """Listing rounds with a job_id should only return rounds for that job."""
        from naukri_server.tools.tracking import _list_interview_rounds

        existing_rounds = [
            {"job_id": "J100", "round_type": "phone_screen", "status": "completed"},
            {"job_id": "J100", "round_type": "technical", "status": "scheduled"},
            {"job_id": "J200", "round_type": "hr", "status": "scheduled"},
        ]

        with patch("naukri_server.tools.tracking._load_json", return_value=existing_rounds):
            result = await _list_interview_rounds(job_id="J100")

        assert result["status"] == "success"
        assert result["total_rounds"] == 2
        assert result["jobs_with_rounds"] == 1
        assert all(r["job_id"] == "J100" for r in result["rounds"])

    @pytest.mark.asyncio
    async def test_list_rounds_empty_returns_zero(self):
        """An empty rounds file should return zero counts."""
        from naukri_server.tools.tracking import _list_interview_rounds

        with patch("naukri_server.tools.tracking._load_json", return_value=[]):
            result = await _list_interview_rounds()

        assert result["status"] == "success"
        assert result["total_rounds"] == 0
        assert result["jobs_with_rounds"] == 0
        assert result["rounds"] == []


class TestCompareOffers:
    """Tests for compare_offers action."""

    @pytest.mark.asyncio
    async def test_compare_offers_returns_side_by_side(self):
        """Comparing two jobs should return both with app data and round counts."""
        from naukri_server.tools.tracking import _compare_offers

        apps = [
            {"job_id": "J100", "title": "SDE", "company": "Acme", "status": "offered", "ars_score": 85},
            {"job_id": "J200", "title": "SDE-2", "company": "Beta", "status": "interview", "ars_score": 72},
        ]
        rounds = [
            {"job_id": "J100", "round_type": "phone_screen", "status": "completed"},
            {"job_id": "J100", "round_type": "technical", "status": "completed"},
            {"job_id": "J200", "round_type": "hr", "status": "scheduled"},
        ]

        def fake_load(path):
            name = str(path)
            if "interview_rounds" in name:
                return rounds
            return apps

        with patch("naukri_server.tools.tracking._load_json", side_effect=fake_load):
            result = await _compare_offers(["J100", "J200"])

        assert result["status"] == "success"
        assert result["count"] == 2

        offer1 = result["offers"][0]
        assert offer1["job_id"] == "J100"
        assert offer1["title"] == "SDE"
        assert offer1["company"] == "Acme"
        assert offer1["ars_score"] == 85
        assert offer1["interview_rounds"] == 2
        assert offer1["latest_round"]["round_type"] == "technical"

        offer2 = result["offers"][1]
        assert offer2["job_id"] == "J200"
        assert offer2["interview_rounds"] == 1
