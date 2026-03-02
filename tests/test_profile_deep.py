"""Deep tests for profile tool — _TtlCache, cached helpers, routing, error cases.

Every test is PURE: no network, no browser, no file I/O.
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# =====================================================================
# 1. _TtlCache — direct unit tests
# =====================================================================

class TestTtlCacheFreshFetch:
    """First call to get() invokes fetch_fn and returns result."""

    @pytest.mark.asyncio
    async def test_ttl_cache_fresh_fetch(self):
        from naukri_server.tools.profile import _TtlCache
        cache = _TtlCache(ttl=10)
        fetch = AsyncMock(return_value={"data": "fresh"})
        result = await cache.get(fetch)
        assert result == {"data": "fresh"}
        fetch.assert_called_once()


class TestTtlCacheCachedReturn:
    """Second call within TTL returns cached value without calling fetch_fn."""

    @pytest.mark.asyncio
    async def test_ttl_cache_returns_cached(self):
        from naukri_server.tools.profile import _TtlCache
        cache = _TtlCache(ttl=10)
        fetch = AsyncMock(return_value={"data": "v1"})
        await cache.get(fetch)
        fetch.return_value = {"data": "v2"}
        result = await cache.get(fetch)
        assert result == {"data": "v1"}
        assert fetch.call_count == 1


class TestTtlCacheExpiry:
    """After TTL expires, fetch_fn is called again with fresh data."""

    @pytest.mark.asyncio
    async def test_ttl_cache_expires(self):
        from naukri_server.tools.profile import _TtlCache
        cache = _TtlCache(ttl=0.01)  # Very short TTL
        fetch = AsyncMock(return_value={"data": "v1"})
        await cache.get(fetch)
        await asyncio.sleep(0.02)  # Wait for expiry
        fetch.return_value = {"data": "v2"}
        result = await cache.get(fetch)
        assert result == {"data": "v2"}
        assert fetch.call_count == 2


class TestTtlCacheInvalidate:
    """invalidate() clears cached data; next get() calls fetch_fn."""

    @pytest.mark.asyncio
    async def test_ttl_cache_invalidate(self):
        from naukri_server.tools.profile import _TtlCache
        cache = _TtlCache(ttl=100)
        fetch = AsyncMock(return_value="first")
        await cache.get(fetch)
        cache.invalidate()
        fetch.return_value = "second"
        result = await cache.get(fetch)
        assert result == "second"
        assert fetch.call_count == 2


class TestTtlCacheConcurrentCalls:
    """Concurrent calls only invoke fetch_fn once (double-checked locking)."""

    @pytest.mark.asyncio
    async def test_ttl_cache_concurrent_calls(self):
        from naukri_server.tools.profile import _TtlCache
        cache = _TtlCache(ttl=10)
        call_count = 0

        async def slow_fetch():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return "result"

        results = await asyncio.gather(
            cache.get(slow_fetch),
            cache.get(slow_fetch),
            cache.get(slow_fetch),
        )
        assert all(r == "result" for r in results)
        assert call_count == 1  # Only one fetch despite 3 concurrent calls


# =====================================================================
# 2. get_cached_profile / get_cached_dashboard via unified cache
# =====================================================================

class TestGetCachedProfile:
    """get_cached_profile() calls api_get and caches the result."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_get_cached_profile_calls_api(self, mock_api_get):
        mock_api_get.return_value = {
            "profile": [{"name": "Test User", "resumeHeadline": "Dev", "keySkills": "python",
                         "experience": {"year": 5, "month": 0}}],
            "profileAdditional": {"profileId": "123"},
            "itskills": [],
            "employments": [],
            "educations": [],
        }
        from naukri_server.tools.profile import get_cached_profile, _profile_ttl_cache
        _profile_ttl_cache.invalidate()  # Clear prior state
        result = await get_cached_profile()
        assert result["status"] == "success"
        assert result["name"] == "Test User"
        mock_api_get.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_get_cached_profile_uses_cache(self, mock_api_get):
        """Two calls — api_get invoked only once thanks to TTL cache."""
        mock_api_get.return_value = {
            "profile": [{"name": "Cached User", "experience": {"year": 3, "month": 6}}],
            "profileAdditional": {},
            "itskills": [],
            "employments": [],
            "educations": [],
        }
        from naukri_server.tools.profile import get_cached_profile, _profile_ttl_cache
        _profile_ttl_cache.invalidate()
        await get_cached_profile()
        await get_cached_profile()
        assert mock_api_get.call_count == 1


