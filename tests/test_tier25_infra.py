"""Tests for Tier 25 Phase 4: browser.py (TokenManager, PagePool) and auth_bridge.py.

Every test is PURE: no network, no browser, no file I/O.
"""

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from playwright._impl._errors import TargetClosedError


# ═══════════════════════════════════════════════════════════════════════
# 1. TokenManager Tests
# ═══════════════════════════════════════════════════════════════════════

class TestTokenManagerExtract:
    """Token extraction from browser context cookies."""

    @pytest.mark.asyncio
    async def test_extract_finds_nauk_at(self):
        """Extract sets token when nauk_at cookie exists."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[
            {"name": "nauk_at", "value": "jwt-token-123"},
            {"name": "other", "value": "val"},
        ])
        tm.bind(ctx)
        with patch.object(tm, "_export_auth_state"):
            await tm.extract()
        assert tm._token == "jwt-token-123"
        assert "nauk_at=jwt-token-123" in tm._cookies

    @pytest.mark.asyncio
    async def test_extract_no_nauk_at(self):
        """Extract sets token to None when nauk_at cookie missing."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[
            {"name": "other_cookie", "value": "val"},
        ])
        tm.bind(ctx)
        await tm.extract()
        assert tm._token is None

    @pytest.mark.asyncio
    async def test_extract_no_context(self):
        """Extract is no-op when context not bound."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        await tm.extract()
        assert tm._token is None
        assert tm._cookies is None

    @pytest.mark.asyncio
    async def test_extract_handles_exception(self):
        """Extract handles cookie extraction errors gracefully."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(side_effect=RuntimeError("browser crashed"))
        tm.bind(ctx)
        await tm.extract()
        assert tm._token is None


class TestTokenManagerGetToken:
    """Token retrieval with validation."""

    def test_get_token_returns_cached(self):
        """get_token returns cached token."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "my-token"
        assert tm.get_token() == "my-token"

    def test_get_token_raises_when_empty(self):
        """get_token raises ValueError when no token."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        with pytest.raises(ValueError, match="Not logged in"):
            tm.get_token()

    def test_get_cookies_returns_empty_when_none(self):
        """get_cookies returns empty string when no cookies."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        assert tm.get_cookies() == ""

    def test_get_cookies_returns_cached(self):
        """get_cookies returns cached cookie string."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._cookies = "a=1; b=2"
        assert tm.get_cookies() == "a=1; b=2"


class TestTokenManagerInvalidate:
    """Token invalidation."""

    def test_invalidate_clears_token(self):
        """invalidate() sets token to None."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "active-token"
        tm.invalidate()
        assert tm._token is None


class TestTokenManagerRefresh:
    """Token refresh via page reload."""

    @pytest.mark.asyncio
    async def test_refresh_reloads_and_extracts(self):
        """refresh() reloads page and re-extracts token."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = None
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[
            {"name": "nauk_at", "value": "refreshed-token"},
        ])
        tm.bind(ctx)

        page = AsyncMock()
        with patch.object(tm, "_export_auth_state"):
            result = await tm.refresh(page)

        page.reload.assert_called_once()
        assert result == "refreshed-token"

    @pytest.mark.asyncio
    async def test_refresh_skips_if_already_valid(self):
        """refresh() returns cached token if already valid (double-check lock)."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "already-valid"
        page = AsyncMock()
        result = await tm.refresh(page)
        assert result == "already-valid"
        page.reload.assert_not_called()


class TestTokenManagerEnsureToken:
    """ensure_token with extraction + renewal fallback."""

    @pytest.mark.asyncio
    async def test_returns_cached_token(self):
        """ensure_token returns immediately if token cached."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "cached"
        result = await tm.ensure_token()
        assert result == "cached"

    @pytest.mark.asyncio
    async def test_extracts_on_missing(self):
        """ensure_token extracts from context if token missing."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[
            {"name": "nauk_at", "value": "extracted"},
        ])
        tm.bind(ctx)
        with patch.object(tm, "_export_auth_state"):
            result = await tm.ensure_token()
        assert result == "extracted"

    @pytest.mark.asyncio
    async def test_raises_when_all_fails(self):
        """ensure_token raises ValueError when extraction and renewal both fail."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[])  # No nauk_at
        ctx.pages = []
        ctx.new_page = AsyncMock(return_value=AsyncMock())
        tm.bind(ctx)
        with pytest.raises(ValueError, match="Not logged in"):
            await tm.ensure_token()


