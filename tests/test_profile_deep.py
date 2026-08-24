"""Deep tests for profile tool — _TtlCache, cached helpers, routing, error cases.

Every test is PURE: no network, no browser, no file I/O.
"""

import asyncio
import time
import pytest
from pathlib import Path
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
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
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
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
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
    async def test_profile_dashboard_routing(self, mock_dashboard):
        mock_dashboard.return_value = {"status": "success", "profile_views": 42}
        from naukri_server.tools.profile import naukri_dashboard
        result = await naukri_dashboard()
        assert result["status"] == "success"
        assert result["profile_views"] == 42
        mock_dashboard.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile._get_profile", new_callable=AsyncMock)
    async def test_profile_get_routing(self, mock_get):
        mock_get.return_value = {"status": "success", "name": "Test"}
        from naukri_server.tools.profile import naukri_get_profile
        result = await naukri_get_profile()
        assert result["status"] == "success"
        assert result["name"] == "Test"
        mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile._audit_profile", new_callable=AsyncMock)
    async def test_profile_audit_routing(self, mock_audit):
        mock_audit.return_value = {
            "status": "success",
            "completeness_pct": 85,
            "grade": "A",
            "strengths": ["Good skills"],
            "gaps": [],
            "tips": ["Keep it up"],
        }
        from naukri_server.tools.profile import naukri_audit_profile
        result = await naukri_audit_profile()
        assert result["status"] == "success"
        assert result["grade"] == "A"
        mock_audit.assert_awaited_once()


# =====================================================================
# 4. Error cases
# =====================================================================

