"""Tests for browser.py — TokenManager, PagePool, page helpers.

Every test is PURE: no network, no browser, no file I/O.
"""

import asyncio
from contextlib import asynccontextmanager

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
# 4b. TokenManager refresh FAILURE CLASSIFICATION (Fix A)
#     A swallowed refresh failure (`except: pass` / debug-only) made a
#     login-wall / checkpoint indistinguishable from a transient timeout.
#     Now: transient nav errors re-raise (so retry/backoff applies); a
#     completed nav that yields no token raises AuthExpiredError.
# =====================================================================

class TestTokenManagerRefreshClassification:
    @pytest.mark.asyncio
    async def test_nav_timeout_reraises_transient_not_swallowed(self):
        """A Playwright navigation timeout during reload must NOT be swallowed —
        it re-raises so the API layer's retry/backoff can act (session may still
        be valid)."""
        from naukri_server.browser import TokenManager
        from playwright._impl._errors import TimeoutError as PWTimeout
        tm = TokenManager()
        tm._token = None
        ctx = AsyncMock()
        tm.bind(ctx)
        page = AsyncMock()
        page.reload = AsyncMock(side_effect=PWTimeout("Timeout 20000ms exceeded"))
        with pytest.raises(PWTimeout):
            await tm.refresh(page)

    @pytest.mark.asyncio
    async def test_target_closed_reraises_transient(self):
        """A TargetClosedError (tab died) during reload re-raises as transient."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = None
        ctx = AsyncMock()
        tm.bind(ctx)
        page = AsyncMock()
        page.reload = AsyncMock(side_effect=TargetClosedError())
        with pytest.raises(TargetClosedError):
            await tm.refresh(page)

    @pytest.mark.asyncio
    async def test_nav_ok_but_no_token_raises_auth_expired(self):
        """Reload SUCCEEDS but no nauk_at cookie reappears → session is gone
        (login wall / checkpoint), classified as AuthExpiredError, not a silent
        None and not a transient error."""
        from naukri_server.browser import TokenManager, AuthExpiredError
        tm = TokenManager()
        tm._token = None
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "other", "value": "v"}])  # no nauk_at
        tm.bind(ctx)
        page = AsyncMock()  # reload() succeeds (AsyncMock no-op)
        with pytest.raises(AuthExpiredError, match="re-authenticate"):
            await tm.refresh(page)

    @pytest.mark.asyncio
    async def test_auth_expired_distinct_from_transient_type(self):
        """AuthExpiredError is NOT a Playwright/transient error type — callers
        can branch on it to demand re-auth instead of retrying."""
        from naukri_server.browser import AuthExpiredError
        from playwright._impl._errors import Error as PWError
        assert not issubclass(AuthExpiredError, PWError)
        assert not issubclass(AuthExpiredError, TargetClosedError)

    @pytest.mark.asyncio
    async def test_ensure_token_nav_failure_not_silently_swallowed(self):
        """ensure_token's renewal-navigation failure is logged (warning), not
        debug-swallowed, and still surfaces as the 'Not logged in' signal so
        callers know re-auth is needed."""
        from naukri_server.browser import TokenManager
        from playwright._impl._errors import TimeoutError as PWTimeout
        tm = TokenManager()
        tm._token = None
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[])  # extract finds nothing
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=PWTimeout("nav timeout"))
        ctx.pages = [page]
        tm.bind(ctx)
        with patch("naukri_server.browser.logger") as mock_log:
            with pytest.raises(ValueError, match="Not logged in"):
                await tm.ensure_token()
        # The failure was logged at warning (classified), not silently dropped.
        assert mock_log.warning.called


# =====================================================================
# 4c. TokenManager.refresh_via_pool — UNIFIED LOCK + NO DEADLOCK (Fix B)
#     Both api.py's 401/bot-check refresh AND TokenManager.ensure_token now
#     serialize on the SAME lock (token_manager._refresh_lock), and the pool
#     page is checked out INSIDE that lock so only one is ever outstanding —
#     no refresh storm, no 2nd-page-while-locked deadlock.
# =====================================================================

class _FakePagePool:
    """Minimal page pool with a capacity-N semaphore that records the maximum
    number of pages checked out simultaneously. If refresh_via_pool ever tried
    to hold one page and acquire a SECOND while serialized poorly, max_in_use
    would exceed 1 (or, at capacity 1, deadlock)."""

    def __init__(self, capacity=1):
        self._sem = asyncio.Semaphore(capacity)
        self.in_use = 0
        self.max_in_use = 0
        self.total_acquires = 0

    @asynccontextmanager
    async def acquire(self):
        await self._sem.acquire()
        self.in_use += 1
        self.total_acquires += 1
        self.max_in_use = max(self.max_in_use, self.in_use)
        try:
            yield AsyncMock()
        finally:
            self.in_use -= 1
            self._sem.release()


class TestRefreshViaPoolUnifiedLock:
    @pytest.mark.asyncio
    async def test_refresh_via_pool_uses_the_single_token_manager_lock(self):
        """refresh_via_pool serializes on tm._refresh_lock — the SAME lock
        ensure_token uses (Fix B: one lock for both paths). Proven by holding
        the lock externally and asserting refresh_via_pool blocks until release."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = None
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "nauk_at", "value": "fresh"}])
        tm.bind(ctx)
        pool = _FakePagePool(capacity=1)

        await tm._refresh_lock.acquire()  # simulate the OTHER path holding it
        with patch.object(tm, "_export_auth_state"):
            task = asyncio.create_task(tm.refresh_via_pool(pool))
            await asyncio.sleep(0.05)
            # Blocked on the lock → no page checked out yet.
            assert pool.total_acquires == 0
            assert not task.done()
            tm._refresh_lock.release()
            result = await asyncio.wait_for(task, timeout=1.0)
        assert result == "fresh"
        assert pool.total_acquires == 1

    @pytest.mark.asyncio
    async def test_concurrent_refreshers_never_overlap_in_critical_section(self):
        """N concurrent refresh_via_pool calls serialize: at most ONE page is
        ever checked out at a time (max_in_use == 1), even on a capacity-1 pool —
        no refresh storm, no deadlock. After the first re-mints the token, the
        rest short-circuit on the double-check (no extra browser round-trips)."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = None

        # First extract yields a token; the double-check then makes later callers
        # skip the reload entirely.
        cookies_calls = {"n": 0}

        async def _cookies(_base):
            cookies_calls["n"] += 1
            return [{"name": "nauk_at", "value": "fresh"}]

        ctx = AsyncMock()
        ctx.cookies = AsyncMock(side_effect=_cookies)
        tm.bind(ctx)
        pool = _FakePagePool(capacity=1)

        with patch.object(tm, "_export_auth_state"):
            results = await asyncio.gather(*[tm.refresh_via_pool(pool) for _ in range(5)])

        assert all(r == "fresh" for r in results)
        # Lock serialized everyone → never more than one page out at once.
        assert pool.max_in_use == 1
        # Only the FIRST caller actually reloaded; the other 4 hit the
        # post-lock double-check and skipped the browser round-trip (no storm).
        assert pool.total_acquires == 1

    @pytest.mark.asyncio
    async def test_stale_token_dedup_skips_reload_when_already_refreshed(self):
        """If another caller already re-minted a DIFFERENT token while we waited
        for the lock, refresh_via_pool(stale_token=ours) reuses it WITHOUT a
        browser round-trip — this is what collapses a concurrent storm to a
        single real refresh."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "fresh-by-other"  # someone already refreshed
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "nauk_at", "value": "should-not-be-used"}])
        tm.bind(ctx)
        pool = _FakePagePool(capacity=1)
        result = await tm.refresh_via_pool(pool, stale_token="my-old-stale")
        assert result == "fresh-by-other"
        assert pool.total_acquires == 0  # no reload happened
        ctx.cookies.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_token_equal_forces_real_refresh(self):
        """If the cached token still equals our stale token (nobody refreshed),
        refresh_via_pool performs the real reload."""
        from naukri_server.browser import TokenManager
        tm = TokenManager()
        tm._token = "stale"
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "nauk_at", "value": "freshly-minted"}])
        tm.bind(ctx)
        pool = _FakePagePool(capacity=1)
        with patch.object(tm, "_export_auth_state"):
            result = await tm.refresh_via_pool(pool, stale_token="stale")
        assert result == "freshly-minted"
        assert pool.total_acquires == 1  # real reload happened

    @pytest.mark.asyncio
    async def test_refresh_via_pool_propagates_auth_expired(self):
        """If the underlying refresh classifies an auth expiry, refresh_via_pool
        propagates AuthExpiredError (so the API layer can demand re-auth)."""
        from naukri_server.browser import TokenManager, AuthExpiredError
        tm = TokenManager()
        tm._token = None
        ctx = AsyncMock()
        ctx.cookies = AsyncMock(return_value=[{"name": "other", "value": "v"}])  # no nauk_at
        tm.bind(ctx)
        pool = _FakePagePool(capacity=1)
        with pytest.raises(AuthExpiredError):
            await tm.refresh_via_pool(pool)
        # The page was still acquired-and-released cleanly (no leak/deadlock).
        assert pool.in_use == 0
        assert pool.total_acquires == 1

    @pytest.mark.asyncio
    async def test_refresh_via_pool_propagates_transient(self):
        """A transient nav failure inside refresh_via_pool re-raises (not
        swallowed) and the pool page is released."""
        from naukri_server.browser import TokenManager
        from playwright._impl._errors import TimeoutError as PWTimeout
        tm = TokenManager()
        tm._token = None
        ctx = AsyncMock()
        tm.bind(ctx)

        class _ReloadFailPool(_FakePagePool):
            @asynccontextmanager
            async def acquire(self):
                await self._sem.acquire()
                self.in_use += 1
                self.total_acquires += 1
                self.max_in_use = max(self.max_in_use, self.in_use)
                page = AsyncMock()
                page.reload = AsyncMock(side_effect=PWTimeout("nav timeout"))
                try:
                    yield page
                finally:
                    self.in_use -= 1
                    self._sem.release()

        pool = _ReloadFailPool(capacity=1)
        with pytest.raises(PWTimeout):
            await tm.refresh_via_pool(pool)
        assert pool.in_use == 0  # released despite the error


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
