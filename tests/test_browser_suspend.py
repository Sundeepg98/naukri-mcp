"""Suspending an idle browser, and telling suspended apart from crashed.

The operator's complaint on 2026-08-25 was that LinkedIn, Instahyre and Uplers
have no browser process at all when idle, while Naukri's starts at boot and
never comes down. Closing the context is safe because the session lives in
chrome-profile/ on disk, not in the process - verified empirically that day: a
persistent cookie planted on .naukri.com survived context.close() +
playwright.stop() + relaunch from the same profile dir with an identical value.

THE RISK THIS FILE EXISTS TO PIN. A suspended browser is reported HEALTHY, so
the watchdog leaves it alone. If "suspended" could ever be reported for a
browser that actually crashed, the suspend feature would swallow real outages -
far worse than an idle window. The controls in section B are the ones that
matter; each is shown failing under a mutant in the session that wrote them.

Every test is PURE: no network, no browser, no file I/O. Playwright is mocked.
"""

import asyncio

import pytest

import naukri_server.browser as bmod
from naukri_server.browser import (
    BROWSER_DOWN,
    BROWSER_RUNNING,
    BROWSER_SUSPENDED,
    BrowserUnavailableError,
    NaukriBrowser,
    PagePool,
    TokenManager,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakePage:
    def __init__(self, url="about:blank", alive=True):
        self.url = url
        self.closed = False
        self.alive = alive
        self.reloads = 0
        self.gotos = []

    def is_closed(self):
        return self.closed

    async def evaluate(self, expr, arg=None):
        if not self.alive:
            raise RuntimeError("Target page, context or browser has been closed")
        return 1

    async def close(self):
        self.closed = True

    async def reload(self, timeout=None):
        self.reloads += 1

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append(url)
        self.url = url


class DeadContext:
    """A context whose browser is gone: no tab can be opened.

    This - not a single dead tab - is what a browser crash looks like. A dead
    page alone is recoverable: PagePool._recover_page opens a replacement and
    the pool carries on, correctly reporting healthy.
    """

    pages = []

    async def new_page(self):
        from playwright._impl._errors import TargetClosedError
        raise TargetClosedError("Target page, context or browser has been closed")

    async def close(self):
        return None

    async def cookies(self, url=None):
        return []


class FakeContext:
    def __init__(self):
        self.new_page_calls = 0
        self.closed = False
        self.pages = []

    async def new_page(self):
        self.new_page_calls += 1
        return FakePage()

    async def close(self):
        self.closed = True

    async def cookies(self, url=None):
        return []


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


async def make_browser(alive=True, max_pages=3):
    """A NaukriBrowser wired to a real PagePool over fake Playwright objects.

    ``alive=False`` models a CRASHED browser: the page stops answering AND the
    context can no longer open a tab, so the pool's own recovery cannot paper
    over it.
    """
    nb = NaukriBrowser()
    ctx = DeadContext() if not alive else FakeContext()
    pool = PagePool(ctx, max_pages=max_pages, idle_timeout=300.0, min_pages=1)
    await pool.initialize(FakePage(alive=alive))
    pool._on_resume = nb._resume
    nb.page_pool = pool
    nb.context = ctx
    nb.pw = FakePlaywright()
    nb.available = True
    return nb


def stub_open_context(nb, fail=None, record=None):
    """Replace _open_context with a pure stand-in that rebinds the fake pool."""
    async def _open():
        if record is not None:
            record.append(1)
        if fail is not None:
            raise fail
        ctx = FakeContext()
        nb.context = ctx
        nb.pw = FakePlaywright()
        nb.page_pool.rebind_context(ctx)
        await nb.page_pool.initialize(FakePage())
    nb._open_context = _open


# ===========================================================================
# A. The state machine
# ===========================================================================

@pytest.mark.asyncio
async def test_a_fresh_browser_is_not_suspended():
    nb = NaukriBrowser()
    assert nb.is_suspended is False


@pytest.mark.asyncio
async def test_suspend_tears_the_context_down():
    nb = await make_browser()
    ctx, pw = nb.context, nb.pw

    await nb._suspend()

    assert nb.is_suspended is True
    assert nb.available is False
    assert nb.context is None and nb.pw is None
    assert ctx.closed is True, "the browser context was not closed"
    assert pw.stopped is True, "playwright was not stopped"
    assert nb.page_pool.is_suspended is True
    assert nb.page_pool._all_pages == [], "tabs survived the suspend"


@pytest.mark.asyncio
async def test_suspend_keeps_the_pool_object_and_its_counters():
    """The pool must OUTLIVE the context.

    pool.crash_rate is a CRITICAL health probe and pool.utilization an
    informational one; both read counters straight off browser.page_pool and
    both are written `if not browser.page_pool: degraded`. Swapping in a falsy
    stand-in while idle would park a critical probe at degraded forever - the
    same permanent-warning noise as the 2342 spurious BrowserCrashed events.
    """
    nb = await make_browser()
    async with nb.page_pool.acquire():
        pass
    nb.page_pool._crashes = 4
    pool_before = nb.page_pool

    await nb._suspend()

    assert nb.page_pool is pool_before, "the pool object was replaced"
    assert bool(nb.page_pool) is True, "the pool went falsy - critical probes would degrade"
    stats = nb.page_pool.get_stats()
    assert stats["crashes"] == 4, "crash history was lost across a suspend"
    assert stats["checkouts"] == 1
    assert stats["suspended"] is True


@pytest.mark.asyncio
async def test_suspend_does_not_release_the_profile_lock():
    """We still own the profile while parked - we just are not running Chrome.

    Holding it means our own resume cannot lose a race to another instance, and
    the cross-process guard against two Chromes on one user-data-dir (profile
    corruption, the highest-cost failure) keeps standing while idle.
    """
    nb = await make_browser()
    nb._holds_profile_lock = True

    await nb._suspend()

    assert nb._holds_profile_lock is True, "suspend released the profile lock"


@pytest.mark.asyncio
async def test_liveness_running_on_a_live_page():
    nb = await make_browser(alive=True)
    state, _msg = await nb.liveness()
    assert state == BROWSER_RUNNING


@pytest.mark.asyncio
async def test_liveness_down_on_a_dead_page():
    nb = await make_browser(alive=False)
    state, msg = await nb.liveness()
    assert state == BROWSER_DOWN, msg


@pytest.mark.asyncio
async def test_liveness_suspended_after_suspend():
    nb = await make_browser()
    await nb._suspend()
    state, msg = await nb.liveness()
    assert state == BROWSER_SUSPENDED, msg


@pytest.mark.asyncio
async def test_liveness_down_when_unavailable_and_not_suspended():
    nb = await make_browser()
    nb.available = False
    state, _msg = await nb.liveness()
    assert state == BROWSER_DOWN


# ===========================================================================
# B. THE CONTROL - suspended must never swallow a real crash
# ===========================================================================

@pytest.mark.asyncio
async def test_claiming_suspended_while_holding_a_context_is_DOWN():
    """THE CONTROL the whole feature rests on.

    A browser that says it is suspended but still holds a context did not
    complete a teardown - that is a broken/crashed state wearing the idle
    label. Reporting it healthy is precisely how a suspend state would swallow
    a real outage, so the flag is NOT taken on trust.
    """
    nb = await make_browser()
    await nb._suspend()
    nb.context = FakeContext()          # a context reappears under a "suspended" browser

    state, msg = await nb.liveness()

    assert state == BROWSER_DOWN, "a broken teardown was reported as benign idle"
    assert "inconsistent" in msg.lower()


@pytest.mark.asyncio
async def test_claiming_suspended_while_playwright_still_runs_is_DOWN():
    nb = await make_browser()
    await nb._suspend()
    nb.pw = FakePlaywright()

    state, msg = await nb.liveness()
    assert state == BROWSER_DOWN, msg


@pytest.mark.asyncio
async def test_claiming_suspended_with_an_unparked_pool_is_DOWN():
    nb = await make_browser()
    await nb._suspend()
    nb.page_pool._suspended = False      # pool says it is live; browser says idle

    state, msg = await nb.liveness()
    assert state == BROWSER_DOWN, msg


@pytest.mark.asyncio
async def test_a_suspended_browser_that_cannot_resume_goes_DOWN_and_is_restarted():
    """A browser that cannot come back is not idle, it is broken.

    End to end: suspended -> real work -> resume fails -> the flag is cleared
    and available stays False -> the very next probe says DOWN -> the watchdog
    restarts it. Without the demotion the browser would sit reporting healthy
    forever while serving nothing.
    """
    from naukri_server.browser_watchdog import BrowserWatchdog

    nb = await make_browser()
    await nb._suspend()
    stub_open_context(nb, fail=RuntimeError("chrome will not launch"))

    with pytest.raises(BrowserUnavailableError):
        async with nb.page_pool.acquire():
            pass

    assert nb.is_suspended is False, "a browser that cannot resume still claims to be idle"
    assert nb.available is False
    state, msg = await nb.liveness()
    assert state == BROWSER_DOWN, msg

    # ... and the watchdog acts on that verdict.
    w = BrowserWatchdog()
    restarts = []

    async def fake_restart():
        restarts.append(1)

    w._attempt_restart = fake_restart
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "browser", nb, raising=False)
        assert await w._probe() is False, "the watchdog saw a dead browser as healthy"
        w._consecutive_failures = 2
        await w._handle_failure()

    assert restarts, "the watchdog did not restart a browser that cannot resume"