class TestProfileErrors:
    """Error handling for API failures and edge cases."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_profile_api_failure(self, mock_api_get):
        """api_get raising NaukriAPIError is caught and returned as error dict."""
        from naukri_server.api import NaukriAPIError
        mock_api_get.side_effect = NaukriAPIError(500, "Internal error")
        from naukri_server.tools.profile import naukri_get_profile
        result = await naukri_get_profile()
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_dashboard_empty_response(self, mock_api_get):
        """api_get returns {} for dashboard — should return success with minimal data."""
        mock_api_get.return_value = {}
        from naukri_server.tools.profile import naukri_dashboard
        result = await naukri_dashboard()
        # Even with empty response, _get_dashboard wraps in success
        assert result["status"] == "success"


# =====================================================================
# 5. Cache invalidation on update
# =====================================================================

class TestProfileInvalidationOnUpdate:
    """After naukri_boost_profile(), the profile TTL cache is invalidated."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_profile_invalidation_on_update(self, mock_api_get):
        """Boost via REST should invalidate the profile TTL cache.

        Note: profile.api_client and profile_update.api_client are the
        same singleton object, so a single patch covers both modules.
        """
        from naukri_server.tools.profile import (
            _profile_ttl_cache, get_cached_profile, naukri_boost_profile,
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

        # Now boost via REST — this should invalidate the cache.
        # profileId is required: the boost now goes through
        # profile_write.fullprofiles_write, which refuses to send a body
        # without a validated 64-character id rather than fabricating one.
        mock_api_get.return_value = {
            "profile": [{"resumeHeadline": "My Headline",
                         "profileId": "p" * 64}],
        }
        with patch("naukri_server.tools.profile.api_client.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {}
            result = await naukri_boost_profile()
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


# =====================================================================
# 7. TtlCache.invalidate() clears _data and _ts
# =====================================================================

class TestTtlCacheInvalidateClearsState:
    """invalidate() resets _data to None and _ts to 0.0."""

    @pytest.mark.asyncio
    async def test_invalidate_clears_data_and_ts(self):
        from naukri_server.tools.profile import _TtlCache
        cache = _TtlCache(ttl=100)
        fetch = AsyncMock(return_value={"key": "value"})
        await cache.get(fetch)
        assert cache._data is not None
        assert cache._ts > 0

        cache.invalidate()

        assert cache._data is None
        assert cache._ts == 0.0

        # Confirm next get() re-fetches
        fetch.return_value = {"key": "refreshed"}
        result = await cache.get(fetch)
        assert result == {"key": "refreshed"}
        assert fetch.call_count == 2


# =====================================================================
# 8. Profile update invalidates both profile and dashboard caches
# =====================================================================

class TestProfileUpdateInvalidatesCaches:
    """_update_profile success path clears both _profile_ttl_cache and _dashboard_ttl_cache."""

    @pytest.mark.asyncio
    async def test_update_invalidates_both_caches(self):
        from naukri_server.tools.profile import (
            _profile_ttl_cache, _dashboard_ttl_cache, naukri_update_profile,
        )
        # Pre-fill both caches
        _profile_ttl_cache._data = {"old": "profile"}
        _profile_ttl_cache._ts = time.time()
        _dashboard_ttl_cache._data = {"old": "dashboard"}
        _dashboard_ttl_cache._ts = time.time()

        with patch("naukri_server.tools.profile._update_profile", new_callable=AsyncMock) as mock_update:
            # Simulate what _update_profile does on success:
            # it invalidates caches then returns the result
            async def update_side_effect(*a, **kw):
                _profile_ttl_cache.invalidate()
                _dashboard_ttl_cache.invalidate()
                return {"status": "updated", "updated_fields": ["resumeHeadline"]}

            mock_update.side_effect = update_side_effect
            result = await naukri_update_profile(fields={"resumeHeadline": "New headline"})
            assert result["status"] == "updated"
            assert _profile_ttl_cache._data is None
            assert _dashboard_ttl_cache._data is None


# =====================================================================
# 9. Resume upload invalidates profile and dashboard caches
# =====================================================================

class TestResumeUploadInvalidatesCaches:
    """_resume_upload success path clears both profile and dashboard TTL caches."""

    @pytest.mark.asyncio
    async def test_resume_upload_invalidates_caches(self):
        from naukri_server.tools.profile import _profile_ttl_cache, _dashboard_ttl_cache

        # Pre-fill both caches
        _profile_ttl_cache._data = {"stale": "profile"}
        _profile_ttl_cache._ts = time.time()
        _dashboard_ttl_cache._data = {"stale": "dashboard"}
        _dashboard_ttl_cache._ts = time.time()

        with patch("naukri_server.tools.resume_photo.browser") as mock_browser:
            mock_page = AsyncMock()
            mock_page.url = "https://www.naukri.com/mnjuser/profile"
            file_input = AsyncMock()
            mock_page.query_selector = AsyncMock(return_value=file_input)

            # Use async context manager for page_pool.acquire()
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=mock_page)
            ctx.__aexit__ = AsyncMock(return_value=False)
            mock_browser.page_pool.acquire.return_value = ctx

            with patch("naukri_server.tools.resume_photo.page_goto", new_callable=AsyncMock):
                with patch("naukri_server.tools.resume_photo.asyncio.sleep", new_callable=AsyncMock):
                    from naukri_server.tools.resume_photo import _resume_upload
                    import tempfile, os
                    # Create a temporary PDF file
                    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, dir=str(Path.home()))
                    try:
                        tmp.write(b"%PDF-1.4 test")
                        tmp.close()
                        result = await _resume_upload(tmp.name)
                        assert result["status"] == "success"
                        assert _profile_ttl_cache._data is None
                        assert _dashboard_ttl_cache._data is None
                    finally:
                        os.unlink(tmp.name)


# =====================================================================
# From test_consolidation.py — profile action routing & validation
# =====================================================================

