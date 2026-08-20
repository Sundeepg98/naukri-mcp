"""Reproduction + regression tests for the 2026-08-20 server-wide wedge.

Reported sequence against the live server:

    naukri_auth_status                          -> instant, logged_in=true
    naukri_health_check(include_browser=False)  -> hung for the client's full 4 min
    naukri_activity_level                       -> ALSO timed out

The third line is the whole story: one stuck handler took every other tool down
with it.

The mechanism is a single process-wide asyncio lock, not a blocked event loop.
Two things rule the loop-block explanation out: the MCP SDK dispatches each
incoming message with `tg.start_soon` (mcp/server/lowlevel/server.py:677), so
handlers run concurrently and one slow handler cannot starve another at the
transport layer; and the two classic sync-block sources are simply absent here
-- the DB layer is aiosqlite, and `requests` appears nowhere in the package.
What remains is shared state:

  * every REST call funnels through ``TokenManager.ensure_token()``
    (naukri_server/api.py:292), which serialises on ``TokenManager._refresh_lock``;
  * ``naukri_health_check``'s search probe reliably draws a 406 bot-check, which
    routes into ``refresh_via_pool()`` -- that sets ``self._token = None`` and
    then does browser work while holding the lock;
  * several awaits executed WHILE the lock is held have no timeout at all
    (``context.new_page()``, ``context.cookies()``, ``PagePool._available.get()``).

So an unresponsive Chrome does not merely fail the health check: it parks the
lock forever, and every later tool blocks on it. These tests pin each link.

All tests are PURE -- no network, no browser, no real Chrome.

NOTE ON HOW THESE ASSERT: a hang must FAIL the test. Every wait is therefore
written as ``_must_settle``, which fails explicitly on TimeoutError instead of
letting a generic ``pytest.raises(Exception)`` swallow it -- a guard that a hang
can satisfy is exactly the un-falsifiable check that caused this outage.
"""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Budget for the tests themselves. Much smaller than the production constants
# (which each test patches down) so a RED run fails fast instead of stalling.
BUDGET = 6.0


def _never_returning():
    """An awaitable that never completes -- stands in for a wedged Chrome.

    A wedged renderer keeps the CDP websocket OPEN, so Playwright neither errors
    nor times out; it just waits. That is exactly this coroutine.
    """
    async def _hang(*args, **kwargs):
        await asyncio.Event().wait()
    return _hang


def _hanging_context():
    """A Playwright BrowserContext double whose every call hangs forever."""
    ctx = MagicMock()
    ctx.pages = []                                   # forces ensure_token -> new_page()
    ctx.new_page = AsyncMock(side_effect=_never_returning())
    ctx.cookies = AsyncMock(side_effect=_never_returning())
    return ctx


async def _must_settle(coro, what: str, budget: float = BUDGET):
    """Await `coro`, failing the test if it hangs.

    Returns ("ok", value) or ("raised", exc). A TimeoutError is never returned:
    it is the bug under test, so it fails the test loudly.
    """
    t0 = time.monotonic()
    try:
        value = await asyncio.wait_for(coro, timeout=budget)
    except asyncio.TimeoutError:
        pytest.fail(
            "%s did not settle within %.1fs -- this is the reported wedge" % (what, budget)
        )
    except Exception as exc:
        return "raised", exc, time.monotonic() - t0
    return "ok", value, time.monotonic() - t0


async def _cancel(task):
    task.cancel()
    try:
        await task
    except BaseException:
        pass


# =====================================================================
# 1. The tool itself must not hang when one probe does
# =====================================================================