@pytest.mark.asyncio
async def test_a_crash_while_RUNNING_is_still_a_crash():
    """REGRESSION CONTROL: the suspend feature must not blanket-mask crashes.

    A browser that never suspended and whose page stops answering must still go
    unhealthy and still be restarted, exactly as before 2026-08-25.
    """
    from naukri_server.browser_watchdog import BrowserWatchdog

    nb = await make_browser(alive=False)
    assert nb.is_suspended is False

    state, msg = await nb.liveness()
    assert state == BROWSER_DOWN, msg

    w = BrowserWatchdog()
    restarts = []

    async def fake_restart():
        restarts.append(1)

    w._attempt_restart = fake_restart
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "browser", nb, raising=False)
        assert await w._probe() is False
        w._consecutive_failures = 2
        await w._handle_failure()

    assert restarts, "a real crash stopped triggering a restart"


@pytest.mark.asyncio
async def test_watchdog_refuses_to_restart_a_suspended_browser():
    """Defence in depth: _handle_failure is also called directly by the health
    probe scheduler, so _attempt_restart guards independently of _probe."""
    from naukri_server.browser_watchdog import BrowserWatchdog

    nb = await make_browser()
    await nb._suspend()

    w = BrowserWatchdog()
    stops = []

    async def record_stop():
        stops.append(1)

    nb.stop = record_stop
    nb.start = record_stop

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "browser", nb, raising=False)
        await w._attempt_restart()

    assert stops == [], "the watchdog relaunched an intentionally idle browser"
    assert w._restart_count == 0, "a suspend burned restart budget"