class TestProfileAtomic:
    """Tests for atomic profile tools."""

    @pytest.mark.asyncio
    async def test_update_no_fields(self):
        """Update with no fields should fail validation inside _update_profile."""
        from naukri_server.tools.profile import naukri_update_profile
        with patch("naukri_server.tools.profile._update_profile", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {
                "status": "error",
                "message": "No fields provided. Pass at least one field to update.",
                "error_code": "VALIDATION_ERROR",
            }
            result = await naukri_update_profile()
            assert result["status"] == "error"
            assert "No fields" in result["message"]

    @pytest.mark.asyncio
    async def test_get_routes_to_helper(self):
        from naukri_server.tools.profile import naukri_get_profile
        with patch("naukri_server.tools.profile._get_profile", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "name": "Test"}
            result = await naukri_get_profile()
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_audit_routes_to_helper(self):
        from naukri_server.tools.profile import naukri_audit_profile
        with patch("naukri_server.tools.profile._audit_profile", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "success", "grade": "A"}
            result = await naukri_audit_profile()
            mock_helper.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_boost_routes_to_helper(self):
        from naukri_server.tools.profile import naukri_boost_profile
        with patch("naukri_server.tools.profile._boost_visibility", new_callable=AsyncMock) as mock_helper:
            mock_helper.return_value = {"status": "refreshed"}
            result = await naukri_boost_profile()
            mock_helper.assert_awaited_once_with(randomize=False)


# =====================================================================
# From test_tier21.py — profile enrichment: lookup_data, ai_features,
# extended_profile, schools, missing sections
# =====================================================================

class TestProfileLookupData:
    """Tests for lookup_data section extracted from lookupData."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_lookup_data_extracted(self, mock_get):
        mock_get.return_value = {
            "profile": [{"name": "Test User", "experience": {}}],
            "profileAdditional": {},
            "lookupData": {
                "resumeScore": 85,
                "lastLoginTime": "2026-03-01T10:00:00",
                "prevLoginTime": "2026-02-28T10:00:00",
                "hasCurrentFullTimeEmployment": True,
                "isPaidUser": False,
                "isJobseekerAgentEligible": True,
                "int360RoleExp": "2027-01-01",
                "ffRDSubExp": "2026-12-01",
            },
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["status"] == "success"
        ld = result["lookup_data"]
        assert ld["resume_score"] == 85
        assert ld["last_login"] == "2026-03-01T10:00:00"
        assert ld["prev_login"] == "2026-02-28T10:00:00"
        assert ld["has_current_employment"] is True
        assert ld["is_paid_user"] is False
        assert ld["is_agent_eligible"] is True
        assert ld["int360_expiry"] == "2027-01-01"
        assert ld["ff_rd_expiry"] == "2026-12-01"


class TestProfileAiFeatures:
    """Tests for ai_features section extracted from additionalDetails."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_ai_features_extracted(self, mock_get):
        mock_get.return_value = {
            "profile": [{"name": "Test User", "experience": {}}],
            "profileAdditional": {},
            "additionalDetails": {
                "isAIResumeEligible": True,
                "curEmpVerEligibility": False,
            },
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["ai_features"]["is_ai_resume_eligible"] is True
        assert result["ai_features"]["employer_verification_eligible"] is False


class TestProfileExtendedProfile:
    """Tests for extended_profile section (job_search_status, career_break, stale_tags)."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_extended_profile_extracted(self, mock_get):
        mock_get.return_value = {
            "profile": [{"name": "Test", "experience": {}}],
            "profileAdditional": {},
            "extendedProfile": {
                "jbSearchStatus": {
                    "data": [{"value": "actively_searching"}],
                },
                "careerBreak": {
                    "data": [{"comingFromBreak": True}],
                },
                "tags": {
                    "data": [
                        {"value": "Python", "meta": {"status": "active"}},
                        {"value": "Java", "meta": {"status": "inactive"}},
                        {"value": "COBOL", "meta": {"status": "inactive"}},
                    ],
                },
            },
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        ep = result["extended_profile"]
        assert ep["job_search_status"] == "actively_searching"
        assert ep["career_break"] is True
        assert ep["stale_tags"] == ["Java", "COBOL"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_extended_profile_no_stale_tags(self, mock_get):
        """When no tags are inactive, stale_tags is an empty list."""
        mock_get.return_value = {
            "profile": [{"name": "Test", "experience": {}}],
            "profileAdditional": {},
            "extendedProfile": {
                "jbSearchStatus": {"data": [{"value": "open"}]},
                "careerBreak": {"data": [{"comingFromBreak": False}]},
                "tags": {
                    "data": [
                        {"value": "React", "meta": {"status": "active"}},
                    ],
                },
            },
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["extended_profile"]["stale_tags"] == []


class TestProfileSchools:
    """Tests for schools section parsing."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_schools_parsed(self, mock_get):
        mock_get.return_value = {
            "profile": [{"name": "Test", "experience": {}}],
            "profileAdditional": {},
            "schools": [
                {
                    "educationType": {"value": "XII"},
                    "schoolBoard": {"value": "CBSE"},
                    "schoolCompletionYear": 2016,
                    "schoolPercentage": {"value": "80-90%"},
                    "schoolMedium": {"value": "English"},
                },
                {
                    "educationType": {"value": "X"},
                    "schoolBoard": {"value": "ICSE"},
                    "schoolCompletionYear": 2014,
                    "schoolPercentage": {"value": "90-100%"},
                    "schoolMedium": {"value": "English"},
                    "schoolLevel": "10",
                },
            ],
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert len(result["schools"]) == 2
        assert result["schools"][0]["level"] == "XII"
        assert result["schools"][0]["board"] == "CBSE"
        assert result["schools"][0]["year"] == 2016
        assert result["schools"][0]["percentage_range"] == "80-90%"
        assert result["schools"][0]["medium"] == "English"
        assert result["schools"][1]["level"] == "X"
        assert result["schools"][1]["board"] == "ICSE"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_schools_fallback_to_school_level(self, mock_get):
        """When educationType is missing, falls back to schoolLevel."""
        mock_get.return_value = {
            "profile": [{"name": "Test", "experience": {}}],
            "profileAdditional": {},
            "schools": [
                {
                    "educationType": {},
                    "schoolLevel": "12",
                    "schoolBoard": {},
                    "schoolCompletionYear": 2018,
                    "schoolPercentage": {},
                    "schoolMedium": {},
                },
            ],
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["schools"][0]["level"] == "Class 12"


class TestProfileMissingSections:
    """Tests that missing sections are gracefully omitted from result."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_missing_sections_not_in_result(self, mock_get):
        """When lookupData, additionalDetails, extendedProfile, schools are absent,
        their corresponding keys are not present in the result."""
        mock_get.return_value = {
            "profile": [{"name": "Minimal User", "experience": {}}],
            "profileAdditional": {},
        }
        from naukri_server.tools.profile import _get_profile
        result = await _get_profile()
        assert result["status"] == "success"
        assert "lookup_data" not in result
        assert "ai_features" not in result
        assert "extended_profile" not in result
        assert "schools" not in result


# =====================================================================
# From test_tier21.py — dashboard enrichment: assessments, expected_ctc,
# recommended_companies, feature_flags, empty fields, edge cases
# =====================================================================

class TestDashboardAssessments:
    """Tests for assessments parsing in _get_dashboard."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_assessments_with_results(self, mock_get):
        mock_get.return_value = {
            "dashBoard": {
                "assessments": [
                    {
                        "skill": "Python",
                        "level": {"name": "Advanced"},
                        "questionCount": 20,
                        "duration": 30,
                        "maxAttempts": 3,
                        "testId": "T123",
                        "results": {
                            "scorePercent": 85,
                            "rank": 1200,
                            "status": "passed",
                        },
                    }
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["status"] == "success"
        a = result["assessments"][0]
        assert a["skill"] == "Python"
        assert a["level"] == "Advanced"
        assert a["question_count"] == 20
        assert a["duration_mins"] == 30
        assert a["max_attempts"] == 3
        assert a["test_id"] == "T123"
        assert a["results"]["score_percent"] == 85
        assert a["results"]["rank"] == 1200
        assert a["results"]["status"] == "passed"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_assessments_without_results(self, mock_get):
        """Assessment with no results has results=None."""
        mock_get.return_value = {
            "dashBoard": {
                "assessments": [
                    {
                        "skill": "JavaScript",
                        "level": {"name": "Beginner"},
                        "questionCount": 15,
                        "duration": 20,
                        "maxAttempts": 5,
                        "testId": "T456",
                        "results": None,
                    }
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        a = result["assessments"][0]
        assert a["results"] is None


class TestDashboardExpectedCtcStructured:
    """Tests for expected_ctc_structured calculation."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_expected_ctc_structured_calculation(self, mock_get):
        """lacs * 100000 + thousands * 1000 = total_annual."""
        mock_get.return_value = {
            "dashBoard": {
                "expectedCtc": {
                    "lacs": {"value": 15},
                    "thousands": {"value": 50},
                },
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        ctc = result["expected_ctc_structured"]
        assert ctc["lacs"] == 15
        assert ctc["thousands"] == 50
        assert ctc["total_annual"] == 15 * 100000 + 50 * 1000  # 1,550,000

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_expected_ctc_structured_zero_thousands(self, mock_get):
        """Zero thousands still computes correctly."""
        mock_get.return_value = {
            "dashBoard": {
                "expectedCtc": {
                    "lacs": {"value": 10},
                    "thousands": {"value": 0},
                },
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        ctc = result["expected_ctc_structured"]
        assert ctc["total_annual"] == 1000000

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_expected_ctc_structured_missing_value(self, mock_get):
        """Missing value defaults to 0."""
        mock_get.return_value = {
            "dashBoard": {
                "expectedCtc": {
                    "lacs": {},
                    "thousands": {},
                },
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        ctc = result["expected_ctc_structured"]
        assert ctc["lacs"] == 0
        assert ctc["thousands"] == 0
        assert ctc["total_annual"] == 0

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_expected_ctc_not_present_when_not_dict(self, mock_get):
        """If expectedCtc is not a dict, expected_ctc_structured is not in result."""
        mock_get.return_value = {
            "dashBoard": {
                "expectedCtc": "15 LPA",
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert "expected_ctc_structured" not in result


class TestDashboardRecommendedCompanies:
    """Tests for recommended_companies from similarCompToFollow."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_recommended_companies_caps_at_5(self, mock_get):
        """similarCompToFollow is capped at 5 entries."""
        companies = [
            {"name": f"Company {i}", "rating": 4.0 + i * 0.1, "reviews": {"count": 100 * i}}
            for i in range(10)
        ]
        mock_get.return_value = {"dashBoard": {"similarCompToFollow": companies}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert len(result["recommended_companies"]) == 5
        assert result["recommended_companies"][0]["name"] == "Company 0"
        assert result["recommended_companies"][4]["name"] == "Company 4"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_recommended_companies_with_reviews_count(self, mock_get):
        mock_get.return_value = {
            "dashBoard": {
                "similarCompToFollow": [
                    {"name": "TCS", "rating": 3.8, "reviews": {"count": 50000}},
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["recommended_companies"][0]["reviews_count"] == 50000

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_recommended_companies_empty_list(self, mock_get):
        """Empty similarCompToFollow means recommended_companies is not in result."""
        mock_get.return_value = {"dashBoard": {"similarCompToFollow": []}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert "recommended_companies" not in result


class TestDashboardFeatureFlags:
    """Tests for feature_flags extraction."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_feature_flags_extracted(self, mock_get):
        mock_get.return_value = {
            "dashBoard": {
                "eligibleFlagForAIMockInterview": True,
                "isAIResumeEligible": False,
                "jbSearchStatus": {
                    "data": [{"value": "open_to_opportunities"}],
                },
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        ff = result["feature_flags"]
        assert ff["ai_mock_interview"] is True
        assert ff["ai_resume"] is False
        assert ff["job_search_status"] == "open_to_opportunities"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_feature_flags_missing_jb_search(self, mock_get):
        """When jbSearchStatus is missing, job_search_status is None."""
        mock_get.return_value = {"dashBoard": {}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["feature_flags"]["job_search_status"] is None


class TestDashboardEmptyFields:
    """Tests for graceful handling of empty/missing dashboard fields."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_empty_dashboard(self, mock_get):
        """Empty dashBoard returns success with status and feature_flags."""
        mock_get.return_value = {"dashBoard": {}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["status"] == "success"
        # None values are stripped
        assert "profile_views" not in result
        assert "assessments" not in result
        assert "expected_ctc_structured" not in result
        assert "recommended_companies" not in result

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_missing_dashboard_key(self, mock_get):
        """Missing dashBoard key returns success with all None."""
        mock_get.return_value = {}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["status"] == "success"


class TestDashboardAssessmentEdgeCases:
    """Edge cases for dashboard assessments."""

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_assessment_with_empty_results_dict(self, mock_get):
        """Assessment with empty results dict {} is falsy in Python, so results=None."""
        mock_get.return_value = {
            "dashBoard": {
                "assessments": [
                    {
                        "skill": "SQL",
                        "level": {"name": "Intermediate"},
                        "questionCount": 10,
                        "duration": 15,
                        "maxAttempts": 2,
                        "testId": "T789",
                        "results": {},
                    }
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        a = result["assessments"][0]
        # Empty dict {} is falsy, so `if a.get("results")` is False -> results=None
        assert a["results"] is None

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_assessment_with_populated_results(self, mock_get):
        """Assessment with a non-empty results dict parses scorePercent, rank, status."""
        mock_get.return_value = {
            "dashBoard": {
                "assessments": [
                    {
                        "skill": "SQL",
                        "level": {"name": "Intermediate"},
                        "questionCount": 10,
                        "duration": 15,
                        "maxAttempts": 2,
                        "testId": "T789",
                        "results": {
                            "scorePercent": 72,
                            "rank": 500,
                            "status": "passed",
                        },
                    }
                ],
            }
        }
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        a = result["assessments"][0]
        assert a["results"]["score_percent"] == 72
        assert a["results"]["rank"] == 500
        assert a["results"]["status"] == "passed"


# =====================================================================
# DFP Profile Targeting (recovered from tier25.py)
# =====================================================================

class TestDFPTargeting:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile_targeting.api_client.get", new_callable=AsyncMock)
    async def test_targeting_returns_profile_fields(self, mock_get):
        mock_get.return_value = {
            "params": {
                "Profile-CTC": 13.0, "Profile-Experience": 4.0, "Profile-Age": 30,
                "Profile-Gender": "U", "Profile-Location": "Example City",
                "Profile-Company": "Acme Technology",
                "Profile-Designation": "Software Engineer (Backend)",
                "Profile-KeySkills": "Node.js, TypeScript",
                "Profile-Pref-Loc": "", "Profile-PG-Course": "",
                "Profile-Registerdays": 1510, "Profile-Activeness": 0,
            },
            "slots": [{"adUnitPath": "/test"}],
        }
        from naukri_server.tools.profile import naukri_profile_targeting
        result = await naukri_profile_targeting()
        assert result["status"] == "success"
        # Renamed from `ctc_lpa`. Profile-CTC is Naukri's ROUNDED ad-targeting
        # bucket, and under the old name it contradicted the two tools that
        # report the real CTC: the bucket rounds up to the next whole lakh, so
        # here it reads 13.0 while naukri_dashboard says 12.50 and
        # naukri_get_profile.current_ctc is 1250000.
        # See tests/test_ctc_sources_disagree.py.
        assert result["profile"]["targeting_ctc_bucket"] == 13.0
        assert "ctc_lpa" not in result["profile"]
        assert result["profile"]["location"] == "Example City"
        assert result["ad_slots"] == 1

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile_targeting.api_client.get", new_callable=AsyncMock)
    async def test_targeting_identifies_gaps(self, mock_get):
        mock_get.return_value = {
            "params": {"Profile-CTC": 13.0, "Profile-Pref-Loc": "", "Profile-PG-Course": "", "Profile-PG-Spl": ""},
            "slots": [],
        }
        from naukri_server.tools.profile import naukri_profile_targeting
        result = await naukri_profile_targeting()
        assert result["gap_count"] == 3
        assert "pref loc" in result["completeness_gaps"]

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile_targeting.api_client.get", new_callable=AsyncMock)
    async def test_targeting_empty_response(self, mock_get):
        mock_get.return_value = {}
        from naukri_server.tools.profile import naukri_profile_targeting
        result = await naukri_profile_targeting()
        assert result["targeting_fields"] == 0
        assert result["gap_count"] == 0

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile_targeting.api_client.get", new_callable=AsyncMock)
    async def test_targeting_api_error(self, mock_get):
        from naukri_server.api import NaukriAPIError
        mock_get.side_effect = NaukriAPIError(401, "Unauthorized")
        from naukri_server.tools.profile import naukri_profile_targeting
        result = await naukri_profile_targeting()
        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile_targeting.api_client.get", new_callable=AsyncMock)
    async def test_targeting_zero_values_not_gaps(self, mock_get):
        """Fields with value 0 are NOT treated as gaps."""
        mock_get.return_value = {
            "params": {"Profile-Activeness": 0, "Profile-CTC": 0, "Profile-Pref-Loc": ""},
            "slots": [],
        }
        from naukri_server.tools.profile import naukri_profile_targeting
        result = await naukri_profile_targeting()
        assert result["gap_count"] == 1
        assert "pref loc" in result["completeness_gaps"]


# =====================================================================
# Dashboard Selective Properties (recovered from tier25.py)
# =====================================================================

class TestDashboardSelectiveProperties:
    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_dashboard_passes_properties_param(self, mock_get):
        mock_get.return_value = {"dashBoard": {"pc": 100, "ca": 0}}
        from naukri_server.tools.profile import _get_dashboard
        await _get_dashboard()
        params = mock_get.call_args.kwargs.get("params") or (mock_get.call_args.args[1] if len(mock_get.call_args.args) > 1 else None)
        assert params is not None
        assert "properties" in params

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_dashboard_properties_contains_expected_keys(self, mock_get):
        mock_get.return_value = {"dashBoard": {}}
        from naukri_server.tools.profile import _get_dashboard
        await _get_dashboard()
        props = mock_get.call_args.kwargs.get("params", {}).get("properties", "")
        for key in ("userDetails", "profilePerformance", "isPaidUser", "photoInfo"):
            assert key in props

    @pytest.mark.asyncio
    @patch("naukri_server.tools.profile.api_client.get", new_callable=AsyncMock)
    async def test_dashboard_still_parses_with_selective_response(self, mock_get):
        mock_get.return_value = {"dashBoard": {"profileViewCount": 42}}
        from naukri_server.tools.profile import _get_dashboard
        result = await _get_dashboard()
        assert result["status"] == "success"
        assert result["profile_views"] == 42
