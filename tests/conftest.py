"""Shared test fixtures for Naukri MCP tests."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_api_client():
    """Pre-configured mock for api_client.get/post."""
    client = MagicMock()
    client.get = AsyncMock(return_value={})
    client.post = AsyncMock(return_value={})
    return client


@pytest.fixture
def sample_job():
    """Standard job dict for reuse across tests."""
    return {
        "job_id": "TEST001",
        "title": "Senior Backend Developer",
        "company": "TestCorp",
        "salary": "20-30 LPA",
        "salary_min_lakhs": 20.0,
        "salary_max_lakhs": 30.0,
        "location": "Bengaluru",
        "experience": "3-8 Yrs",
        "experience_min": 3,
        "experience_max": 8,
        "tags": ["Node.js", "TypeScript", "AWS"],
        "is_applied": False,
        "is_agent_eligible": False,
    }


@pytest.fixture
def sample_application():
    """Standard application dict for reuse."""
    return {
        "job_id": "TEST001",
        "title": "Senior Backend Developer",
        "company": "TestCorp",
        "status": "applied",
        "applied_at": "2026-03-01T10:00:00+00:00",
        "source": "single",
    }


@pytest.fixture
def sample_profile():
    """Standard profile for reuse."""
    return {
        "status": "success",
        "name": "Test User",
        "key_skills": ["Node.js", "TypeScript", "AWS", "PostgreSQL", "Docker"],
        "total_experience": "5 years 0 months",
        "current_location": "Bengaluru",
        "expected_ctc": "25",
    }