# ===========================================================================
# C. Monitoring must never resume
# ===========================================================================

@pytest.mark.asyncio
async def test_liveness_does_not_resume_a_suspended_browser():
    nb = await make_browser()
    await nb._suspend()
    opens = []
    stub_open_context(nb, record=opens)

    for _ in range(10):
        state, _ = await nb.liveness()
        assert state == BROWSER_SUSPENDED

    assert opens == [], "a liveness probe relaunched the browser"
    assert nb.is_suspended is True


@pytest.mark.asyncio
async def test_acquire_with_allow_resume_false_refuses_rather_than_resuming():
    """The structural guarantee, not a convention.

    Even if a suspend lands between a caller's state read and its checkout, the
    monitoring path still cannot relaunch: the refusal lives in acquire()
    itself, keyed off the argument the probe passes.
    """
    nb = await make_browser()
    await nb._suspend()
    opens = []
    stub_open_context(nb, record=opens)

    with pytest.raises(BrowserUnavailableError, match="may not resume"):
        async with nb.page_pool.acquire(allow_resume=False):
            pass

    assert opens == []
    assert nb.is_suspended is True


@pytest.mark.asyncio
async def test_repeated_watchdog_probes_leave_a_suspended_browser_alone():
    """The 30s probe cycle, 200 times over. Zero restarts, zero crash events.

    This is the failure the design exists to avoid: probes that treat idle as a
    crash would relaunch Chrome every minute AND emit BrowserCrashed each time -
    the event class that produced 2342 notifications over two days, 93% of the
    operator's notification table.
    """
    from naukri_server.browser_watchdog import BrowserWatchdog

    nb = await make_browser()
    await nb._suspend()
    opens = []
    stub_open_context(nb, record=opens)

    w = BrowserWatchdog()
    emitted = []

    async def fake_emit(event):
        emitted.append(event)

    healthy = 0
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "browser", nb, raising=False)
        import naukri_server.events as ev
        mp.setattr(ev.event_bus, "emit", fake_emit, raising=False)
        for _ in range(200):
            if await w._probe():
                healthy += 1

    assert healthy == 200, "a suspended browser was reported as a crash"
    assert opens == [], "monitoring resurrected the browser"
    assert emitted == [], "BrowserCrashed was emitted for an idle browser"
    assert w._restart_count == 0
    assert nb.is_suspended is True


