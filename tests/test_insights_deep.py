"""Deep unit tests for naukri_server.tools.insights — salary parsing, application
analytics, cached-answer routing, and the unified naukri_insights dispatcher."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Async-lock mock (replaces asyncio.Lock used in the production module)
# ---------------------------------------------------------------------------

class _MockAsyncLock:
    """Drop-in replacement for asyncio.Lock that does nothing."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ===========================================================================
# 1. _parse_salary_str  (sync, no mocking needed)
# ===========================================================================

def test_parse_salary_range():
    from naukri_server.tools.insights import _parse_salary_str
    assert _parse_salary_str("10-15 Lacs") == (10.0, 15.0)


def test_parse_salary_not_disclosed():
    from naukri_server.tools.insights import _parse_salary_str
    assert _parse_salary_str("Not disclosed") == (None, None)


def test_parse_salary_empty():
    from naukri_server.tools.insights import _parse_salary_str
    assert _parse_salary_str("") == (None, None)


def test_parse_salary_large_numbers():
    """Values > 200 should be auto-converted from rupees to LPA."""
    from naukri_server.tools.insights import _parse_salary_str
    min_s, max_s = _parse_salary_str("1000000-1500000")
    assert min_s == 10.0
    assert max_s == 15.0


def test_parse_salary_single_value():
    from naukri_server.tools.insights import _parse_salary_str
    min_s, max_s = _parse_salary_str("15 LPA")
    assert min_s == 15.0
    assert max_s == 15.0


# ===========================================================================
# 2. _application_insights
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.insights._applications_lock", new=_MockAsyncLock())
@patch("naukri_server.tools.insights._load_json")
async def test_application_insights_basic(mock_load):
    now = datetime.now(timezone.utc).isoformat()
    mock_load.return_value = [
        {"applied_at": now, "status": "applied", "company": "Google"},
        {"applied_at": now, "status": "applied", "company": "Meta"},
        {"applied_at": now, "status": "already_applied", "company": "Google"},
    ]
    from naukri_server.tools.insights import _application_insights
    result = await _application_insights(days=30)

    assert result["status"] == "success"
    assert result["total_applications"] == 3
    assert result["status_breakdown"]["applied"] == 2
    assert result["status_breakdown"]["already_applied"] == 1
    # Google appears twice — should be top company
    assert result["top_companies"][0]["company"] == "Google"


@pytest.mark.asyncio
@patch("naukri_server.tools.insights._applications_lock", new=_MockAsyncLock())
@patch("naukri_server.tools.insights._load_json")
async def test_application_insights_no_data(mock_load):
    mock_load.return_value = []
    from naukri_server.tools.insights import _application_insights
    result = await _application_insights()
    assert result["status"] == "error"


# ===========================================================================
# 3. _salary_position
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.insights._applications_lock", new=_MockAsyncLock())
@patch("naukri_server.tools.insights._load_json")
async def test_salary_position_basic(mock_load):
    mock_load.return_value = [
        {"title": "SDE", "salary": "10-15 LPA"},
        {"title": "SDE", "salary": "12-18 LPA"},
        {"title": "SDE", "salary": "Not disclosed"},
    ]
    from naukri_server.tools.insights import _salary_position
    result = await _salary_position()

    assert result["status"] == "success"
    assert result["total_with_salary"] == 2  # "Not disclosed" excluded
    assert result["salary_range"]["min"] == 10.0


# ===========================================================================
# 4. _cached_answers routing
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.insights._cache_lock", new=_MockAsyncLock())
@patch("naukri_server.tools.insights._load_cache")
async def test_cached_answers_list(mock_load):
    mock_load.return_value = {
        "current_ctc_{options}": {
            "answer": "15 LPA",
            "questionType": "text",
            "cached_at": 1700000000,
        },
    }
    from naukri_server.tools.insights import _cached_answers
    result = await _cached_answers(action="list")

    assert result["status"] == "success"
    assert result["total_cached"] == 1
    assert result["answers"][0]["answer"] == "15 LPA"


@pytest.mark.asyncio
async def test_cached_answers_update_missing_key():
    from naukri_server.tools.insights import _cached_answers
    result = await _cached_answers(action="update")
    assert result["status"] == "error"
    assert "key" in result["message"]


@pytest.mark.asyncio
async def test_cached_answers_delete_missing_key():
    from naukri_server.tools.insights import _cached_answers
    result = await _cached_answers(action="delete")
    assert result["status"] == "error"
    assert "key" in result["message"]


# ===========================================================================
# 5. naukri_insights unified router
# ===========================================================================

