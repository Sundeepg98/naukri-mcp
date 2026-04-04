"""Tests for browser.py — TokenManager, PagePool, page helpers.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from playwright._impl._errors import TargetClosedError


# =====================================================================
# 1. TokenManager.extract()
# =====================================================================

class TestTokenManagerExtract:
    @pytest.mark.asyncio
    async def test_extract_finds_nauk_at(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[
            {"name": "nauk_at", "value": "jwt-123"},
            {"name": "other", "value": "val"},
        ])
        tm.bind(ctx)
        with patch.object(tm, "_export_auth_state"):
            await tm.extract()
        assert tm._token == "jwt-123"

    @pytest.mark.asyncio
    async def test_extract_no_nauk_at(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "other", "value": "v"}])
        tm.bind(ctx)
        await tm.extract()
        assert tm._token is None

    @pytest.mark.asyncio
    async def test_extract_no_context(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        await tm.extract()
        assert tm._token is None

    @pytest.mark.asyncio
    async def test_extract_handles_exception(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(side_effect=RuntimeError("crashed"))
        tm.bind(ctx)
        await tm.extract()
        assert tm._token is None


# =====================================================================
# 2. TokenManager.get_token() / get_cookies()
# =====================================================================

class TestTokenManagerGetToken:
    def test_returns_cached(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "tok"
        assert tm.get_token() == "tok"

    def test_raises_when_empty(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        with pytest.raises(ValueError, match="Not logged in"):
            tm.get_token()

    def test_get_cookies_empty(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        assert tm.get_cookies() == ""

    def test_get_cookies_cached(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._cookies = "a=1"
        assert tm.get_cookies() == "a=1"


# =====================================================================
# 3. TokenManager.invalidate()
# =====================================================================

class TestTokenManagerInvalidate:
    def test_clears_token(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "active"
        tm.invalidate()
        assert tm._token is None


# =====================================================================
# 4. TokenManager.refresh()
# =====================================================================

class TestTokenManagerRefresh:
    @pytest.mark.asyncio
    async def test_reloads_and_extracts(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = None
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "nauk_at", "value": "refreshed"}])
        tm.bind(ctx)
        page = AsyncMock()
        with patch.object(tm, "_export_auth_state"):
            result = await tm.refresh(page)
        assert result == "refreshed"

    @pytest.mark.asyncio
    async def test_skips_if_valid(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "valid"
        page = AsyncMock()
        result = await tm.refresh(page)
        assert result == "valid"
        page.reload.assert_not_called()


# =====================================================================
# 5. TokenManager.ensure_token()
# =====================================================================

class TestTokenManagerEnsureToken:
    @pytest.mark.asyncio
    async def test_returns_cached(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "cached"
        assert await tm.ensure_token() == "cached"

    @pytest.mark.asyncio
    async def test_extracts_on_missing(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "nauk_at", "value": "extracted"}])
        tm.bind(ctx)
        with patch.object(tm, "_export_auth_state"):
            assert await tm.ensure_token() == "extracted"

    @pytest.mark.asyncio
    async def test_raises_when_all_fails(self):
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[])
        ctx.pages = []
        ctx.new_page = AsyncMock(return_value=AsyncMock())
        tm.bind(ctx)
        with pytest.raises(ValueError, match="Not logged in"):
            await tm.ensure_token()


# =====================================================================
# 6. PagePool.acquire() / initialize / stats / close
# =====================================================================

class TestPagePoolAcquire:
    @pytest.mark.asyncio
    async def test_returns_initialized_page(self):
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        pool = PagePool(ctx, max_pages=3)
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        await pool.initialize(mock_page)
        async with pool.acquire() as page:
            assert page is mock_page

    @pytest.mark.asyncio
    async def test_creates_new_if_empty(self):
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        new_page = MagicMock()
        new_page.url = "about:blank"
        ctx.new_page = AsyncMock(return_value=new_page)
        pool = PagePool(ctx, max_pages=3)
        async with pool.acquire() as page:
            assert page is new_page

    @pytest.mark.asyncio
    async def test_recovers_crashed_page(self):
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        recovered = MagicMock()
        recovered.url = "about:blank"
        ctx.new_page = AsyncMock(return_value=recovered)
        pool = PagePool(ctx, max_pages=3)
        crashed = MagicMock()
        type(crashed).url = PropertyMock(side_effect=TargetClosedError())
        await pool.initialize(crashed)
        async with pool.acquire() as page:
            assert page is recovered
        assert pool._crashes == 1

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        pool = PagePool(ctx, max_pages=2)
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        await pool.initialize(mock_page)
        async with pool.acquire():
            pass
        async with pool.acquire():
            pass
        stats = pool.get_stats()
        assert stats["checkouts"] == 2
        assert stats["returns"] == 2

    @pytest.mark.asyncio
    async def test_close_all(self):
        from naukri_server.browser import PagePool
        ctx = AsyncMock()
        pool = PagePool(ctx, max_pages=3)
        mock_page = AsyncMock()
        mock_page.url = "about:blank"
        await pool.initialize(mock_page)
        await pool.close_all()
        assert len(pool._all_pages) == 0


# =====================================================================
# 7. auth_bridge — get_auth_state()
# =====================================================================

class TestGetAuthState:
    def test_reads_valid(self, tmp_path):
        import json, time
        from naukri_server import auth_bridge
        state = {"token": "tk", "cookies": "c=v", "exported_at": time.time(), "cdp_port": 9223}
        f = tmp_path / "auth_state.json"
        f.write_text(json.dumps(state))
        with patch.object(auth_bridge, "_AUTH_STATE_FILE", f):
            result = auth_bridge.get_auth_state()
        assert result["token"] == "tk"

    def test_raises_missing(self, tmp_path):
        from naukri_server import auth_bridge
        with patch.object(auth_bridge, "_AUTH_STATE_FILE", tmp_path / "nope.json"):
            with pytest.raises(FileNotFoundError):
                auth_bridge.get_auth_state()

    def test_raises_stale(self, tmp_path):
        import json, time
        from naukri_server import auth_bridge
        state = {"token": "tk", "cookies": "c=v", "exported_at": time.time() - 600}
        f = tmp_path / "auth_state.json"
        f.write_text(json.dumps(state))
        with patch.object(auth_bridge, "_AUTH_STATE_FILE", f):
            with pytest.raises(ValueError, match="old"):
                auth_bridge.get_auth_state()


# =====================================================================
# Auth Bridge — additional tests (recovered from tier25_infra.py)
# =====================================================================

class TestGetAuthHeaders:
    """Auth headers generation."""

    def test_returns_headers_with_token(self):
        import time
        from naukri_server import auth_bridge
        fake_state = {"token": "my-jwt", "cookies": "a=1; b=2", "exported_at": time.time()}
        with patch.object(auth_bridge, "get_auth_state", return_value=fake_state):
            headers = auth_bridge.get_auth_headers()
        assert headers["Authorization"] == "Bearer my-jwt"
        assert headers["cookie"] == "a=1; b=2"


class TestGetCdpEndpoint:
    """CDP endpoint URL resolution."""

    def test_uses_port_from_state(self):
        import time
        from naukri_server import auth_bridge
        fake_state = {"token": "t", "cookies": "", "exported_at": time.time(), "cdp_port": 9999}
        with patch.object(auth_bridge, "get_auth_state", return_value=fake_state):
            endpoint = auth_bridge.get_cdp_endpoint()
        assert "9999" in endpoint

    def test_falls_back_to_config(self):
        from naukri_server import auth_bridge
        with patch.object(auth_bridge, "get_auth_state", side_effect=FileNotFoundError):
            endpoint = auth_bridge.get_cdp_endpoint()
        assert "localhost" in endpoint


class TestExtractTokenFromCdp:
    """Token extraction from CDP context."""

    @pytest.mark.asyncio
    async def test_extracts_token_and_cookies(self):
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
        from naukri_server.auth_bridge import extract_token_from_cdp
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "other", "value": "val"}])
        with pytest.raises(ValueError, match="nauk_at"):
            await extract_token_from_cdp(ctx)