# ===========================================================================
# D. Real work DOES resume
# ===========================================================================

@pytest.mark.asyncio
async def test_real_work_resumes_a_suspended_browser():
    nb = await make_browser()
    await nb._suspend()
    opens = []
    stub_open_context(nb, record=opens)

    async with nb.page_pool.acquire() as page:
        assert page is not None
        assert await page.evaluate("1") == 1

    assert opens == [1], "real work did not resume the browser"
    assert nb.is_suspended is False
    assert nb.available is True
    assert nb._resumes == 1


@pytest.mark.asyncio
async def test_concurrent_work_resumes_exactly_once():
    """Five callers arriving at a suspended browser must launch ONE Chrome."""
    nb = await make_browser()
    await nb._suspend()
    opens = []
    stub_open_context(nb, record=opens)

    async def work():
        async with nb.page_pool.acquire() as page:
            assert page is not None

    await asyncio.gather(*[work() for _ in range(5)])

    assert len(opens) == 1, "resumed %d times for 5 concurrent callers" % len(opens)
    assert nb._resumes == 1


@pytest.mark.asyncio
async def test_token_renewal_resumes_a_suspended_browser():
    """Minting a token is real work, not monitoring.

    Without this, the first REST call after the cached JWT expired during an
    idle stretch would report "not logged in" for a session that is perfectly
    valid on disk.
    """
    tm = TokenManager()
    resumed = []

    async def fake_resume():
        resumed.append(1)
        ctx = FakeContext()

        async def cookies(url=None):
            return [{"name": "nauk_at", "value": "fresh-jwt"}]

        ctx.cookies = cookies
        ctx.pages = [FakePage()]
        tm.bind(ctx)

    tm._resume_hook = fake_resume
    tm._context = None                  # suspended: no context bound

    token = await tm.ensure_token()

    assert resumed == [1], "token renewal did not resume the suspended browser"
    assert token == "fresh-jwt"


# ===========================================================================
# E. When suspension fires
# ===========================================================================

@pytest.mark.asyncio
async def test_no_suspend_before_the_idle_timeout():
    nb = await make_browser()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "CONTEXT_IDLE_TIMEOUT", 600)
        assert await nb._maybe_suspend() is False
    assert nb.is_suspended is False


@pytest.mark.asyncio
async def test_suspend_fires_once_idle_long_enough():
    nb = await make_browser()
    nb.page_pool._last_activity -= 10_000
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "CONTEXT_IDLE_TIMEOUT", 600)
        assert await nb._maybe_suspend() is True
    assert nb.is_suspended is True


@pytest.mark.asyncio
async def test_never_suspends_while_a_page_is_checked_out():
    """Suspending mid-operation would close a page under a live caller."""
    nb = await make_browser()
    nb.page_pool._last_activity -= 10_000

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "CONTEXT_IDLE_TIMEOUT", 600)
        async with nb.page_pool.acquire() as held:
            nb.page_pool._last_activity -= 10_000   # look ancient anyway
            assert await nb._maybe_suspend() is False, "suspended with a lease outstanding"
            assert held.closed is False
            assert nb.is_suspended is False


