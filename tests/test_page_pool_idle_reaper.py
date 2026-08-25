"""Idle-tab reaping in PagePool.

The defect these cover: ``_all_pages`` only ever grew. A tab opened for one
scheduled run sat at about:blank until the process died. Measured live on
2026-08-25 before the fix - 12 chrome processes / 263.5 MB, with CDP reporting
3 page targets of which 2 were about:blank, while the LinkedIn, Instahyre and
Uplers servers had no browser process at all when idle.

Every test here is PURE: no network, no browser, no file I/O. Playwright is
mocked. Idle age is simulated by writing ``_last_used`` directly rather than by
sleeping, so the suite stays fast and deterministic.
"""

import asyncio
import inspect

import pytest

from naukri_server.browser import PagePool, is_page_alive


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePage:
    """A live page that records whether it was closed."""

    def __init__(self, name="page"):
        self.name = name
        self.closed = False
        self.evaluate_calls = 0

    def is_closed(self):
        return self.closed

    async def evaluate(self, expr, arg=None):
        self.evaluate_calls += 1
        return 1

    async def close(self):
        self.closed = True

    def __repr__(self):
        return "<FakePage %s closed=%s>" % (self.name, self.closed)


class FakeContext:
    def __init__(self):
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        return FakePage("created-%d" % self.new_page_calls)


async def _make_pool(n_pages, idle_timeout=300.0, min_pages=1, max_pages=3):
    """Pool seeded with ``n_pages`` idle pages, all sitting in the queue."""
    pool = PagePool(FakeContext(), max_pages=max_pages,
                    idle_timeout=idle_timeout, min_pages=min_pages)
    pages = [FakePage("p%d" % i) for i in range(n_pages)]
    await pool.initialize(pages[0])
    for p in pages[1:]:
        pool._all_pages.append(p)
        pool._touch(p)
        pool._available.put_nowait(p)
    return pool, pages


def _age(pool, page, seconds):
    """Backdate a page's last-use stamp by ``seconds``."""
    pool._last_used[page] -= seconds


def _age_all(pool, seconds):
    for p in list(pool._last_used):
        pool._last_used[p] -= seconds


# ---------------------------------------------------------------------------
# 1. THE HEADLINE: an idle tab is closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_page_is_closed():
    """A pooled tab nobody has touched past the timeout is closed and dropped."""
    pool, pages = await _make_pool(3, idle_timeout=300.0, min_pages=1)
    _age_all(pool, 600)                 # all three well past the timeout

    closed = await pool.reap_idle_pages()

    assert closed == 2, "expected the 2 surplus tabs to be reaped"
    assert sum(p.closed for p in pages) == 2
    assert len(pool._all_pages) == 1
    assert pool._available.qsize() == 1
    # the reaped pages are gone from BOTH structures, so acquire() cannot
    # possibly hand one out
    for p in pages:
        if p.closed:
            assert p not in pool._all_pages
            assert p not in pool._last_used


@pytest.mark.asyncio
async def test_idle_closed_counter_is_reported():
    pool, _pages = await _make_pool(3, idle_timeout=300.0, min_pages=1)
    _age_all(pool, 600)
    await pool.reap_idle_pages()

    stats = pool.get_stats()
    assert stats["idle_closed"] == 2
    assert stats["open_tabs"] == 1
    assert stats["idle_tabs"] == 1


# ---------------------------------------------------------------------------
# 2. THE MIRROR: a page in active use is NOT closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_page_in_active_use_is_not_closed():
    """A tab currently checked out survives, however stale its last stamp is.

    Structural, not timing-based: acquire() holds the page OUT of _available for
    the whole ``async with`` body, and the reaper only ever considers what is in
    _available.
    """
    pool, _pages = await _make_pool(3, idle_timeout=300.0, min_pages=1)
    _age_all(pool, 9999)                # everything looks ancient

    async with pool.acquire() as held:
        assert held is not None
        closed = await pool.reap_idle_pages()
        assert held.closed is False, "a page in active use was closed"
        assert held in pool._all_pages
        # Two tabs were left in the idle queue; the floor keeps one of them, so
        # exactly one is reaped. The floor is counted over the IDLE QUEUE, so a
        # checked-out page does not consume it - the conservative direction, and
        # the reason a busy pool still leaves a tab for the liveness probes.
        assert closed == 1
        assert pool._available.qsize() == 1

    # after the block the held page is back and usable
    assert held.closed is False
    assert pool._available.qsize() == 2
    assert len(pool._all_pages) == 2