class TestHealthCheckProbeBudget:
    """naukri_health_check must degrade to a partial result, never hang."""

    @staticmethod
    def _start_env(hanging: str):
        """Patch the five API checks: `hanging` never returns, the rest are ok."""
        ok = {"status": "ok", "message": "ok", "elapsed_ms": 5}
        names = ["_check_login", "_check_profile_api", "_check_search_api",
                 "_check_recommendations_api", "_check_dashboard_api"]
        patches = []
        for name in names:
            if name == hanging:
                p = patch(f"naukri_server.tools.health.{name}",
                          new_callable=AsyncMock, side_effect=_never_returning())
            else:
                p = patch(f"naukri_server.tools.health.{name}",
                          new_callable=AsyncMock,
                          return_value={**ok, "name": name.replace("_check_", "")})
            patches.append(p)
        patches.append(patch("naukri_server.tools.health.browser"))
        patches.append(patch("naukri_server.tools.health.os.path.isdir", return_value=True))
        patches.append(patch("naukri_server.tools.health.api_metrics"))

        started = [p.start() for p in patches]
        started[5].page_pool = None                  # the patched `browser`
        started[7].get_stats.return_value = {}       # the patched `api_metrics`
        return patches

    async def test_returns_within_budget_when_one_probe_hangs(self):
        """RED before the fix: no per-probe budget, so the tool never returns.

        The reviewer saw exactly this -- a 4-minute client timeout, no reply.
        """
        from naukri_server.tools.health import naukri_health_check

        patches = self._start_env("_check_search_api")
        try:
            with patch("naukri_server.tools.health.HEALTH_CHECK_TIMEOUT", 1.0):
                outcome, result, _ = await _must_settle(
                    naukri_health_check(include_browser=False), "naukri_health_check"
                )
        finally:
            for p in patches:
                p.stop()

        assert outcome == "ok", "health_check raised instead of degrading: %r" % (result,)
        assert result["status"] == "success"
        rows = {c["name"]: c for c in result["checks"]}
        assert rows["search_api"]["status"] == "timeout", (
            "a probe that blew its budget must land as a degraded row, not kill the tool"
        )
        assert result["summary"]["timeout"] == 1
        # The four healthy probes must still be reported -- partial results are
        # the entire point of a degraded aggregation.
        assert result["summary"]["ok"] == 4

    async def test_hanging_probe_does_not_hide_the_others(self):
        """A timed-out probe must not swallow the checks that did complete."""
        from naukri_server.tools.health import naukri_health_check

        patches = self._start_env("_check_login")
        try:
            with patch("naukri_server.tools.health.HEALTH_CHECK_TIMEOUT", 1.0):
                outcome, result, _ = await _must_settle(
                    naukri_health_check(include_browser=False), "naukri_health_check"
                )
        finally:
            for p in patches:
                p.stop()

        assert outcome == "ok"
        names = [c["name"] for c in result["checks"]]
        assert len(names) == 5
        assert "login" in names


# =====================================================================
# 2. Blast radius -- the damning part of the report
# =====================================================================


class TestWedgeBlastRadius:
    """A stuck token refresh must not take unrelated tools down with it."""

    async def test_unrelated_api_call_survives_a_stuck_token_refresh(self):
        """RED before the fix: caller B blocks on _refresh_lock forever.

        This is the reproduction of `naukri_activity_level` timing out behind a
        stuck `naukri_health_check`. Both tools reach the SAME lock through
        api.py:292, so B's fate is decided by A.

        No HTTP is ever attempted: both callers settle inside ensure_token(),
        before the aiohttp session is touched.
        """
        from naukri_server.browser import TokenManager

        tm = TokenManager()
        tm.bind(_hanging_context())

        fake_browser = MagicMock()
        fake_browser.token_manager = tm
        fake_browser.page_pool = None

        with patch("naukri_server.api.browser", fake_browser), \
             patch("naukri_server.browser.TOKEN_REFRESH_TIMEOUT", 1.0), \
             patch("naukri_server.browser.TOKEN_LOCK_WAIT_TIMEOUT", 2.0), \
             patch("naukri_server.browser.BROWSER_OP_TIMEOUT", 1.0):
            from naukri_server.api import api_get

            # A = the health-check leg that gets stuck holding the lock.
            leg_a = asyncio.create_task(api_get("/stuck"))
            await asyncio.sleep(0.1)               # let A take the lock

            # B = the unrelated read-only tool the reviewer ran next.
            _, _, elapsed = await _must_settle(
                api_get("/unrelated"), "an unrelated tool behind a stuck refresh"
            )
            await _cancel(leg_a)

        assert elapsed < BUDGET, (
            "B settled in %.1fs; it must not wait on A's browser work" % elapsed
        )


# =====================================================================
# 3. Every critical section that holds _refresh_lock must be bounded
# =====================================================================