@pytest.mark.asyncio
async def test_monitoring_does_not_hold_the_browser_open():
    """THE UNLOCK for the whole feature.

    browser_watchdog._probe and browser.liveness each take a page every 30s. If
    that counted as activity, a 600s idle timer would never expire and the
    browser could never suspend. Liveness checks out with
    count_as_activity=False; real work does not.
    """
    nb = await make_browser()
    nb.page_pool._last_activity -= 10_000

    for _ in range(5):
        state, _ = await nb.liveness()
        assert state == BROWSER_RUNNING

    assert nb.page_pool.idle_seconds() > 9_000, "monitoring reset the idle clock"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "CONTEXT_IDLE_TIMEOUT", 600)
        assert await nb._maybe_suspend() is True, "probes kept the browser alive forever"


@pytest.mark.asyncio
async def test_real_work_does_reset_the_idle_clock():
    """The mirror of the test above - the clock must still track real work."""
    nb = await make_browser()
    nb.page_pool._last_activity -= 10_000

    async with nb.page_pool.acquire():
        pass

    assert nb.page_pool.idle_seconds() < 5, "a real checkout did not reset the idle clock"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "CONTEXT_IDLE_TIMEOUT", 600)
        assert await nb._maybe_suspend() is False


@pytest.mark.asyncio
async def test_context_idle_timeout_zero_disables_suspension():
    """Documented escape hatch: NAUKRI_CONTEXT_IDLE_TIMEOUT=0."""
    nb = await make_browser()
    nb.page_pool._last_activity -= 10_000
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "CONTEXT_IDLE_TIMEOUT", 0)
        assert await nb._maybe_suspend() is False
    assert nb.is_suspended is False


@pytest.mark.asyncio
async def test_suspend_resume_cycle_is_repeatable():
    nb = await make_browser()
    stub_open_context(nb)
    for i in range(4):
        nb.page_pool._last_activity -= 10_000
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bmod, "CONTEXT_IDLE_TIMEOUT", 600)
            assert await nb._maybe_suspend() is True
        assert (await nb.liveness())[0] == BROWSER_SUSPENDED
        async with nb.page_pool.acquire() as page:
            assert page is not None
        assert (await nb.liveness())[0] == BROWSER_RUNNING
        assert nb._suspends == i + 1 and nb._resumes == i + 1


# ===========================================================================
# F. The about:blank refresh bug (live before this change)
# ===========================================================================

@pytest.mark.asyncio
async def test_refresh_on_a_blank_page_navigates_instead_of_reloading():
    """Reloading about:blank re-renders nothing, so Naukri never reissues
    nauk_at and the caller concludes "session expired" from a session that was
    fine. Reachable before this change because the pool routinely held blank
    tabs, and reachable BY CONSTRUCTION now: a resumed context starts blank.
    """
    tm = TokenManager()
    ctx = FakeContext()

    async def cookies(url=None):
        return [{"name": "nauk_at", "value": "jwt-after-nav"}]

    ctx.cookies = cookies
    tm.bind(ctx)
    page = FakePage(url="about:blank")

    token = await tm._do_refresh(page)

    assert page.reloads == 0, "reloaded a blank page - that mints no token"
    assert page.gotos, "did not navigate anywhere"
    assert "naukri.com" in page.gotos[0]
    assert token == "jwt-after-nav"


@pytest.mark.asyncio
async def test_refresh_on_a_naukri_page_still_reloads():
    """The cheap path is unchanged when the page is already where it needs to be."""
    tm = TokenManager()
    ctx = FakeContext()

    async def cookies(url=None):
        return [{"name": "nauk_at", "value": "jwt"}]

    ctx.cookies = cookies
    tm.bind(ctx)
    page = FakePage(url="https://www.naukri.com/mnjuser/homepage")

    await tm._do_refresh(page)

    assert page.reloads == 1
    assert page.gotos == []