@pytest.mark.asyncio
async def test_recently_used_page_is_not_closed():
    """Freshness alone protects a tab - no checkout required."""
    pool, pages = await _make_pool(3, idle_timeout=300.0, min_pages=1)
    _age(pool, pages[0], 600)           # stale
    _age(pool, pages[1], 600)           # stale
    # pages[2] left fresh

    closed = await pool.reap_idle_pages()

    assert closed == 2
    assert pages[2].closed is False, "a recently used tab was closed"


@pytest.mark.asyncio
async def test_returning_a_page_refreshes_its_idle_clock():
    """Using a tab makes it un-reapable again."""
    pool, pages = await _make_pool(2, idle_timeout=300.0, min_pages=0)
    _age_all(pool, 600)

    async with pool.acquire():
        pass                            # checked out and returned -> touched

    closed = await pool.reap_idle_pages()
    assert closed == 1, "only the page that was never used should be reaped"
    assert sum(p.closed for p in pages) == 1


# ---------------------------------------------------------------------------
# 3. THE CLOSER MUST NOT FIRE UNDER AN IN-FLIGHT OPERATION
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reaper_running_concurrently_never_closes_the_held_page():
    """Reaper sweeps repeatedly WHILE an operation holds a page.

    The operation awaits between sweeps, so the event loop really does
    interleave them. The held page must survive every sweep, and must still
    answer a round-trip when the operation is done with it.
    """
    pool, _pages = await _make_pool(3, idle_timeout=0.0, min_pages=0)

    state = {"sweeps": 0, "stop": False}

    async def reaper():
        while not state["stop"]:
            await pool.reap_idle_pages()
            state["sweeps"] += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(reaper())
    async with pool.acquire() as held:
        for _ in range(20):
            await asyncio.sleep(0)      # let the reaper run, repeatedly
            assert held.closed is False, "reaper closed a page mid-operation"
        assert await held.evaluate("1") == 1     # still a working page
    state["stop"] = True
    await task

    assert state["sweeps"] > 1, "the reaper did not actually get to run"
    assert held.closed is False


@pytest.mark.asyncio
async def test_reaper_never_starves_a_concurrent_acquirer():
    """With every tab reap-eligible, callers still get a working page.

    Guards the interleaving the atomic bookkeeping exists to prevent: a caller
    must never find _available drained while _all_pages is still full, which
    would park it on the queue until POOL_CHECKOUT_TIMEOUT.
    """
    pool, _pages = await _make_pool(3, idle_timeout=0.0, min_pages=1)

    state = {"stop": False}

    async def reaper():
        while not state["stop"]:
            await pool.reap_idle_pages()
            await asyncio.sleep(0)

    task = asyncio.create_task(reaper())
    try:
        for _ in range(25):
            async with pool.acquire() as page:
                assert page is not None
                assert page.closed is False
                assert await page.evaluate("1") == 1
            await asyncio.sleep(0)
    finally:
        state["stop"] = True
        await task


# ---------------------------------------------------------------------------
# 4. THE WARM-PAGE FLOOR - what keeps the watchdog out of this
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_floor_keeps_one_warm_page_forever():
    """min_pages tabs survive any amount of idleness.

    NOT the anti-watchdog mechanism, despite the obvious story. Measured, the
    liveness probe shape survives an aggressive reaper at floor 0 too (0/400
    failures either way) because acquire() creates a tab on demand. The floor
    buys no cold-start for interactive callers and no create/close churn - see
    test_watchdog_probe_never_fails_under_an_aggressive_reaper below, which
    pins the actual claim.
    """
    pool, pages = await _make_pool(3, idle_timeout=1.0, min_pages=1)
    for _ in range(5):
        _age_all(pool, 10_000)
        await pool.reap_idle_pages()

    assert len(pool._all_pages) == 1
    assert pool._available.qsize() == 1
    assert sum(not p.closed for p in pages) == 1