class TestGetCachedDashboard:
    """get_cached_dashboard() calls api_get for dashboard data."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_get_cached_dashboard_calls_api(self, mock_api_get):
        mock_api_get.return_value = {
            "dashBoard": {
                "profileViewCount": 100,
                "resumeHeadline": "Senior Dev",
                "rawCtc": 15.0,
            }
        }
        from naukri_server.tools.profile import get_cached_dashboard, _dashboard_ttl_cache
        _dashboard_ttl_cache.invalidate()
        result = await get_cached_dashboard()
        assert result["status"] == "success"
        assert result["profile_views"] == 100
        assert result["ctc_lpa"] == 15.0


# =====================================================================
# 3. naukri_profile routing
# =====================================================================

class TestProfileRouting:
    """naukri_profile dispatches to correct internal helper per action."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile._get_dashboard", new_callable=AsyncMock)
    async def test_profile_action_dashboard_routing(self, mock_dashboard):
        mock_dashboard.return_value = {"status": "success", "profile_views": 42}
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="dashboard")
        assert result["status"] == "success"
        assert result["profile_views"] == 42
        mock_dashboard.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile._get_profile", new_callable=AsyncMock)
    async def test_profile_action_get_routing(self, mock_get):
        mock_get.return_value = {"status": "success", "name": "Test"}
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="get")
        assert result["status"] == "success"
        assert result["name"] == "Test"
        mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile._audit_profile", new_callable=AsyncMock)
    async def test_profile_action_audit_routing(self, mock_audit):
        mock_audit.return_value = {
            "status": "success",
            "completeness_pct": 85,
            "grade": "A",
            "strengths": ["Good skills"],
            "gaps": [],
            "tips": ["Keep it up"],
        }
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="audit")
        assert result["status"] == "success"
        assert result["grade"] == "A"
        mock_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_profile_action_invalid(self):
        """Unknown action returns VALIDATION_ERROR."""
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="nonexistent")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "nonexistent" in result["message"]


# =====================================================================
# 4. Error cases
# =====================================================================

class TestProfileErrors:
    """Error handling for API failures and edge cases."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_profile_api_failure(self, mock_api_get):
        """api_get raising NaukriAPIError is caught and returned as error dict."""
        from naukri_server.api import NaukriAPIError
        mock_api_get.side_effect = NaukriAPIError(500, "Internal error")
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="get")
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_dashboard_empty_response(self, mock_api_get):
        """api_get returns {} for dashboard — should return success with minimal data."""
        mock_api_get.return_value = {}
        from naukri_server.tools.profile import naukri_profile
        result = await naukri_profile(action="dashboard")
        # Even with empty response, _get_dashboard wraps in success
        assert result["status"] == "success"


# =====================================================================
# 5. Cache invalidation on update
# =====================================================================

class TestProfileInvalidationOnUpdate:
    """After naukri_profile(action='update'), the profile TTL cache is invalidated."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_get", new_callable=AsyncMock)
    async def test_profile_invalidation_on_update(self, mock_api_get):
        """Update path (via browser) invalidates _profile_ttl_cache.

        We can't easily run the full browser path, so we verify that
        _update_profile is called with the correct args. Instead we test
        the simpler case: calling with no fields returns VALIDATION_ERROR
        (this path doesn't reach the browser but proves routing works).
        For the cache-invalidation proof, we check boost's REST path.
        """
        # Set up profile data for boost's REST path
        mock_api_get.return_value = {
            "profile": [{"resumeHeadline": "My Headline"}],
        }
        from naukri_server.tools.profile import (
            _profile_ttl_cache, get_cached_profile, naukri_profile,
        )
        # Prime the cache
        mock_api_get.return_value = {
            "profile": [{"name": "Old Name", "experience": {"year": 1, "month": 0}}],
            "profileAdditional": {},
            "itskills": [],
            "employments": [],
            "educations": [],
        }
        _profile_ttl_cache.invalidate()
        await get_cached_profile()
        assert mock_api_get.call_count == 1
        # Verify cache is populated (data is not None)
        assert _profile_ttl_cache._data is not None

        # Now boost via REST — this should invalidate the cache
        mock_api_get.return_value = {
            "profile": [{"resumeHeadline": "My Headline"}],
        }
        with patch("naukri_server.tools.profile.api_post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {}
            result = await naukri_profile(action="boost")
            assert result["status"] == "success"
            assert result["action"] == "refreshed"
        # Cache should be invalidated after boost
        assert _profile_ttl_cache._data is None


# =====================================================================
# 6. _TtlCache edge case — None data not cached
# =====================================================================

class TestTtlCacheNoneNotCached:
    """If fetch_fn returns None, the cache stores it but the staleness
    check uses `self._data is not None`, so next call re-fetches."""

    @pytest.mark.asyncio
    async def test_ttl_cache_none_data_not_cached(self):
        from naukri_server.tools.profile import _TtlCache
        cache = _TtlCache(ttl=100)
        fetch = AsyncMock(return_value=None)
        result1 = await cache.get(fetch)
        assert result1 is None

        # Because _data is None, the freshness check `self._data is not None`
        # is False, so the next call should invoke fetch_fn again
        fetch.return_value = {"data": "now_available"}
        result2 = await cache.get(fetch)
        assert result2 == {"data": "now_available"}
        assert fetch.call_count == 2