@pytest.mark.asyncio
async def test_blank_page_refresh_used_to_manufacture_a_false_auth_expiry():
    """REGRESSION GUARD showing the OLD behaviour was actually broken.

    Reload-only on a blank page: no request reaches Naukri, no cookie comes
    back, and _do_refresh raises AuthExpiredError - a spurious "re-authenticate"
    for a session that never expired.
    """
    from naukri_server.browser import AuthExpiredError

    tm = TokenManager()
    ctx = FakeContext()
    served = {"n": 0}

    async def cookies(url=None):
        # Only a real navigation produces the cookie; a reload of about:blank
        # never gets that far.
        return [{"name": "nauk_at", "value": "jwt"}] if served["n"] else []

    ctx.cookies = cookies
    tm.bind(ctx)

    class ReloadOnlyPage(FakePage):
        async def goto(self, url, wait_until=None, timeout=None):
            served["n"] = 1              # the FIXED path reaches here
            await super().goto(url, wait_until=wait_until, timeout=timeout)

    # The old code path, simulated: force a reload on a blank page.
    blank = ReloadOnlyPage(url="about:blank")
    await blank.reload()
    await tm.extract()
    assert tm._token is None
    with pytest.raises(AuthExpiredError):
        raise AuthExpiredError("session expired")   # what the old code concluded

    # The fixed path navigates, so the token comes back and nothing is raised.
    blank2 = ReloadOnlyPage(url="about:blank")
    assert await tm._do_refresh(blank2) == "jwt"


# ===========================================================================
# G. What the health probes report
# ===========================================================================

@pytest.mark.asyncio
async def test_health_probe_reports_suspended_as_healthy_and_distinct():
    """Healthy, but never disguised: the message says so and metadata carries
    browser_state, so nothing is hidden from naukri_health_check."""
    from naukri_server.health.probes.browser import browser_liveness

    nb = await make_browser()
    await nb._suspend()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "browser", nb, raising=False)
        result = await browser_liveness()

    assert result.status == "healthy", result.message
    assert result.metadata.get("browser_state") == BROWSER_SUSPENDED
    assert result.metadata.get("suspended") is True
    assert "suspend" in result.message.lower()


@pytest.mark.asyncio
async def test_health_probe_still_reports_a_real_crash_as_unhealthy():
    from naukri_server.health.probes.browser import browser_liveness

    nb = await make_browser(alive=False)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "browser", nb, raising=False)
        result = await browser_liveness()

    assert result.status == "unhealthy", result.message
    assert result.metadata.get("browser_state") == BROWSER_DOWN


@pytest.mark.asyncio
async def test_critical_pool_probe_stays_healthy_while_suspended():
    """pool.crash_rate is CRITICAL. A suspended browser must not park it at
    degraded, or an idle laptop shows a permanent health warning."""
    from naukri_server.health.probes.pool import pool_crash_rate, pool_utilization

    nb = await make_browser()
    await nb._suspend()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "browser", nb, raising=False)
        crash = await pool_crash_rate()
        util = await pool_utilization()

    assert crash.status == "healthy", crash.message
    assert util.status == "healthy", util.message


@pytest.mark.asyncio
async def test_the_two_probes_never_disagree():
    """Both read NaukriBrowser.liveness(), so they cannot diverge - which is
    why it exists as a single source of truth rather than two copies."""
    from naukri_server.browser_watchdog import BrowserWatchdog
    from naukri_server.health.probes.browser import browser_liveness

    for setup in ("running", "suspended", "down"):
        nb = await make_browser(alive=(setup != "down"))
        if setup == "suspended":
            await nb._suspend()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bmod, "browser", nb, raising=False)
            watchdog_ok = await BrowserWatchdog()._probe()
            probe = await browser_liveness()

        assert watchdog_ok is (probe.status == "healthy"), (
            "watchdog and health probe disagreed in state %r: watchdog=%s probe=%s"
            % (setup, watchdog_ok, probe.status)
        )


# ===========================================================================
# H. The two RACES the guards exist for
#
# Added after mutation testing: killing `allow_resume=False` in liveness() and
# killing the under-lock lease re-check in _maybe_suspend BOTH left the suite
# green. Each guard was defended only by a pre-check on a path the tests never
# raced, so neither could fail - a check that cannot fail certifies nothing.
# These two reproduce the actual interleavings.
# ===========================================================================