class TestRefreshLockCriticalSections:
    """The acquire sites in browser.py, each with a hanging browser."""

    async def test_ensure_token_gives_up_instead_of_parking_the_lock(self):
        """browser.py ensure_token(): `context.new_page()` has no timeout.

        RED before the fix: that call never returns, so the lock is never
        released.
        """
        from naukri_server.browser import TokenManager

        tm = TokenManager()
        tm.bind(_hanging_context())

        with patch("naukri_server.browser.TOKEN_REFRESH_TIMEOUT", 1.0), \
             patch("naukri_server.browser.TOKEN_LOCK_WAIT_TIMEOUT", 2.0), \
             patch("naukri_server.browser.BROWSER_OP_TIMEOUT", 1.0):
            outcome, exc, _ = await _must_settle(tm.ensure_token(), "ensure_token")

            assert outcome == "raised", "a wedged browser must not look like a success"
            assert isinstance(exc, ValueError), (   # NotLoggedInError subclasses ValueError
                "callers catch ValueError for auth failures; got %r" % (exc,)
            )
            assert not tm._refresh_lock.locked(), "refresh lock was left held"

    async def test_refresh_via_pool_gives_up_instead_of_parking_the_lock(self):
        """browser.py refresh_via_pool(): `extract()` -> `context.cookies()`.

        The pool page is healthy here and `page.reload()` succeeds; the hang is
        purely in the cookie read that follows -- the await the 406 bot-check
        path reaches on every single health check.
        """
        from naukri_server.browser import PagePool, TokenManager

        ctx = MagicMock()
        ctx.cookies = AsyncMock(side_effect=_never_returning())
        page = MagicMock()
        page.is_closed.return_value = False
        page.evaluate = AsyncMock(return_value=1)
        page.reload = AsyncMock(return_value=None)

        pool = PagePool(ctx, max_pages=1)
        await pool.initialize(page)

        tm = TokenManager()
        tm.bind(ctx)

        with patch("naukri_server.browser.TOKEN_REFRESH_TIMEOUT", 1.0), \
             patch("naukri_server.browser.TOKEN_LOCK_WAIT_TIMEOUT", 2.0), \
             patch("naukri_server.browser.BROWSER_OP_TIMEOUT", 1.0):
            outcome, exc, _ = await _must_settle(
                tm.refresh_via_pool(pool, stale_token="stale"), "refresh_via_pool"
            )

            assert outcome == "raised"
            assert not tm._refresh_lock.locked(), "refresh lock was left held"

    async def test_second_caller_is_not_parked_behind_a_stuck_refresh(self):
        """Two concurrent refreshers: the second must settle, not wait forever."""
        from naukri_server.browser import TokenManager

        tm = TokenManager()
        tm.bind(_hanging_context())

        with patch("naukri_server.browser.TOKEN_REFRESH_TIMEOUT", 1.0), \
             patch("naukri_server.browser.TOKEN_LOCK_WAIT_TIMEOUT", 2.0), \
             patch("naukri_server.browser.BROWSER_OP_TIMEOUT", 1.0):
            first = asyncio.create_task(tm.ensure_token())
            await asyncio.sleep(0.1)

            outcome, exc, elapsed = await _must_settle(
                tm.ensure_token(), "the second concurrent refresher"
            )
            await _cancel(first)

        assert outcome == "raised"
        assert elapsed < BUDGET


# =====================================================================
# 4. PagePool -- the pool's own unbounded waits
# =====================================================================


async def _enter(pool):
    """Enter pool.acquire() as a plain awaitable so wait_for can bound it."""
    cm = pool.acquire()
    page = await cm.__aenter__()
    await cm.__aexit__(None, None, None)
    return page


class TestPagePoolBounds:
    async def test_new_page_hang_does_not_block_the_pool_forever(self):
        """PagePool._create_page(): `context.new_page()` has no timeout."""
        from naukri_server.browser import PagePool

        ctx = MagicMock()
        ctx.new_page = AsyncMock(side_effect=_never_returning())
        pool = PagePool(ctx, max_pages=2)            # empty pool -> must create

        with patch("naukri_server.browser.BROWSER_OP_TIMEOUT", 1.0):
            outcome, exc, _ = await _must_settle(_enter(pool), "pool checkout (new_page)")

        assert outcome == "raised", "a pool that cannot open a tab must say so"

    async def test_queue_wait_is_bounded(self):
        """PagePool.acquire(): `await self._available.get()` has no timeout.

        Reached whenever a page is lost from the queue while still counted in
        `_all_pages` -- the semaphore then admits a caller for whom no page will
        ever arrive. `POOL_CHECKOUT_TIMEOUT` bounds only the semaphore.
        """
        from naukri_server.browser import PagePool

        ctx = MagicMock()
        pool = PagePool(ctx, max_pages=1)
        pool._all_pages.append(MagicMock())          # pool is "full"...
        # ...but nothing is in _available, so acquire() falls into Queue.get().

        with patch("naukri_server.browser.POOL_CHECKOUT_TIMEOUT", 1.0):
            outcome, exc, _ = await _must_settle(_enter(pool), "pool checkout (queue wait)")

        assert outcome == "raised", "an empty pool queue must fail, not wait forever"