@pytest.mark.asyncio
async def test_insights_invalid_type():
    from naukri_server.tools.insights import naukri_insights
    result = await naukri_insights(insight_type="nonexistent")
    assert result["status"] == "error"
    assert "Unknown insight_type" in result["message"]


@pytest.mark.asyncio
async def test_insights_salary_benchmark_requires_keywords():
    from naukri_server.tools.insights import naukri_insights
    result = await naukri_insights(insight_type="salary_benchmark")
    assert result["status"] == "error"
    assert "requires keywords" in result["message"]


# ===========================================================================
# 6. _conversion_funnel
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.insights._applications_lock", new=_MockAsyncLock())
@patch("naukri_server.tools.insights._load_json")
async def test_conversion_funnel_mixed_statuses(mock_load):
    """Conversion funnel with mixed statuses should compute correct counts and rate."""
    now = datetime.now(timezone.utc).isoformat()
    mock_load.return_value = [
        {"applied_at": now, "status": "applied", "company": "Google"},
        {"applied_at": now, "status": "viewed", "company": "Google"},
        {"applied_at": now, "status": "interview", "company": "Meta"},
        {"applied_at": now, "status": "applied", "company": "Meta"},
        {"applied_at": now, "status": "applied", "company": "Netflix"},
        {"applied_at": now, "status": "offered", "company": "Netflix"},
    ]
    from naukri_server.tools.insights import _conversion_funnel
    result = await _conversion_funnel(days=30)

    assert result["status"] == "success"
    assert result["total_applied"] == 6
    assert result["funnel"]["applied"] == 3
    assert result["funnel"]["viewed"] == 1
    assert result["funnel"]["interview"] == 1
    assert result["funnel"]["offered"] == 1
    # conversion_rate = interviews / total * 100 = 1/6 * 100 ≈ 16.7
    assert result["conversion_rate"] == round(1 / 6 * 100, 1)
    # Google: 2 applies, 1 responded (viewed) -> rate 50
    # Meta:   2 applies, 1 responded (interview) -> rate 50
    # Netflix: 2 applies, 1 responded (offered) -> rate 50
    assert len(result["top_responsive_companies"]) == 3
    for co in result["top_responsive_companies"]:
        assert co["rate"] == 50


@pytest.mark.asyncio
@patch("naukri_server.tools.insights._applications_lock", new=_MockAsyncLock())
@patch("naukri_server.tools.insights._load_json")
async def test_conversion_funnel_dead_zones(mock_load):
    """Dead zones: companies with 3+ applies and 0 responses."""
    now = datetime.now(timezone.utc).isoformat()
    mock_load.return_value = [
        {"applied_at": now, "status": "applied", "company": "DeadCorp"},
        {"applied_at": now, "status": "applied", "company": "DeadCorp"},
        {"applied_at": now, "status": "applied", "company": "DeadCorp"},
        {"applied_at": now, "status": "applied", "company": "DeadCorp"},
        {"applied_at": now, "status": "interview", "company": "GoodCo"},
        {"applied_at": now, "status": "applied", "company": "GoodCo"},
    ]
    from naukri_server.tools.insights import _conversion_funnel
    result = await _conversion_funnel(days=30)

    assert result["status"] == "success"
    assert result["total_applied"] == 6
    # DeadCorp: 4 applies, 0 responded, rate 0 -> dead zone
    assert len(result["dead_zones"]) == 1
    assert result["dead_zones"][0]["company"] == "DeadCorp"
    assert result["dead_zones"][0]["applied"] == 4
    assert result["dead_zones"][0]["rate"] == 0
    # GoodCo has only 2 applies, so not in responsive list (threshold >= 2), but is there
    # and is NOT a dead zone
    good = [c for c in result["top_responsive_companies"] if c["company"] == "GoodCo"]
    assert len(good) == 1
    assert good[0]["rate"] == 50


@pytest.mark.asyncio
@patch("naukri_server.tools.insights._applications_lock", new=_MockAsyncLock())
@patch("naukri_server.tools.insights._load_json")
async def test_conversion_funnel_empty_applications(mock_load):
    """Empty applications should return zeroed-out funnel."""
    mock_load.return_value = []
    from naukri_server.tools.insights import _conversion_funnel
    result = await _conversion_funnel(days=30)

    assert result["status"] == "success"
    assert result["total_applied"] == 0
    assert result["funnel"] == {}
    assert result["conversion_rate"] == 0
    assert result["top_responsive_companies"] == []
    assert result["dead_zones"] == []
