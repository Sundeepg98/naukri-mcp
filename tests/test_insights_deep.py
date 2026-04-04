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
@patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
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
@patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
async def test_application_insights_no_data(mock_load):
    mock_load.return_value = []
    from naukri_server.tools.insights import _application_insights
    result = await _application_insights()
    assert result["status"] == "error"


# ===========================================================================
# 3. _salary_position
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
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
@patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
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
@patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
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
@patch("naukri_server.database.list_all_applications", new_callable=AsyncMock)
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


# ===========================================================================
# 7. _detect_status_changes
# ===========================================================================

@pytest.mark.asyncio
@patch("naukri_server.tools.sync._sync_applications", new_callable=AsyncMock)
async def test_detect_status_changes_positive_transitions(mock_sync):
    """Positive transitions (applied->viewed, viewed->interview) detected correctly."""
    mock_sync.return_value = {
        "status": "success",
        "method": "rest_api",
        "last_sync": "2026-04-03T10:00:00+00:00",
        "status_changes": [
            {"job_id": "1001", "title": "Backend Engineer", "old_status": "applied", "new_status": "viewed_by_recruiter"},
            {"job_id": "1002", "title": "Frontend Dev", "old_status": "viewed_by_recruiter", "new_status": "interview"},
            {"job_id": "1003", "title": "DevOps Engineer", "old_status": "applied", "new_status": "rejected"},
        ],
    }

    from naukri_server.tools.insights import _detect_status_changes
    result = await _detect_status_changes(days_back=30)

    assert result["status"] == "success"
    assert result["total_changes"] == 3
    assert result["positive_changes"] == 2
    assert len(result["positive"]) == 2
    assert len(result["neutral"]) == 1
    # Check positive entries
    positive_ids = {c["job_id"] for c in result["positive"]}
    assert positive_ids == {"1001", "1002"}
    assert all(c["transition_type"] == "positive" for c in result["positive"])
    # Check neutral entry
    assert result["neutral"][0]["job_id"] == "1003"
    assert result["neutral"][0]["transition_type"] == "neutral"
    assert result["sync_method"] == "rest_api"
    assert result["last_sync"] == "2026-04-03T10:00:00+00:00"


@pytest.mark.asyncio
@patch("naukri_server.tools.sync._sync_applications", new_callable=AsyncMock)
async def test_detect_status_changes_no_changes(mock_sync):
    """No changes returns empty lists."""
    mock_sync.return_value = {
        "status": "success",
        "method": "rest_api",
        "last_sync": "2026-04-03T10:00:00+00:00",
        # No status_changes key — sync returns it only when changes exist
    }

    from naukri_server.tools.insights import _detect_status_changes
    result = await _detect_status_changes(days_back=7)

    assert result["status"] == "success"
    assert result["total_changes"] == 0
    assert result["positive_changes"] == 0
    assert result["positive"] == []
    assert result["neutral"] == []


# =====================================================================
# From test_consolidation.py — insights action routing & validation
# =====================================================================

class TestInsightsConsolidation:
    """Tests for naukri_server.tools.insights.naukri_insights."""

    @pytest.mark.asyncio
    async def test_invalid_insight_type(self):
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="invalid")
        assert result["status"] == "error"
        assert "Unknown insight_type" in result["message"]

    @pytest.mark.asyncio
    async def test_cached_answers_update_requires_key(self):
        """cached_answers update without key should fail validation inside _cached_answers."""
        from naukri_server.tools.insights import _cached_answers
        result = await _cached_answers(action="update")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "key" in result["message"]

    @pytest.mark.asyncio
    async def test_cached_answers_delete_requires_key(self):
        from naukri_server.tools.insights import _cached_answers
        result = await _cached_answers(action="delete")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "key" in result["message"]

    @pytest.mark.asyncio
    async def test_cached_answers_invalid_action(self):
        from naukri_server.tools.insights import _cached_answers
        result = await _cached_answers(action="purge")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Unknown action" in result["message"]

    @pytest.mark.asyncio
    async def test_applications_routes_to_helper(self):
        from naukri_server.tools.insights import naukri_insights
        with patch("naukri_server.tools.insights._application_insights", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "total_applications": 10}
            result = await naukri_insights(insight_type="applications", days=7)
            mock_helper.assert_awaited_once_with(days=7)
            assert result["status"] == "success"


# =====================================================================
# Taxonomy lookup (recovered from tier22.py, tier22_misc_edge.py)
# =====================================================================

def _make_cache_miss():
    """Return a mock TtlCache that always calls the fetch function (cache miss)."""
    async def _call(fn):
        return await fn()
    mock_cache = MagicMock()
    mock_cache.get = AsyncMock(side_effect=_call)
    return mock_cache