@pytest.mark.asyncio
async def test_liveness_probe_shape_still_succeeds_after_heavy_reaping():
    """The watchdog's exact probe shape, run after the pool has been trimmed."""
    pool, _pages = await _make_pool(3, idle_timeout=1.0, min_pages=1)
    _age_all(pool, 10_000)
    await pool.reap_idle_pages()

    # verbatim shape of browser_watchdog._probe / health.probes.browser
    async with pool.acquire() as page:
        assert await is_page_alive(page) is True


@pytest.mark.asyncio
async def test_reaped_tab_is_recreated_on_demand():
    """Trimming is not one-way: the pool grows back when work arrives."""
    pool, _pages = await _make_pool(3, idle_timeout=1.0, min_pages=1)
    _age_all(pool, 10_000)
    await pool.reap_idle_pages()
    assert len(pool._all_pages) == 1

    release = asyncio.Event()

    async def hold():
        async with pool.acquire() as page:
            assert page is not None
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(3)]
    for _ in range(50):                 # let all three check out
        if len(pool._all_pages) == 3:
            break
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*holders)

    assert len(pool._all_pages) == 3, "pool did not grow back to max_pages"
    assert pool._context.new_page_calls >= 2


# ---------------------------------------------------------------------------
# 5. Guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reaper_is_a_noop_when_circuit_is_open():
    """The browser is known-dead; the watchdog owns recovery, not the reaper."""
    pool, pages = await _make_pool(3, idle_timeout=0.0, min_pages=0)
    pool._circuit_state = "open"
    _age_all(pool, 9999)

    assert await pool.reap_idle_pages() == 0
    assert not any(p.closed for p in pages)


@pytest.mark.asyncio
async def test_reaper_is_a_noop_on_an_empty_pool():
    pool = PagePool(FakeContext(), max_pages=3, idle_timeout=0.0, min_pages=0)
    assert await pool.reap_idle_pages() == 0


@pytest.mark.asyncio
async def test_a_page_that_refuses_to_close_is_still_dropped():
    """A tab whose close() raises is already gone; it must not be re-served."""
    pool, pages = await _make_pool(2, idle_timeout=0.0, min_pages=0)

    async def boom():
        raise RuntimeError("target already closed")

    pages[0].close = boom
    _age_all(pool, 9999)

    closed = await pool.reap_idle_pages()
    assert closed == 2
    assert pool._all_pages == []
    assert pool._last_used == {}


@pytest.mark.asyncio
async def test_reaper_keeps_the_freshest_tabs():
    """The floor keeps the most recently used tab, not an arbitrary one.

    Matters beyond tidiness: TokenManager.refresh_via_pool reloads whichever
    pooled page it is handed, so keeping the freshest favours a tab on a real
    Naukri page over one parked at about:blank.
    """
    pool, pages = await _make_pool(3, idle_timeout=300.0, min_pages=1)
    _age(pool, pages[0], 5000)
    _age(pool, pages[1], 4000)
    _age(pool, pages[2], 3000)          # freshest

    await pool.reap_idle_pages()

    assert pages[2].closed is False
    assert pages[0].closed is True
    assert pages[1].closed is True


def test_bookkeeping_phase_contains_no_await():
    """REGRESSION GUARD on the property the whole safety argument rests on.

    Phase 1 of reap_idle_pages (drain, choose, put back, drop) must stay
    synchronous. If someone adds an await inside it, the event loop can
    interleave a caller into a half-updated pool and the structural guarantee
    the tests above rely on quietly becomes a race. Checked by source
    inspection because there is no runtime way to observe "did not yield".
    """
    src = inspect.getsource(PagePool.reap_idle_pages)
    marker = "# ---- end of the atomic section ----"
    assert marker in src, "the atomic section marker was removed"
    phase1 = src.split("now = time.monotonic()", 1)[1].split(marker, 1)[0]

    # Strip comments before looking - the section's own comment says the word
    # "await", which is prose, not a suspension point.
    code = "\n".join(line.split("#", 1)[0] for line in phase1.splitlines())
    assert "await" not in code, (
        "an await appeared inside the atomic bookkeeping section of "
        "reap_idle_pages - the no-interleave guarantee is broken"
    )
    # and prove the guard can still see a real one
    assert "await" in (code + "\n await x")


