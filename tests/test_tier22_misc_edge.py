"""Tier 22 edge-case tests for notifications, insights, and search modules."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cache_miss():
    """Return a mock TtlCache that always calls the fetch function (cache miss)."""
    async def _call(fn):
        return await fn()
    mock_cache = MagicMock()
    mock_cache.get = AsyncMock(side_effect=_call)
    return mock_cache


# ---------------------------------------------------------------------------
# 1. notifications.py — _get_unified_notify()
# ---------------------------------------------------------------------------

class TestUnifiedNotifyEdgeCases:
    """Edge-case tests for the count-field priority logic in _get_unified_notify."""

    # ------------------------------------------------------------------
    # 1a. "latest" field is preserved when present in category data
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_latest_field_preserved(self, mock_api):
        mock_api.return_value = {
            "recoJobs": {
                "noti_count": 5,
                "latest": {"title": "SDE at Google"},
            }
        }
        from naukri_server.tools.notifications import _get_unified_notify

        result = await _get_unified_notify()

        assert result["status"] == "success"
        assert result["categories"]["recoJobs"]["latest"] == {"title": "SDE at Google"}

    # ------------------------------------------------------------------
    # 1b. total_count used as fallback when noti_count is absent
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_total_count_fallback(self, mock_api):
        # No noti_count — must fall through to total_count
        mock_api.return_value = {
            "appStatus": {
                "total_count": 42,
            }
        }
        from naukri_server.tools.notifications import _get_unified_notify

        result = await _get_unified_notify()

        assert result["status"] == "success"
        assert result["categories"]["appStatus"]["count"] == 42

    # ------------------------------------------------------------------
    # 1c. bare "count" used as last-resort fallback
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.notifications.api_get", new_callable=AsyncMock)
    async def test_bare_count_fallback(self, mock_api):
        # No noti_count, no total_count — must fall through to count
        mock_api.return_value = {
            "rmj": {
                "count": 7,
            }
        }
        from naukri_server.tools.notifications import _get_unified_notify

        result = await _get_unified_notify()

        assert result["status"] == "success"
        assert result["categories"]["rmj"]["count"] == 7


# ---------------------------------------------------------------------------
# 2. insights.py — _get_taxonomy()
# ---------------------------------------------------------------------------

class TestTaxonomyEdgeCases:
    """Edge-case tests for cache hit short-circuit and data-wrapped API response."""

    # ------------------------------------------------------------------
    # 2a. Cache hit — API must NOT be called
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_get", new_callable=AsyncMock)
    async def test_taxonomy_cache_hit(self, mock_api):
        cached_result = {
            "status": "success",
            "total_departments": 37,
            "total_roles": 1461,
            "departments": [],
        }
        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=cached_result)
        with patch("naukri_server.tools.insights._taxonomy_cache", mock_cache):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()
        assert result == cached_result
        mock_api.assert_not_called()

    # ------------------------------------------------------------------
    # 2b. Response wrapped in "data" key — parsing must still work
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.insights.api_get", new_callable=AsyncMock)
    async def test_taxonomy_data_wrapper(self, mock_api):
        # API wraps the entity list under a "data" key instead of returning a raw list
        mock_api.return_value = {
            "data": [
                {
                    "id": 1,
                    "label": "IT",
                    "synonyms": [],
                    "child": [
                        {
                            "id": 11,
                            "label": "Dev",
                            "child": [
                                {"id": 111, "label": "SDE", "synonyms": []},
                            ],
                        }
                    ],
                }
            ]
        }
        with patch("naukri_server.tools.insights._taxonomy_cache", _make_cache_miss()):
            from naukri_server.tools.insights import _get_taxonomy
            result = await _get_taxonomy()

        assert result["status"] == "success"
        assert result["total_departments"] == 1
        assert result["total_roles"] == 1


# ---------------------------------------------------------------------------
# 3. search.py — naukri_get_recommendations()
# ---------------------------------------------------------------------------

class TestRecommendationClustersEdgeCases:
    """Edge-case tests for the clusters / recommendedClusters fallback logic."""

    # ------------------------------------------------------------------
    # 3a. "recommendedClusters" key used when "clusters" is absent/falsy
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_post", new_callable=AsyncMock)
    async def test_recommended_clusters_fallback_key(self, mock_api):
        mock_api.return_value = {
            "jobDetails": [],
            "noOfJobs": 0,
            "recommendedClusters": {
                "apply": {"count": 10, "title": "Applied Jobs"},
            },
        }
        from naukri_server.tools.search import naukri_get_recommendations

        result = await naukri_get_recommendations()

        assert result["status"] == "success"
        assert result["clusters"]["apply"]["count"] == 10

    # ------------------------------------------------------------------
    # 3b. Missing clusters key entirely — result["clusters"] must be {}
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_post", new_callable=AsyncMock)
    async def test_empty_clusters_handled(self, mock_api):
        mock_api.return_value = {
            "jobDetails": [],
            "noOfJobs": 0,
        }
        from naukri_server.tools.search import naukri_get_recommendations

        result = await naukri_get_recommendations()

        assert result["status"] == "success"
        assert result["clusters"] == {}
