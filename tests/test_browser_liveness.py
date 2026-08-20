"""Browser liveness predicate - the check that must be able to FAIL.

Context (2026-08-20 outage): all three browser-liveness guards read
``_ = page.url``. In Playwright 1.58.0 ``Page.url`` returns a CACHED string
(``_impl/_frame.py`` -> ``self._url or ""``); no IPC leaves the process, so the
read cannot raise even when the browser is dead. Seven ``BrowserCrashed``
events produced zero restarts and the pool kept vending dead pages for 37+
minutes.

Every test below either proves the OLD predicate was unfalsifiable, or proves
the NEW one returns the failing verdict. A liveness check that has never been
observed failing certifies nothing.
"""

import asyncio

import pytest
from playwright._impl._errors import TargetClosedError

from naukri_server.browser import (
    BrowserUnavailableError,
    PagePool,
    is_page_alive,
)


# ---------------------------------------------------------------------------
# Fakes - a dead page whose ``.url`` still answers, exactly like the real one
# ---------------------------------------------------------------------------

class FakeDeadPage:
    """A page whose target is gone but whose ``.url`` still returns the last
    cached value - the precise shape that defeated the old predicate."""

    def __init__(self, url="https://www.naukri.com/mnjuser/homepage"):
        self.url = url
        self.evaluate_calls = 0

    def is_closed(self):
        return True

    async def evaluate(self, expr, arg=None):
        self.evaluate_calls += 1
        raise TargetClosedError("Target page, context or browser has been closed")

    async def close(self):
        return None


class FakeLivePage:
    def __init__(self, url="https://www.naukri.com/"):
        self.url = url
        self.evaluate_calls = 0

    def is_closed(self):
        return False

    async def evaluate(self, expr, arg=None):
        self.evaluate_calls += 1
        return 1

    async def close(self):
        return None


class FakeHangingPage(FakeLivePage):
    """Page whose evaluate never returns - a wedged renderer, not a closed one."""

    async def evaluate(self, expr, arg=None):
        self.evaluate_calls += 1
        await asyncio.sleep(30)
        return 1


class FakeContext:
    def __init__(self, dead=False):
        self.dead = dead
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        if self.dead:
            raise TargetClosedError("Target page, context or browser has been closed")
        return FakeLivePage()


# ---------------------------------------------------------------------------
# 1. The regression guard - proves the OLD predicate could not fail
# ---------------------------------------------------------------------------

def test_old_predicate_cannot_fail_on_a_dead_page():
    """REGRESSION GUARD. ``_ = page.url`` on a dead page raises nothing.

    If this ever starts raising, the old check would have worked and this whole
    fix could be reconsidered. It does not raise, which is why the outage was
    silent.
    """
    dead = FakeDeadPage()
    _ = dead.url                      # the old liveness check, verbatim
    assert isinstance(dead.url, str)  # answered happily
    assert dead.evaluate_calls == 0   # it never asked the browser anything


# ---------------------------------------------------------------------------
# 2. The new predicate, shown failing
# ---------------------------------------------------------------------------

async def test_is_page_alive_false_on_dead_page():
    dead = FakeDeadPage()
    assert await is_page_alive(dead) is False


async def test_is_page_alive_does_round_trip_on_live_page():
    live = FakeLivePage()
    assert await is_page_alive(live) is True
    assert live.evaluate_calls == 1, "liveness must round-trip to the browser"


async def test_is_page_alive_false_on_none():
    assert await is_page_alive(None) is False


async def test_is_page_alive_false_when_evaluate_hangs():
    """A wedged page is not a live page. Must time out, not block the pool."""
    hung = FakeHangingPage()
    result = await asyncio.wait_for(is_page_alive(hung, timeout=0.2), timeout=5)
    assert result is False


async def test_is_page_alive_false_when_is_closed_raises():
    class Weird(FakeLivePage):
        def is_closed(self):
            raise RuntimeError("connection gone")

    assert await is_page_alive(Weird()) is False


# ---------------------------------------------------------------------------
# 3. PagePool.acquire must detect and recover
# ---------------------------------------------------------------------------

async def test_pool_recovers_dead_page_when_context_is_alive():
    ctx = FakeContext(dead=False)
    pool = PagePool(ctx, max_pages=2)
    dead = FakeDeadPage()
    await pool.initialize(dead)

    async with pool.acquire() as page:
        assert page is not dead, "pool vended the dead page"
        assert isinstance(page, FakeLivePage)

    assert pool.get_stats()["crashes"] == 1, "crash must be counted"
    assert ctx.new_page_calls == 1


async def test_pool_raises_browser_unavailable_when_context_is_dead():
    """Defect 2: recovery called new_page() on the SAME dead context.

    Recovery is impossible from inside the pool when the context itself is
    gone, so the pool must say so loudly instead of throwing a second-order
    TargetClosedError that a caller reads as a page problem.
    """
    ctx = FakeContext(dead=True)
    pool = PagePool(ctx, max_pages=2)
    await pool.initialize(FakeDeadPage())

    with pytest.raises(BrowserUnavailableError):
        async with pool.acquire():
            pass

    assert pool.circuit_state == "open", "dead context must open the circuit"


async def test_pool_does_not_requeue_a_dead_page():
    """A dead page must never go back into the available queue."""
    ctx = FakeContext(dead=True)
    pool = PagePool(ctx, max_pages=2)
    dead = FakeDeadPage()
    await pool.initialize(dead)

    with pytest.raises(BrowserUnavailableError):
        async with pool.acquire():
            pass

    assert dead not in pool._all_pages
    assert pool._available.empty()


