"""Anti-detection hardening tests — OFFLINE, fully mocked.

Two robustness fixes are covered here:

A. Akamai bot-block classification (406/403) in ``naukri_server.api``:
   406/403 must be treated as a BOT_CHECK signal — refresh the (stale) token +
   cool down, retry ONCE, and on persistence surface a clear re-auth error and
   trip the circuit breaker — NOT retry-hammered like a transient 5xx.

B. Real rate-limit + jitter on the apply path
   (``naukri_server.resilience`` + ``naukri_server.tools.apply``):
   the RateLimiter token bucket is actually wired, and the batch cadence is
   jittered. Both verified with a FAKE CLOCK — no real waiting, no network.

No live browser, network, or filesystem auth state is touched.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ===========================================================================
# Shared mock helpers (mirror tests/test_infra_deep.py conventions)
# ===========================================================================

def _make_mock_response(status, json_data=None, text="", headers=None):
    resp = AsyncMock()
    resp.status = status
    default_headers = {"content-type": "application/json"}
    if headers:
        default_headers.update(headers)
    resp.headers = default_headers
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    return resp


def _make_session_with_responses(responses):
    call_idx = {"i": 0}

    def make_ctx(*args, **kwargs):
        idx = call_idx["i"]
        call_idx["i"] += 1
        resp = responses[idx]
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session = AsyncMock()
    session.request = MagicMock(side_effect=make_ctx)
    return session


def _make_token_mgr(tokens):
    """TokenManager mock whose ensure_token() yields successive values, then
    sticks on the last one (so re-entrant attempts don't exhaust the iterator).

    Passing the same value repeatedly models a still-stale token; a differing
    2nd value models another caller having refreshed it concurrently. The api
    layer now routes every refresh through ``refresh_via_pool`` (Fix B — one
    shared lock + page checkout inside the lock), so that is the method tests
    assert against; ``refresh`` (the page-taking lower-level call) and
    ``invalidate`` remain on the mock for completeness.
    """
    seq = list(tokens)

    async def _ensure():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    mgr = AsyncMock()
    mgr.ensure_token = AsyncMock(side_effect=_ensure)
    mgr.get_cookies = MagicMock(return_value="cookie=val")
    mgr.invalidate = MagicMock()
    mgr.refresh = AsyncMock()
    mgr.refresh_via_pool = AsyncMock()
    return mgr


def _patch_api(session, token_mgr):
    """Context managers patching api module deps + neutralizing real sleeps."""
    page_ctx = AsyncMock()
    page_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    page_ctx.__aexit__ = AsyncMock(return_value=False)

    browser_patch = patch("naukri_server.api.browser")
    sleep_patch = patch("naukri_server.api.asyncio.sleep", new_callable=AsyncMock)
    session_patch = patch("naukri_server.api.get_session", AsyncMock(return_value=session))
    return browser_patch, sleep_patch, session_patch, page_ctx, token_mgr


@pytest.fixture(autouse=True)
def _reset_api_circuit():
    """Reset the shared API circuit breaker so bot-check failures don't leak."""
    import naukri_server.api as api_module
    api_module._api_circuit.record_success()
    yield
    api_module._api_circuit.record_success()


# ===========================================================================
# Fix A — 406/403 Akamai bot-block classification
# ===========================================================================

class TestBotCheckClassification:
    """406/403 → refresh token + cooldown + single retry, then clear error."""

    @pytest.mark.asyncio
    async def test_406_refreshes_token_cools_down_then_succeeds(self):
        """A 406 followed by a 200 (after re-auth) returns the 200 body and
        performs exactly one cooldown sleep — it does NOT retry-hammer."""
        from naukri_server.api import _api_request, BOT_CHECK_COOLDOWN_SECONDS

        resp_406 = _make_mock_response(406, text="Akamai blocked")
        resp_200 = _make_mock_response(200, json_data={"ok": True})
        session = _make_session_with_responses([resp_406, resp_200])
        token_mgr = _make_token_mgr(["stale", "fresh"])

        browser_patch, sleep_patch, session_patch, page_ctx, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch as mock_sleep:
            mock_browser.token_manager = token_mgr
            mock_browser.page_pool.acquire = MagicMock(return_value=page_ctx)
            result = await _api_request("GET", "/test-path")

        assert result == {"ok": True}
        # Refresh went through the UNIFIED, deadlock-free path (Fix B): one
        # shared lock + page checkout owned inside refresh_via_pool.
        token_mgr.refresh_via_pool.assert_awaited_once()
        # api.py no longer holds + acquires a 2nd page itself; it does not call
        # page_pool.acquire directly (that now happens inside refresh_via_pool).
        mock_browser.page_pool.acquire.assert_not_called()
        # Exactly one cooldown sleep of the configured duration.
        mock_sleep.assert_awaited_once_with(BOT_CHECK_COOLDOWN_SECONDS)

    @pytest.mark.asyncio
    async def test_403_persisting_raises_bot_check_error(self):
        """403 that persists after re-auth raises NaukriBotCheckError (code
        BOT_CHECK) with a re-auth message — not an infinite retry loop."""
        from naukri_server.api import _api_request, NaukriBotCheckError, NaukriAPIError

        resp_403_a = _make_mock_response(403, text="Forbidden")
        resp_403_b = _make_mock_response(403, text="Forbidden")
        session = _make_session_with_responses([resp_403_a, resp_403_b])
        token_mgr = _make_token_mgr(["stale", "stale"])

        browser_patch, sleep_patch, session_patch, page_ctx, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch:
            mock_browser.token_manager = token_mgr
            mock_browser.page_pool.acquire = MagicMock(return_value=page_ctx)
            with pytest.raises(NaukriBotCheckError) as exc_info:
                await _api_request("GET", "/test-path")

        assert exc_info.value.status == 403
        assert exc_info.value.code == "BOT_CHECK"
        assert "re-authenticate" in str(exc_info.value).lower()
        # Subclass relationship preserved so existing handlers still catch it.
        assert isinstance(exc_info.value, NaukriAPIError)

    @pytest.mark.asyncio
    async def test_bot_check_does_not_hammer_like_5xx(self):
        """A persisting 406 issues exactly TWO HTTP calls (original + one retry),
        proving it is NOT looped like a retriable 5xx (which would do >2)."""
        from naukri_server.api import _api_request, NaukriBotCheckError

        responses = [_make_mock_response(406, text="blocked") for _ in range(5)]
        session = _make_session_with_responses(responses)
        token_mgr = _make_token_mgr(["stale", "stale"])

        browser_patch, sleep_patch, session_patch, page_ctx, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch:
            mock_browser.token_manager = token_mgr
            mock_browser.page_pool.acquire = MagicMock(return_value=page_ctx)
            with pytest.raises(NaukriBotCheckError):
                await _api_request("GET", "/test-path")

        assert session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_bot_check_trips_circuit_breaker(self):
        """A persisting bot-check records a circuit-breaker failure."""
        import naukri_server.api as api_module
        from naukri_server.api import _api_request, NaukriBotCheckError

        responses = [_make_mock_response(406, text="blocked") for _ in range(2)]
        session = _make_session_with_responses(responses)
        token_mgr = _make_token_mgr(["stale", "stale"])

        before = api_module._api_circuit._failures
        browser_patch, sleep_patch, session_patch, page_ctx, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch:
            mock_browser.token_manager = token_mgr
            mock_browser.page_pool.acquire = MagicMock(return_value=page_ctx)
            with pytest.raises(NaukriBotCheckError):
                await _api_request("GET", "/test-path")

        assert api_module._api_circuit._failures > before

    @pytest.mark.asyncio
    async def test_api_tool_decorator_surfaces_bot_check_code(self):
        """@api_tool maps NaukriBotCheckError to error_code BOT_CHECK so callers
        can detect a bot-flag specifically (not a generic API_ERROR)."""
        from naukri_server.api import api_tool, NaukriBotCheckError

        @api_tool("Test")
        async def failing():
            raise NaukriBotCheckError(406, "Bot-check, call naukri_login")

        result = await failing()
        assert result["status"] == "error"
        assert result["error_code"] == "BOT_CHECK"
        assert result["http_status"] == 406

    @pytest.mark.asyncio
    async def test_406_refresh_goes_through_single_unified_lock_path(self):
        """The bot-check refresh is delegated to TokenManager.refresh_via_pool —
        the SINGLE serialization point (Fix B) — rather than api.py taking its
        own lock and separately checking out a pool page. Cooldown + one retry
        still happen, and the 200 is returned.

        (The "another caller already refreshed → skip the browser round-trip"
        optimization now lives inside refresh_via_pool's double-check; it is
        unit-tested directly in tests/test_browser_deep.py.)
        """
        from naukri_server.api import _api_request

        resp_406 = _make_mock_response(406, text="blocked")
        resp_200 = _make_mock_response(200, json_data={"ok": 1})
        session = _make_session_with_responses([resp_406, resp_200])
        token_mgr = _make_token_mgr(["stale", "fresh-by-other"])

        browser_patch, sleep_patch, session_patch, page_ctx, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch as mock_sleep:
            mock_browser.token_manager = token_mgr
            mock_browser.page_pool.acquire = MagicMock(return_value=page_ctx)
            result = await _api_request("GET", "/test-path")

        assert result == {"ok": 1}
        # Exactly one refresh attempt, via the unified path; api.py itself never
        # checks out a pool page (no 2nd-page-while-locked deadlock surface).
        token_mgr.refresh_via_pool.assert_awaited_once()
        mock_browser.page_pool.acquire.assert_not_called()
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_406_auth_expiry_during_refresh_raises_bot_check_error(self):
        """If the refresh during a bot-check discovers the session is truly gone
        (AuthExpiredError — login wall / checkpoint), surface a clear
        NaukriBotCheckError (re-auth) and trip the circuit — no retry loop."""
        import naukri_server.api as api_module
        from naukri_server.api import _api_request, NaukriBotCheckError
        from naukri_server.browser import AuthExpiredError

        resp_406 = _make_mock_response(406, text="blocked")
        session = _make_session_with_responses([resp_406])
        token_mgr = _make_token_mgr(["stale", "stale"])
        token_mgr.refresh_via_pool = AsyncMock(side_effect=AuthExpiredError("login wall"))

        before = api_module._api_circuit._failures
        browser_patch, sleep_patch, session_patch, page_ctx, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch as mock_sleep:
            mock_browser.token_manager = token_mgr
            mock_browser.page_pool.acquire = MagicMock(return_value=page_ctx)
            with pytest.raises(NaukriBotCheckError) as exc_info:
                await _api_request("GET", "/test-path")

        assert exc_info.value.code == "BOT_CHECK"
        assert "expired" in str(exc_info.value).lower()
        # Auth expiry is terminal here — no cooldown-then-retry, and the circuit trips.
        mock_sleep.assert_not_awaited()
        assert api_module._api_circuit._failures > before

    @pytest.mark.asyncio
    async def test_retriable_5xx_still_hammers_and_recovers(self):
        """Regression guard: a real transient 503 is STILL retried (loops) —
        the bot-check change must not have broken normal 5xx backoff."""
        from naukri_server.api import _api_request

        resp_503 = _make_mock_response(503, text="Service Unavailable")
        resp_200 = _make_mock_response(200, json_data={"recovered": True})
        session = _make_session_with_responses([resp_503, resp_200])
        token_mgr = _make_token_mgr(["tok", "tok", "tok"])

        browser_patch, sleep_patch, session_patch, _, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch:
            mock_browser.token_manager = token_mgr
            result = await _api_request("GET", "/test-path")

        assert result == {"recovered": True}
        assert session.request.call_count == 2


# ===========================================================================
# Fix B (api 401 path) — unified single-lock refresh, no 2nd-page deadlock
# ===========================================================================

class Test401RefreshUnifiedLock:
    """The 401 refresh path delegates to TokenManager.refresh_via_pool (the one
    shared lock + page-checkout-inside-the-lock), instead of api.py taking its
    own module lock and SEPARATELY checking out a pool page. This removes both
    the refresh storm (two locks) and the 2nd-page-while-locked deadlock."""

    @pytest.mark.asyncio
    async def test_401_then_200_via_unified_refresh(self):
        """401 → refresh_via_pool → retry → 200. api.py never directly acquires
        a pool page (deadlock surface eliminated)."""
        from naukri_server.api import _api_request

        resp_401 = _make_mock_response(401, text="Unauthorized")
        resp_200 = _make_mock_response(200, json_data={"ok": True})
        session = _make_session_with_responses([resp_401, resp_200])
        token_mgr = _make_token_mgr(["stale", "fresh"])

        browser_patch, sleep_patch, session_patch, page_ctx, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch:
            mock_browser.token_manager = token_mgr
            mock_browser.page_pool.acquire = MagicMock(return_value=page_ctx)
            result = await _api_request("GET", "/test-path")

        assert result == {"ok": True}
        token_mgr.refresh_via_pool.assert_awaited_once()
        # The single page checkout is owned by refresh_via_pool, NOT api.py.
        mock_browser.page_pool.acquire.assert_not_called()
        assert session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_401_refresh_failure_raises_auth_error_no_loop(self):
        """If refresh_via_pool fails generically after a 401, surface a 401
        NaukriAPIError telling the user to re-auth — not an infinite retry."""
        from naukri_server.api import _api_request, NaukriAPIError

        resp_401 = _make_mock_response(401, text="Unauthorized")
        session = _make_session_with_responses([resp_401])
        token_mgr = _make_token_mgr(["stale", "stale"])
        token_mgr.refresh_via_pool = AsyncMock(side_effect=RuntimeError("reload blew up"))

        browser_patch, sleep_patch, session_patch, page_ctx, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch:
            mock_browser.token_manager = token_mgr
            mock_browser.page_pool.acquire = MagicMock(return_value=page_ctx)
            with pytest.raises(NaukriAPIError) as exc_info:
                await _api_request("GET", "/test-path")

        assert exc_info.value.status == 401
        assert "re-authenticate" in str(exc_info.value).lower()
        # One original call only — refresh failed, so no retry was issued.
        assert session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_401_auth_expired_surfaces_clear_reauth(self):
        """A 401 whose refresh discovers a dead session (AuthExpiredError) trips
        the circuit and raises a clear 401 re-auth error (no retry)."""
        import naukri_server.api as api_module
        from naukri_server.api import _api_request, NaukriAPIError
        from naukri_server.browser import AuthExpiredError

        resp_401 = _make_mock_response(401, text="Unauthorized")
        session = _make_session_with_responses([resp_401])
        token_mgr = _make_token_mgr(["stale", "stale"])
        token_mgr.refresh_via_pool = AsyncMock(
            side_effect=AuthExpiredError("login wall / checkpoint")
        )

        before = api_module._api_circuit._failures
        browser_patch, sleep_patch, session_patch, page_ctx, _ = _patch_api(session, token_mgr)
        with session_patch, browser_patch as mock_browser, sleep_patch:
            mock_browser.token_manager = token_mgr
            mock_browser.page_pool.acquire = MagicMock(return_value=page_ctx)
            with pytest.raises(NaukriAPIError) as exc_info:
                await _api_request("GET", "/test-path")

        assert exc_info.value.status == 401
        assert "re-auth" in str(exc_info.value).lower()
        assert api_module._api_circuit._failures > before
        assert session.request.call_count == 1


# ===========================================================================
# Fix B.1 — RateLimiter token bucket (fake clock, no real sleep)
# ===========================================================================

class FakeClock:
    """Deterministic monotonic clock; advances only when we sleep."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float):
        self.sleeps.append(seconds)
        self.now += seconds


class TestRateLimiterFakeClock:
    """Token-bucket behavior proven without real time."""

    @pytest.mark.asyncio
    async def test_under_limit_never_sleeps(self):
        from naukri_server.resilience import RateLimiter

        clock = FakeClock()
        rl = RateLimiter(max_calls=3, period_seconds=60.0,
                         time_func=clock.time, sleep_func=clock.sleep)
        for _ in range(3):
            await rl.acquire("apply")
        assert clock.sleeps == []  # 3 calls, cap 3 -> no throttling

    @pytest.mark.asyncio
    async def test_exceeding_limit_sleeps_until_window_frees(self):
        from naukri_server.resilience import RateLimiter

        clock = FakeClock()
        rl = RateLimiter(max_calls=2, period_seconds=60.0,
                         time_func=clock.time, sleep_func=clock.sleep)
        await rl.acquire()  # t=1000
        await rl.acquire()  # t=1000, window now full (2/2)
        await rl.acquire()  # must wait for oldest (t=1000) to age out -> 60s

        assert len(clock.sleeps) == 1
        # Oldest entry was at 1000, period 60 -> wait ~60s.
        assert clock.sleeps[0] == pytest.approx(60.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_window_slides_no_sleep_after_aging(self):
        from naukri_server.resilience import RateLimiter

        clock = FakeClock()
        rl = RateLimiter(max_calls=1, period_seconds=10.0,
                         time_func=clock.time, sleep_func=clock.sleep)
        await rl.acquire()         # t=1000
        clock.now += 11           # advance past the window manually
        await rl.acquire()         # oldest aged out -> no sleep
        assert clock.sleeps == []

    @pytest.mark.asyncio
    async def test_max_calls_zero_disables_limiter(self):
        from naukri_server.resilience import RateLimiter

        clock = FakeClock()
        rl = RateLimiter(max_calls=0, period_seconds=60.0,
                         time_func=clock.time, sleep_func=clock.sleep)
        for _ in range(50):
            await rl.acquire()
        assert clock.sleeps == []

    @pytest.mark.asyncio
    async def test_concurrent_acquire_serialized_by_lock(self):
        """Two concurrent acquires past the cap don't both slip through without
        a throttle — the lock serializes the window update."""
        from naukri_server.resilience import RateLimiter

        clock = FakeClock()
        rl = RateLimiter(max_calls=1, period_seconds=30.0,
                         time_func=clock.time, sleep_func=clock.sleep)
        await rl.acquire()  # fill the single slot
        # Fire two more concurrently; at least one must have throttled.
        await asyncio.gather(rl.acquire(), rl.acquire())
        assert len(clock.sleeps) >= 1


# ===========================================================================
# Fix B.2 — jittered_delay helper (deterministic via injected RNG/sleep)
# ===========================================================================

class TestJitteredDelay:
    """Cadence randomization helper."""

    @pytest.mark.asyncio
    async def test_adds_jitter_to_base(self):
        from naukri_server.resilience import jittered_delay

        slept = []

        async def fake_sleep(s):
            slept.append(s)

        total = await jittered_delay(0.5, 0.4, 1.5,
                                     sleep_func=fake_sleep,
                                     rand_func=lambda a, b: 1.0)
        assert total == pytest.approx(1.5)  # 0.5 base + 1.0 jitter
        assert slept == [pytest.approx(1.5)]

    @pytest.mark.asyncio
    async def test_jitter_varies_within_bounds(self):
        from naukri_server.resilience import jittered_delay

        captured = {}

        def fake_rand(a, b):
            captured["bounds"] = (a, b)
            return (a + b) / 2

        await jittered_delay(0.0, 0.4, 1.5,
                             sleep_func=AsyncMock(), rand_func=fake_rand)
        assert captured["bounds"] == (0.4, 1.5)

    @pytest.mark.asyncio
    async def test_swapped_bounds_are_normalized(self):
        from naukri_server.resilience import jittered_delay

        # min > max should be tolerated (swapped), not crash.
        total = await jittered_delay(0.0, 2.0, 1.0,
                                     sleep_func=AsyncMock(),
                                     rand_func=lambda a, b: a)
        # After swap, low bound is 1.0.
        assert total == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_non_constant_cadence_across_calls(self):
        """Successive delays are NOT identical (the whole point: no constant
        cadence). Drive with a varying RNG and assert the spread."""
        from naukri_server.resilience import jittered_delay

        seq = iter([0.4, 1.5, 0.9])

        delays = []
        for _ in range(3):
            d = await jittered_delay(0.0, 0.4, 1.5,
                                     sleep_func=AsyncMock(),
                                     rand_func=lambda a, b: next(seq))
            delays.append(d)
        assert len(set(delays)) > 1  # not a constant cadence


# ===========================================================================
# Fix B.3 — apply path is actually wired to the limiter + jitter
# ===========================================================================

class TestApplyPathThrottled:
    """The dead-code limiter is now live on the apply path."""

    @pytest.mark.asyncio
    async def test_apply_single_acquires_rate_limiter(self):
        """_apply_single calls the shared limiter's acquire() before applying."""
        import naukri_server.tools.apply as apply_mod

        fake_limiter = MagicMock()
        fake_limiter.acquire = AsyncMock()

        with (
            patch("naukri_server.tools.apply.get_apply_rate_limiter", return_value=fake_limiter),
            patch("naukri_server.tools.apply.api_client") as mock_api,
            patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock),
            patch("naukri_server.tools.apply._cache_lock", asyncio.Lock()),
            patch("naukri_server.tools.apply._load_cache", return_value={}),
            patch("naukri_server.tools.apply.event_bus") as mock_bus,
        ):
            mock_bus.emit = AsyncMock()
            mock_api.post = AsyncMock(return_value={"jobs": [{"status": 200}],
                                                    "quotaDetails": {"dailyApplied": 1}})
            result = await apply_mod._apply_single("12345", title="SDE", company="Acme")

        assert result["status"] == "applied"
        fake_limiter.acquire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_apply_rate_limiter_uses_config(self):
        """The shared limiter is built from config values (env-overridable)."""
        import naukri_server.resilience as resilience
        from naukri_server import config

        resilience._apply_rate_limiter = None
        rl = resilience.get_apply_rate_limiter()
        assert rl._max_calls == config.APPLY_RATE_MAX_CALLS
        assert rl._period == config.APPLY_RATE_PERIOD_SECONDS
        # Singleton: second call returns the same instance.
        assert resilience.get_apply_rate_limiter() is rl

    @pytest.mark.asyncio
    async def test_batch_apply_uses_human_think_time(self):
        """batch_apply paces submissions with a randomized human-like think-time
        (NOT a fixed delay) — proven by spying on human_think_time. DECISION
        stealth>throughput: the cadence is a sampled think-time, not delay_ms."""
        import naukri_server.tools.apply as apply_mod

        think_calls = []

        async def fake_think(median, sigma, lo, hi, **kwargs):
            think_calls.append((median, sigma, lo, hi))
            return 0.0  # don't actually sleep

        # Two appl-able jobs so exactly one inter-application gap occurs.
        search_result = {
            "status": "success",
            "jobs": [
                {"job_id": "J1", "title": "A", "company": "C1", "is_applied": False},
                {"job_id": "J2", "title": "B", "company": "C2", "is_applied": False},
            ],
        }

        async def fake_apply_single(job_id, *a, **k):
            return {"status": "applied", "job_id": job_id}

        with (
            patch("naukri_server.tools.search.naukri_search_jobs",
                  new_callable=AsyncMock, return_value=search_result),
            patch("naukri_server.database.get_applied_job_ids",
                  new_callable=AsyncMock, return_value=set()),
            patch("naukri_server.tools.apply._apply_single",
                  new_callable=AsyncMock, side_effect=fake_apply_single),
            patch("naukri_server.tools.apply.human_think_time", side_effect=fake_think),
            patch("naukri_server.tools.apply.asyncio.sleep", new_callable=AsyncMock) as mock_const_sleep,
        ):
            result = await apply_mod.naukri_batch_apply(keywords="python", limit=5, delay_ms=0)

        assert result["status"] == "success"
        assert result["applied"] == 2
        # Human think-time used exactly once (between the 2 serial submissions),
        # sourced from the config distribution params.
        assert len(think_calls) == 1
        median, sigma, lo, hi = think_calls[0]
        assert median == apply_mod.APPLY_THINK_TIME_MEDIAN_SECONDS
        assert sigma == apply_mod.APPLY_THINK_TIME_SIGMA
        assert (lo, hi) == (apply_mod.APPLY_THINK_TIME_MIN_SECONDS, apply_mod.APPLY_THINK_TIME_MAX_SECONDS)
        # ...and NOT a bare constant sleep for cadence.
        mock_const_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_apply_is_serial_not_concurrent(self):
        """Applies run ONE AT A TIME — overlapping concurrency is an automation
        tell. Verify max in-flight _apply_single is 1."""
        import asyncio as _asyncio
        import naukri_server.tools.apply as apply_mod

        search_result = {
            "status": "success",
            "jobs": [
                {"job_id": f"J{i}", "title": "T", "company": "C", "is_applied": False}
                for i in range(4)
            ],
        }

        in_flight = 0
        max_in_flight = 0

        async def fake_apply_single(job_id, *a, **k):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await _asyncio.sleep(0)  # yield to let any concurrency manifest
            in_flight -= 1
            return {"status": "applied", "job_id": job_id}

        async def no_think(*a, **k):
            return 0.0

        with (
            patch("naukri_server.tools.search.naukri_search_jobs",
                  new_callable=AsyncMock, return_value=search_result),
            patch("naukri_server.database.get_applied_job_ids",
                  new_callable=AsyncMock, return_value=set()),
            patch("naukri_server.tools.apply._apply_single",
                  new_callable=AsyncMock, side_effect=fake_apply_single),
            patch("naukri_server.tools.apply.human_think_time", side_effect=no_think),
        ):
            result = await apply_mod.naukri_batch_apply(keywords="python", limit=5,
                                                        max_concurrent=3)

        assert result["applied"] == 4
        # Even with max_concurrent=3 (deprecated/ignored), applies were serial.
        assert max_in_flight == 1