# =====================================================================
# 4b. The self-healing path must not be able to hang either
# =====================================================================


class TestWatchdogRestartBounds:
    """A recovery mechanism that can itself wedge supervises nothing."""

    async def test_restart_settles_when_the_browser_will_not_stop(self):
        """browser.stop() awaits context.close()/pw.stop(), neither bounded.

        On a wedged Chrome this parked `_attempt_restart`, and because the
        monitor loop awaits it, the watchdog never probed again -- no further
        restart was ever attempted. That is the shape of the 2026-08-20
        37-minute silent outage.
        """
        from naukri_server.browser_watchdog import BrowserWatchdog

        fake_browser = MagicMock()
        fake_browser.stop = AsyncMock(side_effect=_never_returning())
        fake_browser.start = AsyncMock(return_value=None)
        fake_browser.page_pool = None

        bus = MagicMock()
        bus.emit = AsyncMock()

        wd = BrowserWatchdog()
        with patch("naukri_server.browser.browser", fake_browser),              patch("naukri_server.events.event_bus", bus),              patch("naukri_server.browser_watchdog.BROWSER_RESTART_TIMEOUT", 1.0):
            outcome, _, _ = await _must_settle(wd._attempt_restart(), "watchdog restart")

        assert outcome == "ok", "a stuck stop() must not abort the restart"
        fake_browser.start.assert_awaited(), "start() must still be attempted"

    async def test_probe_scheduler_survives_a_stuck_watchdog(self):
        """framework._on_result triggers the watchdog OUTSIDE the probe timeout.

        An unbounded await there stalls the entire probe-interval bucket, so
        every probe in it stops reporting exactly when the browser dies.
        """
        from naukri_server.health.framework import (
            HealthProbeScheduler, ProbeResult, _RegisteredProbe,
        )

        wd = MagicMock()
        wd._handle_failure = AsyncMock(side_effect=_never_returning())

        bus = MagicMock()
        bus.emit = AsyncMock()

        probe = _RegisteredProbe(
            name="browser.liveness", description="", criticality="critical",
            interval_seconds=30.0, execute=AsyncMock(), subsystem="browser",
            previous_status="healthy",
        )
        sched = HealthProbeScheduler(MagicMock(), watchdog=wd)
        result = ProbeResult(status="unhealthy", message="browser is gone")

        with patch("naukri_server.events.event_bus", bus),              patch("naukri_server.health.framework.WATCHDOG_TRIGGER_TIMEOUT", 1.0):
            outcome, _, _ = await _must_settle(
                sched._on_result(probe, result), "probe scheduler _on_result"
            )

        assert outcome == "ok"


# =====================================================================
# 5. The tool-level watchdog, shown firing
# =====================================================================


class TestToolWatchdog:
    async def test_watchdog_returns_timeout_envelope_instead_of_hanging(self):
        """A tool that never returns must become a TIMEOUT error, not a hang."""
        from naukri_server.utils import tool_watchdog

        @tool_watchdog(timeout=0.5)
        async def _wedged_tool():
            await asyncio.Event().wait()

        outcome, result, _ = await _must_settle(_wedged_tool(), "a watchdogged tool")
        assert outcome == "ok"
        assert result["status"] == "error"
        assert result["error_code"] == "TIMEOUT"
        assert "_wedged_tool" in result["message"]

    async def test_watchdog_is_transparent_to_a_healthy_tool(self):
        """The control: the guard must not alter a tool that returns normally."""
        from naukri_server.utils import tool_watchdog

        @tool_watchdog(timeout=5.0)
        async def _healthy_tool(x):
            return {"status": "success", "x": x}

        assert await _healthy_tool(7) == {"status": "success", "x": 7}

    async def test_watchdog_lets_real_errors_through(self):
        """The second control: it must not convert a real failure into TIMEOUT."""
        from naukri_server.utils import tool_watchdog

        @tool_watchdog(timeout=5.0)
        async def _broken_tool():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await _broken_tool()