async def test_pool_marks_browser_unavailable_on_dead_context(monkeypatch):
    """browser.available was assigned in only 3 places, none of which fire on
    out-of-band death - so health probes kept reporting the browser present."""
    import naukri_server.browser as browser_mod

    ctx = FakeContext(dead=True)
    pool = PagePool(ctx, max_pages=2)
    await pool.initialize(FakeDeadPage())

    monkeypatch.setattr(browser_mod.browser, "page_pool", pool, raising=False)
    monkeypatch.setattr(browser_mod.browser, "available", True, raising=False)

    with pytest.raises(BrowserUnavailableError):
        async with pool.acquire():
            pass

    assert browser_mod.browser.available is False


async def test_pool_semaphore_is_released_after_dead_context():
    """The failure path must not leak the checkout slot."""
    ctx = FakeContext(dead=True)
    pool = PagePool(ctx, max_pages=1)
    await pool.initialize(FakeDeadPage())

    for _ in range(3):
        with pytest.raises(BrowserUnavailableError):
            async with pool.acquire():
                pass
        pool.reset_circuit()  # re-arm so the next acquire is not fast-failed

    assert pool._semaphore._value == 1, "checkout slot leaked"


async def test_pool_checkout_return_counters_balance():
    """Defect 6 was reported as a lease leak. The semaphore never leaked; only
    the counters diverged, because ``_returns`` was skipped whenever a checkout
    failed before a page was assigned. Counters must balance once in flight is
    zero, or the gap is unreadable as a diagnostic."""
    ctx = FakeContext(dead=False)
    pool = PagePool(ctx, max_pages=2)
    await pool.initialize(FakeLivePage())

    for _ in range(4):
        async with pool.acquire():
            pass

    stats = pool.get_stats()
    assert stats["checkouts"] == stats["returns"] == 4


# ---------------------------------------------------------------------------
# 4. The two OTHER instruments that could not fail
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager  # noqa: E402


def _fake_pool_yielding(page):
    """A pool stand-in that hands out exactly the page it was given.

    Bypasses PagePool on purpose: the watchdog probe and the health probe are
    SEPARATE instruments and each must be able to return the failing verdict on
    its own, not merely inherit the pool check.
    """

    @asynccontextmanager
    async def acquire():
        yield page

    pool = type("FakePool", (), {})()
    pool.acquire = acquire
    pool.circuit_state = "closed"
    return pool


async def test_watchdog_probe_reports_dead_page_as_unhealthy(monkeypatch):
    """browser_watchdog._probe used `_ = page.url` and therefore returned True
    for a corpse. That is why 7 BrowserCrashed events produced 0 restarts."""
    import naukri_server.browser as browser_mod
    from naukri_server.browser_watchdog import BrowserWatchdog

    monkeypatch.setattr(browser_mod.browser, "available", True, raising=False)
    monkeypatch.setattr(
        browser_mod.browser, "page_pool", _fake_pool_yielding(FakeDeadPage()), raising=False
    )

    assert await BrowserWatchdog()._probe() is False


async def test_watchdog_probe_reports_live_page_as_healthy(monkeypatch):
    import naukri_server.browser as browser_mod
    from naukri_server.browser_watchdog import BrowserWatchdog

    monkeypatch.setattr(browser_mod.browser, "available", True, raising=False)
    monkeypatch.setattr(
        browser_mod.browser, "page_pool", _fake_pool_yielding(FakeLivePage()), raising=False
    )

    assert await BrowserWatchdog()._probe() is True


async def test_health_probe_reports_dead_page_as_unhealthy(monkeypatch):
    """health/probes/browser.py had the same unfalsifiable check."""
    import naukri_server.browser as browser_mod
    from naukri_server.health.probes.browser import browser_liveness

    monkeypatch.setattr(browser_mod.browser, "available", True, raising=False)
    monkeypatch.setattr(
        browser_mod.browser, "page_pool", _fake_pool_yielding(FakeDeadPage()), raising=False
    )

    result = await browser_liveness()
    assert result.status == "unhealthy", result.message


async def test_health_probe_reports_live_page_as_healthy(monkeypatch):
    import naukri_server.browser as browser_mod
    from naukri_server.health.probes.browser import browser_liveness

    monkeypatch.setattr(browser_mod.browser, "available", True, raising=False)
    monkeypatch.setattr(
        browser_mod.browser, "page_pool", _fake_pool_yielding(FakeLivePage()), raising=False
    )

    result = await browser_liveness()
    assert result.status == "healthy", result.message


async def test_watchdog_crash_count_is_a_real_count():
    """BrowserCrashed carried `crash_count=self._restart_count`, which is 0
    until a restart succeeds -- so four crash events all reported 0 and any
    circuit breaker keyed on it could never trip."""
    from unittest.mock import AsyncMock, patch

    from naukri_server.browser_watchdog import BrowserWatchdog

    w = BrowserWatchdog()
    emitted = []

    async def capture(event):
        emitted.append(event)

    with patch("naukri_server.events.event_bus.emit", new=AsyncMock(side_effect=capture)):
        w._consecutive_failures = 1
        await w._handle_failure()
        w._consecutive_failures = 1  # keep below the restart threshold
        await w._handle_failure()

    assert [e.crash_count for e in emitted] == [1, 2]
    assert w.stats["crash_count"] == 2