# ═══════════════════════════════════════════════════════════════════════
# 2. PagePool Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPagePoolAcquire:
    """Page checkout/return and pool management."""

    @pytest.mark.asyncio
    async def test_acquire_returns_initialized_page(self):
        """acquire() returns the seeded page."""
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        pool = PagePool(ctx, max_pages=3)
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        await pool.initialize(mock_page)

        async with pool.acquire() as page:
            assert page is mock_page
        assert pool._checkouts == 1
        assert pool._returns == 1

    @pytest.mark.asyncio
    async def test_acquire_creates_new_page_if_pool_empty(self):
        """acquire() creates new page when pool empty and under limit."""
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        new_page = MagicMock()
        new_page.url = "about:blank"
        ctx.new_page = AsyncMock(return_value=new_page)
        pool = PagePool(ctx, max_pages=3)

        async with pool.acquire() as page:
            assert page is new_page
        assert len(pool._all_pages) == 1

    @pytest.mark.asyncio
    async def test_acquire_recovers_crashed_page(self):
        """acquire() replaces a crashed page with a new one."""
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        recovered = MagicMock()
        recovered.url = "about:blank"
        ctx.new_page = AsyncMock(return_value=recovered)
        pool = PagePool(ctx, max_pages=3)

        crashed_page = MagicMock()
        type(crashed_page).url = PropertyMock(side_effect=TargetClosedError())
        await pool.initialize(crashed_page)

        async with pool.acquire() as page:
            assert page is recovered
        assert pool._crashes == 1

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        """get_stats() returns accurate counters."""
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        pool = PagePool(ctx, max_pages=2)
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        await pool.initialize(mock_page)

        async with pool.acquire() as _:
            pass
        async with pool.acquire() as _:
            pass

        stats = pool.get_stats()
        assert stats["checkouts"] == 2
        assert stats["returns"] == 2
        assert stats["crashes"] == 0

    @pytest.mark.asyncio
    async def test_close_all_clears_pages(self):
        """close_all() closes all pages and empties pool."""
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        pool = PagePool(ctx, max_pages=3)
        mock_page = AsyncMock()
        mock_page.url = "about:blank"
        await pool.initialize(mock_page)

        await pool.close_all()
        assert len(pool._all_pages) == 0


# ═══════════════════════════════════════════════════════════════════════
# 3. Auth Bridge Tests
# ═══════════════════════════════════════════════════════════════════════

class TestGetAuthState:
    """Auth state file reading and validation."""

    def test_reads_valid_state(self, tmp_path):
        """get_auth_state reads and returns valid state."""
        from naukri_server import auth_bridge
        state = {"token": "tk", "cookies": "c=v", "exported_at": time.time(), "cdp_port": 9223}
        state_file = tmp_path / "auth_state.json"
        state_file.write_text(json.dumps(state))

        with patch.object(auth_bridge, "_AUTH_STATE_FILE", state_file):
            result = auth_bridge.get_auth_state()
        assert result["token"] == "tk"

    def test_raises_on_missing_file(self, tmp_path):
        """get_auth_state raises FileNotFoundError when file missing."""
        from naukri_server import auth_bridge
        missing = tmp_path / "nonexistent.json"
        with patch.object(auth_bridge, "_AUTH_STATE_FILE", missing):
            with pytest.raises(FileNotFoundError):
                auth_bridge.get_auth_state()

    def test_raises_on_stale_state(self, tmp_path):
        """get_auth_state raises ValueError when state is too old."""
        from naukri_server import auth_bridge
        state = {"token": "tk", "cookies": "c=v", "exported_at": time.time() - 600}
        state_file = tmp_path / "auth_state.json"
        state_file.write_text(json.dumps(state))

        with patch.object(auth_bridge, "_AUTH_STATE_FILE", state_file):
            with pytest.raises(ValueError, match="old"):
                auth_bridge.get_auth_state()


class TestGetAuthHeaders:
    """Auth headers generation."""

    def test_returns_headers_with_token(self):
        """get_auth_headers returns dict with Authorization and cookie."""
        from naukri_server import auth_bridge
        fake_state = {"token": "my-jwt", "cookies": "a=1; b=2", "exported_at": time.time()}
        with patch.object(auth_bridge, "get_auth_state", return_value=fake_state):
            headers = auth_bridge.get_auth_headers()
        assert headers["Authorization"] == "Bearer my-jwt"
        assert headers["cookie"] == "a=1; b=2"


class TestGetCdpEndpoint:
    """CDP endpoint URL resolution."""

    def test_uses_port_from_state(self):
        """get_cdp_endpoint reads port from auth state."""
        from naukri_server import auth_bridge
        fake_state = {"token": "t", "cookies": "", "exported_at": time.time(), "cdp_port": 9999}
        with patch.object(auth_bridge, "get_auth_state", return_value=fake_state):
            endpoint = auth_bridge.get_cdp_endpoint()
        assert "9999" in endpoint

    def test_falls_back_to_config(self):
        """get_cdp_endpoint uses config CDP_PORT when state unavailable."""
        from naukri_server import auth_bridge
        with patch.object(auth_bridge, "get_auth_state", side_effect=FileNotFoundError):
            endpoint = auth_bridge.get_cdp_endpoint()
        assert "localhost" in endpoint


class TestExtractTokenFromCdp:
    """Token extraction from CDP context."""

    @pytest.mark.asyncio
    async def test_extracts_token_and_cookies(self):
        """extract_token_from_cdp returns (token, cookies) tuple."""
        from naukri_server.auth_bridge import extract_token_from_cdp
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[
            {"name": "nauk_at", "value": "cdp-jwt"},
            {"name": "session", "value": "sess123"},
        ])
        token, cookies = await extract_token_from_cdp(ctx)
        assert token == "cdp-jwt"
        assert "nauk_at=cdp-jwt" in cookies
        assert "session=sess123" in cookies

    @pytest.mark.asyncio
    async def test_raises_when_no_token(self):
        """extract_token_from_cdp raises ValueError when nauk_at missing."""
        from naukri_server.auth_bridge import extract_token_from_cdp
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[
            {"name": "other", "value": "val"},
        ])
        with pytest.raises(ValueError, match="nauk_at"):
            await extract_token_from_cdp(ctx)