# ---------------------------------------------------------------------------
# 6. Reaper TASK lifecycle on NaukriBrowser
#
# The lifecycle matters because two other subsystems stop and start this
# browser: browser_watchdog._attempt_restart, and reauth()'s browser_restart
# stage (services/session_service.py). Both go through stop() then start(). An
# orphaned reaper task still pointing at the OLD pool would be the race the
# brief warns about, so stop() must cancel it and start() must install a fresh
# one.
# ---------------------------------------------------------------------------

class _StubPool:
    def __init__(self):
        self.reaps = 0

    def __bool__(self):
        return True

    async def reap_idle_pages(self):
        self.reaps += 1
        return 0


@pytest.mark.asyncio
async def test_stop_reaper_is_safe_when_never_started():
    from naukri_server.browser import NaukriBrowser
    nb = NaukriBrowser()
    assert nb._reaper_task is None
    await nb._stop_reaper()             # must not raise
    assert nb._reaper_task is None


@pytest.mark.asyncio
async def test_start_reaper_then_stop_cancels_the_task():
    from naukri_server.browser import NaukriBrowser
    nb = NaukriBrowser()
    nb._start_reaper()
    task = nb._reaper_task
    assert task is not None and not task.done()

    await nb._stop_reaper()
    assert task.done(), "reaper task was not stopped"
    assert nb._reaper_task is None


@pytest.mark.asyncio
async def test_start_reaper_is_idempotent():
    """A second start() must not leave two reapers on one pool."""
    from naukri_server.browser import NaukriBrowser
    nb = NaukriBrowser()
    nb._start_reaper()
    first = nb._reaper_task
    nb._start_reaper()
    assert nb._reaper_task is first, "a duplicate reaper task was created"
    await nb._stop_reaper()


@pytest.mark.asyncio
async def test_reaper_loop_skips_while_browser_unavailable():
    """While the browser is down the reaper must not touch the pool at all -
    the watchdog owns that window."""
    from naukri_server.browser import NaukriBrowser
    import naukri_server.browser as bmod

    nb = NaukriBrowser()
    pool = _StubPool()
    nb.page_pool = pool
    nb.available = False

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "PAGE_REAPER_INTERVAL", 0.001)
        nb._start_reaper()
        await asyncio.sleep(0.05)
        await nb._stop_reaper()

    assert pool.reaps == 0, "reaper swept while the browser was unavailable"


@pytest.mark.asyncio
async def test_reaper_loop_sweeps_when_browser_is_available():
    from naukri_server.browser import NaukriBrowser
    import naukri_server.browser as bmod

    nb = NaukriBrowser()
    pool = _StubPool()
    nb.page_pool = pool
    nb.available = True

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "PAGE_REAPER_INTERVAL", 0.001)
        nb._start_reaper()
        await asyncio.sleep(0.05)
        await nb._stop_reaper()

    assert pool.reaps > 0, "reaper never swept a live pool"


@pytest.mark.asyncio
async def test_one_failing_sweep_does_not_kill_the_loop():
    """A reaper that dies on its first bad cycle stops trimming forever."""
    from naukri_server.browser import NaukriBrowser
    import naukri_server.browser as bmod

    class ExplodingThenFinePool(_StubPool):
        async def reap_idle_pages(self):
            self.reaps += 1
            if self.reaps == 1:
                raise RuntimeError("one bad sweep")
            return 0

    nb = NaukriBrowser()
    pool = ExplodingThenFinePool()
    nb.page_pool = pool
    nb.available = True

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "PAGE_REAPER_INTERVAL", 0.001)
        nb._start_reaper()
        await asyncio.sleep(0.05)
        task = nb._reaper_task
        assert task is not None and not task.done(), "loop died on a failed sweep"
        await nb._stop_reaper()

    assert pool.reaps > 1, "loop stopped sweeping after the failure"


