"""Tests for auth and apply MCP tools — action routing and parameter validation.

Every test is PURE: no network, no browser, no file I/O.
We only exercise validation / routing logic that runs BEFORE any async helper call.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager


# =====================================================================
# 1. Auth atomic tools — parameter validation and routing
# =====================================================================

class TestAuth:
    """Tests for naukri_server.tools.auth atomic tools (naukri_login, naukri_verify_otp, naukri_auth_status)."""

    @pytest.mark.asyncio
    async def test_login_empty_method_returns_validation_error(self):
        """naukri_login with empty method should return VALIDATION_ERROR."""
        from naukri_server.tools.auth import naukri_login
        result = await naukri_login(method="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "method" in result["message"]

    @pytest.mark.asyncio
    async def test_verify_otp_requires_otp(self):
        """naukri_verify_otp with empty otp should return VALIDATION_ERROR."""
        from naukri_server.tools.auth import naukri_verify_otp
        result = await naukri_verify_otp(otp="")
        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "otp" in result["message"]

    @pytest.mark.asyncio
    async def test_auth_status_routes_to_helper(self):
        """naukri_auth_status should delegate to _get_login_status."""
        from naukri_server.tools.auth import naukri_auth_status
        with patch(
            "naukri_server.tools.auth._get_login_status",
            new_callable=AsyncMock,
        ) as mock_helper:
            mock_helper.return_value = {"status": "success", "logged_in": True}
            result = await naukri_auth_status()
            mock_helper.assert_awaited_once()
            assert result["status"] == "success"
            assert result["logged_in"] is True

    @pytest.mark.asyncio
    async def test_login_routes_to_helper(self):
        """naukri_login with valid method should delegate to _login via page pool."""
        from naukri_server.tools.auth import naukri_login

        mock_page = MagicMock()

        @asynccontextmanager
        async def fake_acquire():
            yield mock_page

        with patch("naukri_server.tools.auth._login", new_callable=AsyncMock) as mock_login, \
             patch("naukri_server.tools.auth.browser_provider") as mock_provider:
            mock_provider.acquire_page = fake_acquire
            mock_login.return_value = {"status": "logged_in", "method": "google", "profile_name": "Test", "has_token": True}
            result = await naukri_login(method="google")
            mock_login.assert_awaited_once_with(mock_page, method="google", email=None, password=None)
            assert result["status"] == "logged_in"
            assert result["method"] == "google"

    @pytest.mark.asyncio
    async def test_verify_otp_routes_to_helper(self):
        """naukri_verify_otp with valid otp should delegate to _verify_otp via page pool."""
        from naukri_server.tools.auth import naukri_verify_otp

        mock_page = MagicMock()

        @asynccontextmanager
        async def fake_acquire():
            yield mock_page

        with patch("naukri_server.tools.auth._verify_otp", new_callable=AsyncMock) as mock_verify, \
             patch("naukri_server.tools.auth.browser_provider") as mock_provider:
            mock_provider.acquire_page = fake_acquire
            mock_verify.return_value = {"status": "logged_in", "profile_name": "Test", "has_token": True}
            result = await naukri_verify_otp(otp="123456")
            mock_verify.assert_awaited_once_with(mock_page, otp="123456")
            assert result["status"] == "logged_in"

    @pytest.mark.asyncio
    async def test_auth_status_api_error(self):
        """naukri_auth_status should catch NaukriAPIError and return error dict."""
        from naukri_server.tools.auth import naukri_auth_status
        from naukri_server.api import NaukriAPIError
        with patch(
            "naukri_server.tools.auth._get_login_status",
            new_callable=AsyncMock,
        ) as mock_helper:
            mock_helper.side_effect = NaukriAPIError(401, "Session expired")
            result = await naukri_auth_status()
            assert result["status"] == "error"
            # 401/403 now classify as AUTH_ERROR, not API_ERROR. The same status
            # used to mean different things depending on which dispatch path a
            # tool used; a signed-out caller got no next step. Converted, not
            # deleted -- the case still matters, the expected code changed.
            assert result["error_code"] == "AUTH_ERROR"
            assert result["http_status"] == 401


# =====================================================================
# 2. naukri_apply — parameter validation
# =====================================================================

class TestApply:
    """Tests for naukri_server.tools.apply.naukri_apply."""

    @pytest.mark.asyncio
    async def test_apply_missing_job_id(self):
        """Calling naukri_apply without job_id should raise TypeError (required param)."""
        from naukri_server.tools.apply import naukri_apply
        with pytest.raises(TypeError):
            await naukri_apply()

    @pytest.mark.asyncio
    async def test_apply_empty_job_id(self):
        """Calling naukri_apply with empty string should raise ValueError from _extract_job_id."""
        from naukri_server.tools.apply import naukri_apply
        with pytest.raises(ValueError):
            await naukri_apply(job_id="")

    @pytest.mark.asyncio
    async def test_apply_invalid_job_id(self):
        """Calling naukri_apply with non-numeric garbage should raise ValueError."""
        from naukri_server.tools.apply import naukri_apply
        with pytest.raises(ValueError):
            await naukri_apply(job_id="not-a-job-id")


# =====================================================================
# 3. naukri_batch_apply — parameter validation and limit clamping
# =====================================================================

class TestBatchApply:
    """Tests for naukri_server.tools.apply.naukri_batch_apply."""

    @pytest.mark.asyncio
    async def test_batch_apply_missing_keywords(self):
        """Calling naukri_batch_apply without keywords should raise TypeError."""
        from naukri_server.tools.apply import naukri_batch_apply
        with pytest.raises(TypeError):
            await naukri_batch_apply()

    @pytest.mark.asyncio
    async def test_batch_apply_zero_limit_clamped(self):
        """limit=0 is silently clamped to 1 by validate_limit."""
        from naukri_server.tools.apply import naukri_batch_apply
        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {"status": "success", "jobs": []}
            result = await naukri_batch_apply(keywords="python developer", limit=0)
            mock_search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_batch_apply_negative_limit_clamped(self):
        """Negative limit is silently clamped to 1 by validate_limit."""
        from naukri_server.tools.apply import naukri_batch_apply
        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = {"status": "success", "jobs": []}
            result = await naukri_batch_apply(keywords="python developer", limit=-5)
            mock_search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_batch_apply_limit_clamped(self):
        """limit > 20 should be clamped to 20 and forwarded to search."""
        from naukri_server.tools.apply import naukri_batch_apply

        with patch(
            "naukri_server.tools.search.naukri_search_jobs",
            new_callable=AsyncMock,
        ) as mock_search:
            # Search returns no jobs, so batch_apply short-circuits after search
            mock_search.return_value = {"status": "success", "jobs": []}
            result = await naukri_batch_apply(keywords="python developer", limit=50)
            # Verify search was called with limit clamped to 20
            mock_search.assert_awaited_once()
            _, kwargs = mock_search.call_args
            assert kwargs["limit"] == 20
            # Since search returns empty jobs, result should be NOT_FOUND
            assert result["status"] == "error"
            assert result["error_code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_batch_apply_search_failure(self):
        """If search fails, batch_apply should return error with search_result."""
        from naukri_server.tools.apply import naukri_batch_apply

        with patch(
            "naukri_server.tools.search.naukri_search_jobs",
            new_callable=AsyncMock,
        ) as mock_search:
            mock_search.return_value = {"status": "error", "message": "API unavailable"}
            result = await naukri_batch_apply(keywords="python developer", limit=5)
            assert result["status"] == "error"
            assert result["error_code"] == "API_ERROR"
            assert "Search failed" in result["message"]
