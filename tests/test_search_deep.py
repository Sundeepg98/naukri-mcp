"""Deep tests for naukri_server.tools.search — recommendation clusters, agent_eligible flag.

Every test is PURE: no network, no browser, no file I/O.
Recovered from deleted tier22.py and tier22_misc_edge.py.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# 1. Recommendation Clusters
# ---------------------------------------------------------------------------

class TestRecommendationClusters:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_client.post", new_callable=AsyncMock)
    async def test_cluster_parsing_dict(self, mock_post):
        """Clusters as dicts with count+title are parsed."""
        mock_post.return_value = {
            "jobDetails": [], "noOfJobs": 0,
            "clusters": {"apply": {"count": 64, "title": "Based on applies"}},
        }
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["clusters"]["apply"]["count"] == 64
        assert result["clusters"]["apply"]["title"] == "Based on applies"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_client.post", new_callable=AsyncMock)
    async def test_cluster_parsing_int(self, mock_post):
        """Plain int clusters are converted to count-only dicts."""
        mock_post.return_value = {"jobDetails": [], "noOfJobs": 0, "clusters": {"apply": 64}}
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["clusters"]["apply"]["count"] == 64

    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_client.post", new_callable=AsyncMock)
    async def test_agent_eligible_flag(self, mock_post):
        mock_post.return_value = {
            "jobDetails": [], "noOfJobs": 0, "clusters": {},
            "agentEligibleJobExists": True,
        }
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["agent_eligible_exists"] is True

    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_client.post", new_callable=AsyncMock)
    async def test_cluster_split_date(self, mock_post):
        mock_post.return_value = {
            "jobDetails": [], "noOfJobs": 0, "clusters": {},
            "clusterSplitDate": "2026-03-01",
        }
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["cluster_split_date"] == "2026-03-01"


# ---------------------------------------------------------------------------
# 2. Recommendation Clusters Edge Cases
# ---------------------------------------------------------------------------

class TestRecommendationClustersEdgeCases:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_client.post", new_callable=AsyncMock)
    async def test_recommended_clusters_fallback_key(self, mock_post):
        """recommendedClusters key used when clusters is absent."""
        mock_post.return_value = {
            "jobDetails": [], "noOfJobs": 0,
            "recommendedClusters": {"apply": {"count": 10, "title": "Applied Jobs"}},
        }
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["clusters"]["apply"]["count"] == 10

    @pytest.mark.asyncio
    @patch("naukri_server.tools.search.api_client.post", new_callable=AsyncMock)
    async def test_empty_clusters_handled(self, mock_post):
        """Missing clusters key entirely results in empty dict."""
        mock_post.return_value = {"jobDetails": [], "noOfJobs": 0}
        from naukri_server.tools.search import naukri_get_recommendations
        result = await naukri_get_recommendations()
        assert result["clusters"] == {}