@pytest.mark.asyncio
async def test_no_page_pool_is_skipped_by_the_reaper():
    """_NO_POOL is deliberately falsy; the reaper must respect that and not
    call acquire/reap on it."""
    from naukri_server.browser import NaukriBrowser, _NO_POOL
    import naukri_server.browser as bmod

    nb = NaukriBrowser()
    assert nb.page_pool is _NO_POOL
    nb.available = True                 # even so: no pool means nothing to do

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "PAGE_REAPER_INTERVAL", 0.001)
        nb._start_reaper()
        await asyncio.sleep(0.05)
        task = nb._reaper_task
        assert task is not None and not task.done()
        await nb._stop_reaper()


@pytest.mark.asyncio
async def test_interval_zero_disables_reaping_entirely():
    """The documented escape hatch: NAUKRI_PAGE_REAPER_INTERVAL=0 means the
    background task is never started and the pool behaves as it did before."""
    from naukri_server.browser import NaukriBrowser
    import naukri_server.browser as bmod

    nb = NaukriBrowser()
    nb.page_pool = _StubPool()
    nb.available = True

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "PAGE_REAPER_INTERVAL", 0)
        nb._start_reaper()
        assert nb._reaper_task is None, "reaper started despite interval 0"
        await asyncio.sleep(0.02)

    assert nb.page_pool.reaps == 0
    await nb._stop_reaper()             # still safe to call


@pytest.mark.asyncio
@pytest.mark.parametrize("min_pages", [1, 0])
async def test_watchdog_probe_never_fails_under_an_aggressive_reaper(min_pages):
    """THE ANTI-RACE CLAIM, pinned.

    The brief's real worry: "an idle-closer that races the watchdog is worse
    than the leak". browser_watchdog._probe and health.probes.browser each
    acquire a page every 30s; two consecutive failures drive an automatic
    browser restart, and a spurious restart storm is exactly the 2026-08-20
    shape nobody wants back.

    Here the reaper sweeps flat out with idle_timeout=0 - far more aggressive
    than the shipped 300s - while the probe shape runs repeatedly. Zero
    failures is the requirement. Parametrized over the floor because measuring
    it is what corrected the original (wrong) explanation: the probes are safe
    at floor 0 as well, since acquire() creates a tab on demand. The floor is
    about cold-start and churn, not about the watchdog.
    """
    pool, _pages = await _make_pool(3, idle_timeout=0.0, min_pages=min_pages)

    state = {"stop": False}

    async def reaper():
        while not state["stop"]:
            await pool.reap_idle_pages()
            await asyncio.sleep(0)

    task = asyncio.create_task(reaper())
    failures = 0
    try:
        for _ in range(200):
            try:
                # verbatim shape of browser_watchdog._probe
                async with asyncio.timeout(10):
                    async with pool.acquire() as page:
                        if not await is_page_alive(page):
                            failures += 1
            except Exception:
                failures += 1
            await asyncio.sleep(0)
    finally:
        state["stop"] = True
        await task

    assert failures == 0, (
        "the reaper made %d/200 watchdog liveness probes fail at min_pages=%d - "
        "that is a spurious-restart storm waiting to happen" % (failures, min_pages)
    )
    assert pool._circuit_state == "closed", "reaper opened the circuit breaker"


@pytest.mark.asyncio
async def test_reaper_never_touches_browser_lifecycle():
    """The reaper's blast radius is tab count and nothing else.

    This is the actual mechanism keeping it clear of browser_watchdog and of
    reauth's browser_restart stage: those own stop/start, this owns tabs.
    """
    import naukri_server.browser as bmod
    from naukri_server.browser import NaukriBrowser

    nb = NaukriBrowser()
    pool, _pages = await _make_pool(3, idle_timeout=0.0, min_pages=1)
    nb.page_pool = pool
    nb.available = True
    nb.context = object()
    nb.pw = object()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "PAGE_REAPER_INTERVAL", 0.001)
        nb._start_reaper()
        await asyncio.sleep(0.05)
        await nb._stop_reaper()

    assert nb.available is True, "reaper flipped browser.available"
    assert nb.context is not None, "reaper dropped the context"
    assert nb.pw is not None, "reaper stopped playwright"
    assert nb.page_pool is pool, "reaper replaced the pool"
    assert pool._circuit_state == "closed", "reaper opened the circuit"
    assert pool._idle_closed > 0, "the reaper did not actually run"