class TestTaxonomyLookup:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_client.get", new_callable=AsyncMock)
    async def test_hierarchy_parsing(self, mock_api):
        mock_api.return_value = [
            {"id": 1, "label": "IT & Software", "synonyms": [],
             "child": [{"id": 11, "label": "Software Development",
                        "child": [{"id": 111, "label": "Backend Developer", "synonyms": ["server-side dev"]},
                                  {"id": 112, "label": "Frontend Developer", "synonyms": []}]}]},
            {"id": 2, "label": "Data Science", "synonyms": ["ML"],
             "child": [{"id": 21, "label": "Machine Learning",
                        "child": [{"id": 211, "label": "ML Engineer", "synonyms": ["AI engineer"]},
                                  {"id": 212, "label": "Data Scientist", "synonyms": []}]}]},
        ]
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        assert result["total_departments"] == 2
        assert len(result["departments"][0]["role_categories"][0]["roles"]) == 2

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_client.get", new_callable=AsyncMock)
    async def test_synonym_extraction(self, mock_api):
        mock_api.return_value = [
            {"id": 1, "label": "Engineering", "synonyms": [],
             "child": [{"id": 11, "label": "Software",
                        "child": [{"id": 111, "label": "Developer", "synonyms": ["dev", "developer"]}]}]},
        ]
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        role = result["departments"][0]["role_categories"][0]["roles"][0]
        assert "dev" in role["synonyms"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_client.get", new_callable=AsyncMock)
    async def test_total_role_count(self, mock_api):
        mock_api.return_value = [
            {"id": 1, "label": "Dept A", "synonyms": [],
             "child": [{"id": 11, "label": "Cat A1",
                        "child": [{"id": i, "label": f"Role {i}", "synonyms": []} for i in range(111, 114)]}]},
            {"id": 2, "label": "Dept B", "synonyms": [],
             "child": [{"id": 21, "label": "Cat B1",
                        "child": [{"id": i, "label": f"Role {i}", "synonyms": []} for i in range(211, 213)]}]},
        ]
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        assert result["total_roles"] == 5

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_client.get", new_callable=AsyncMock)
    async def test_empty_response(self, mock_api):
        mock_api.return_value = {}
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        assert result["departments"] == []
        assert result["total_roles"] == 0


class TestTaxonomyEdgeCases:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_client.get", new_callable=AsyncMock)
    async def test_taxonomy_cache_hit(self, mock_api):
        cached_result = {"status": "success", "total_departments": 37, "total_roles": 1461, "departments": []}
        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=cached_result)
        with patch("naukri_server.tools.insights._taxonomy_cache", mock_cache):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        assert result == cached_result
        mock_api.assert_not_called()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_client.get", new_callable=AsyncMock)
    async def test_taxonomy_data_wrapper(self, mock_api):
        """Response wrapped in 'data' key still parses."""
        mock_api.return_value = {
            "data": [{"id": 1, "label": "IT", "synonyms": [],
                      "child": [{"id": 11, "label": "Dev",
                                 "child": [{"id": 111, "label": "SDE", "synonyms": []}]}]}]
        }
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        assert result["total_departments"] == 1
        assert result["total_roles"] == 1


# =====================================================================
# Profile prompts (recovered from tier25.py)
# =====================================================================

class TestProfilePrompts:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights._get_profile_prompts", new_callable=AsyncMock)
    async def test_profile_prompts_dispatches(self, mock_prompts):
        mock_prompts.return_value = {"status": "success", "pending_count": 2}
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="profile_prompts")
        assert result["pending_count"] == 2
        mock_prompts.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights._get_profile_prompts", new_callable=AsyncMock)
    async def test_profile_prompts_pending_states(self, mock_prompts):
        mock_prompts.return_value = {
            "status": "success", "source": "ccs_widget",
            "pending_count": 4, "completed_count": 1,
            "pending_prompts": [
                {"field": "salary_breakup", "action": "Add detailed salary breakup", "impact": "high", "reason": "test"},
            ],
            "completed_prompts": [{"field": "profile_data", "status": "done"}],
            "all_state_keys": {}, "cache_ttl_seconds": 27429, "widget_sections_count": 3,
        }
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="profile_prompts")
        assert result["pending_count"] == 4
        assert result["completed_count"] == 1
        assert result["cache_ttl_seconds"] == 27429

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights._get_profile_prompts", new_callable=AsyncMock)
    async def test_profile_prompts_all_completed(self, mock_prompts):
        mock_prompts.return_value = {
            "status": "success", "pending_count": 0, "completed_count": 5,
            "pending_prompts": [], "completed_prompts": [{"field": "f", "status": "done"}] * 5,
            "all_state_keys": {}, "cache_ttl_seconds": 10000, "widget_sections_count": 0,
        }
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="profile_prompts")
        assert result["pending_count"] == 0
        assert result["completed_count"] == 5

    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights._get_profile_prompts", new_callable=AsyncMock)
    async def test_profile_prompts_ccs_error(self, mock_prompts):
        mock_prompts.return_value = {
            "status": "error", "message": "CCS fetch failed: empty response",
            "error_code": "BROWSER_ERROR",
        }
        from naukri_server.tools.insights import naukri_insights
        result = await naukri_insights(insight_type="profile_prompts")
        assert result["status"] == "error"
        assert result["error_code"] == "BROWSER_ERROR"