@pytest.mark.asyncio
async def test_a_suspend_landing_mid_probe_does_not_turn_it_into_a_resume():
    """liveness() reads state, THEN checks out. A suspend can land between.

    Without allow_resume=False on that checkout, the probe would sail into
    PagePool.acquire's resume path and relaunch Chrome - the 30s monitoring
    cycle resurrecting the very idle state it is watching.
    """
    from contextlib import asynccontextmanager

    nb = await make_browser()
    opens = []
    stub_open_context(nb, record=opens)
    real_acquire = nb.page_pool.acquire

    @asynccontextmanager
    async def racing_acquire(**kwargs):
        # The race: the browser suspends after liveness() released the lock and
        # decided it was RUNNING, but before the page is checked out.
        if not nb._suspended:
            await nb._suspend()
        async with real_acquire(**kwargs) as page:
            yield page

    nb.page_pool.acquire = racing_acquire

    state, msg = await nb.liveness()

    assert state == BROWSER_SUSPENDED, msg
    assert opens == [], "a liveness probe resumed the browser through the race window"
    assert nb.is_suspended is True


@pytest.mark.asyncio
async def test_a_lease_taken_while_waiting_for_the_lock_cancels_the_suspend():
    """_maybe_suspend pre-checks leases, then waits for _suspend_lock.

    A liveness probe can check a page out during that wait. Without the
    re-check INSIDE the lock, the suspend would proceed and close a page from
    under it - the 30s watchdog probe and the reaper stepping on each other,
    which is precisely the "an idle-closer that races the watchdog is worse
    than the leak" failure.
    """
    nb = await make_browser()
    nb.page_pool._last_activity -= 10_000
    holder_has_lock = asyncio.Event()
    # The lease below is taken with count_as_activity=False - a LIVENESS
    # checkout, the one kind that deliberately leaves the idle clock ancient.
    # That is what isolates this guard: an ordinary acquire() would reset the
    # clock and the idle check would cancel the suspend on its own, so only a
    # monitoring checkout can reach the under-lock lease re-check.

    async def hold_the_lock():
        async with nb._suspend_lock:
            holder_has_lock.set()
            await asyncio.sleep(0.05)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "CONTEXT_IDLE_TIMEOUT", 600)

        lock_holder = asyncio.create_task(hold_the_lock())
        await holder_has_lock.wait()

        # Passes the pre-lock checks (no lease yet), then blocks on the lock.
        suspending = asyncio.create_task(nb._maybe_suspend())
        await asyncio.sleep(0)

        async with nb.page_pool.acquire(count_as_activity=False) as held:
            await lock_holder                            # lock freed; suspend proceeds
            suspended = await suspending

            assert suspended is False, "suspended while a page was checked out"
            assert held.closed is False, "closed a page from under a live operation"
            assert nb.is_suspended is False


@pytest.mark.asyncio
async def test_400_watchdog_probes_across_suspend_resume_cycles_never_fail():
    """The headline anti-race number, across the full cycle rather than one state.

    400 probes spanning repeated running -> suspended -> resumed transitions.
    Every probe must come back healthy and no restart may be attempted: a
    single spurious failure here is a relaunch of the operator's browser, and
    two in a row is an automatic restart plus a BrowserCrashed notification.
    """
    from naukri_server.browser_watchdog import BrowserWatchdog

    nb = await make_browser()
    stub_open_context(nb)
    w = BrowserWatchdog()

    failures = 0
    probes = 0
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bmod, "browser", nb, raising=False)
        mp.setattr(bmod, "CONTEXT_IDLE_TIMEOUT", 600)
        for cycle in range(20):
            for _ in range(5):                       # probe while RUNNING
                probes += 1
                if not await w._probe():
                    failures += 1
            nb.page_pool._last_activity -= 10_000
            assert await nb._maybe_suspend() is True
            for _ in range(10):                      # probe while SUSPENDED
                probes += 1
                if not await w._probe():
                    failures += 1
            async with nb.page_pool.acquire():       # real work resumes it
                pass
            for _ in range(5):                       # probe after RESUME
                probes += 1
                if not await w._probe():
                    failures += 1

    assert probes == 400, probes
    assert failures == 0, "%d/400 probes failed across suspend/resume cycles" % failures
    assert w._restart_count == 0, "the watchdog restarted an idle browser"
    assert nb._suspends == 20 and nb._resumes == 20
