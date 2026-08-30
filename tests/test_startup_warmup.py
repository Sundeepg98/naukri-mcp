"""The startup split: the lifespan returns fast, warm-up runs behind it.

WHAT WENT WRONG (measured 2026-08-30 against the live server, PID 15952).
Every expensive startup step -- Chrome launch against the persistent profile,
the JSON->SQLite migration, the browser watchdog, 21 health probes, 10
scheduled tasks, a startup health check -- ran inside ``naukri_server.lifespan``
BEFORE it yielded. ``mcp.server.lowlevel.server.Server.run`` enters that
lifespan at the top of a session and only afterwards reads the first message
off the transport, and under ``--http`` the session manager starts one
``Server.run`` per MCP session. So the first client's ``initialize`` sat unread
for the whole sequence: process start 10:01:32, browser launch beginning 10:09
(chrome-profile.lock), profile written 10:11:22. The client gave up and dropped
all 120 tools.

(The listening socket was never implicated. uvicorn binds after the STARLETTE
lifespan, which for a FastMCP app is only ``StreamableHTTPSessionManager.run()``
-- a task-group creation. Measured on a spare port the same morning: 8.6s to
accept, all of it module import. The tests below therefore pin the MCP
lifespan, which is where the stall actually was.)

WHAT THE FIX BUYS, AND WHAT IT COSTS. Answering fast means the server can now
be asked for things it cannot yet do, so every test here that is not about
speed is about HONESTY: a call that lands mid-warm-up must come back saying
"warming up, phase=X", never an empty list, never a zero, and above all never
"you are not logged in" -- which is what the token path used to say while the
session sitting in the profile was perfectly good.

Each test names, in its own docstring, the mutation it was shown failing at.
"""

import asyncio
import time

import pytest

import naukri_server
from naukri_server import readiness as readiness_mod
from naukri_server.browser import (
    NaukriBrowser,
    NotLoggedInError,
    ServerWarmingUpError,
    TokenManager,
)
from naukri_server.error_handler import handle_tool_action
from naukri_server.readiness import PHASE_BROWSER, readiness

# Every wait in this module must be short enough that a stuck warm-up costs the
# suite tenths of a second, and long enough that it cannot be satisfied by
# scheduling luck. 0.2s is ~200x the event-loop turn these tests need.
FAST_WAIT = 0.2


@pytest.fixture(autouse=True)
def _readiness_is_idle_again_afterwards():
    """Readiness is process-wide state; leaking a WARMING one poisons the suite.

    Not paranoia: ``browser_pending`` is consulted by `_NoPagePool.acquire`,
    `TokenManager.ensure_token` and both dispatch seams, so a test that left it
    warming would make unrelated tests wait FAST_WAIT or refuse outright.
    """
    readiness.reset()
    yield
    readiness.reset()


@pytest.fixture
def fast_wait(monkeypatch):
    """Shrink the tool-call wait budget for the duration of one test.

    Patching the MODULE attribute works only because ``wait_for_browser``
    resolves WARMUP_TOOL_WAIT_SECONDS at call time. As a default argument it
    would have been frozen into the function object at import and this fixture
    would silently do nothing -- the test would still pass, after waiting the
    full 20 seconds.
    """
    monkeypatch.setattr(readiness_mod, "WARMUP_TOOL_WAIT_SECONDS", FAST_WAIT)
    return FAST_WAIT


@pytest.fixture
def stub_warm_up(monkeypatch):
    """Replace every warm-up phase with something instant and controllable.

    Returns a dict with a ``browser_gate`` event: the fake ``browser.start``
    blocks on it, so a test decides exactly when the browser phase completes.
    Nothing here launches Chrome, touches the profile, or starts a real
    scheduler -- the point is the ORDERING, not the subsystems.
    """
    state = {"browser_gate": asyncio.Event(), "browser_started": False,
             "health_check_calls": 0}

    async def fake_browser_start():
        await state["browser_gate"].wait()
        state["browser_started"] = True

    async def fake_init_db():
        return None

    async def fake_migrate():
        return {"status": "skipped"}

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        async def start(self):
            return None

        async def stop(self):
            return None

    def fake_register_all(scheduler):
        return None

    async def counting_health_check(*a, **kw):
        state["health_check_calls"] += 1
        return {"summary": {"fail": 0}}

    import naukri_server.database as db_mod
    import naukri_server.health as health_mod
    import naukri_server.scheduler as sched_mod
    import naukri_server.scheduler_tasks as sched_tasks_mod
    import naukri_server.tools.health as tools_health_mod

    # Shutdown waits WARMUP_SHUTDOWN_GRACE_SECONDS (30s in production) for an
    # in-flight warm-up. A test whose assertion fires before it opens the gate
    # would otherwise pay that in full -- 30 real seconds per failure, which is
    # how a suite stops being run.
    monkeypatch.setattr(naukri_server, "WARMUP_SHUTDOWN_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(naukri_server._browser_instance, "start", fake_browser_start)
    monkeypatch.setattr(naukri_server._browser_instance, "stop", _FakeService().stop)
    monkeypatch.setattr(naukri_server, "init_db", fake_init_db)
    monkeypatch.setattr(db_mod, "migrate_json_to_sqlite", fake_migrate)
    monkeypatch.setattr(naukri_server, "BrowserWatchdog", _FakeService)
    monkeypatch.setattr(health_mod, "HealthProbeScheduler", _FakeService)
    monkeypatch.setattr(sched_mod, "TaskScheduler", _FakeService)
    monkeypatch.setattr(sched_tasks_mod, "register_all", fake_register_all)
    monkeypatch.setattr(tools_health_mod, "naukri_health_check", counting_health_check)
    return state


async def test_lifespan_yields_without_awaiting_the_expensive_warm_up(stub_warm_up):
    """THE REGRESSION. The lifespan must reach its yield while the browser is
    still launching.

    SHOWN FAILING AT: the pre-fix lifespan, which awaited
    ``_browser_instance.start()`` inline. With the browser gate shut,
    ``__aenter__`` never returns and this fails as::

        TimeoutError
        (asyncio.wait_for on lifespan.__aenter__, 2.0s)

    2.0s is ~250x the measured cost of what legitimately remains on the
    critical path (init_db, 0.099s).
    """
    cm = naukri_server.lifespan(None)
    await asyncio.wait_for(cm.__aenter__(), timeout=2.0)
    try:
        # Entered while the browser phase is provably still blocked.
        assert stub_warm_up["browser_started"] is False
        assert readiness.is_warming
        assert readiness.browser_pending
        assert readiness.phase == PHASE_BROWSER

        # Release it and the rest of warm-up runs to completion behind us.
        stub_warm_up["browser_gate"].set()
        for _ in range(200):
            if readiness.state == "ready":
                break
            await asyncio.sleep(0.01)
        assert readiness.state == "ready", readiness.snapshot()
        assert stub_warm_up["browser_started"] is True
    finally:
        await cm.__aexit__(None, None, None)


async def test_startup_health_check_does_not_run_during_startup(stub_warm_up):
    """The startup health check was removed, not relocated.

    SHOWN FAILING AT: restoring the ``await naukri_health_check(...)`` call at
    the end of ``_warm_up`` -- ``assert 1 == 0`` on the call counter.

    It only ever logged its result, it duplicates the 21 probes the same
    warm-up starts, and it is the same handler whose wedge on 2026-08-20 took
    an unrelated tool down with it.
    """
    cm = naukri_server.lifespan(None)
    await asyncio.wait_for(cm.__aenter__(), timeout=2.0)
    try:
        stub_warm_up["browser_gate"].set()
        for _ in range(200):
            if readiness.state == "ready":
                break
            await asyncio.sleep(0.01)
        assert readiness.state == "ready"
        assert stub_warm_up["health_check_calls"] == 0
    finally:
        await cm.__aexit__(None, None, None)


async def test_second_session_does_not_block_behind_the_first_warm_up(stub_warm_up):
    """``--dual`` shares one browser by ref-count; it must not share one stall.

    SHOWN FAILING AT: the pre-fix lifespan, where session 1 held
    ``_lifespan_lock`` across the whole browser launch, so session 2's
    ``__aenter__`` waited for it too -- TimeoutError at 2.0s.

    Also pins that a second entry does NOT start a second warm-up: the fake
    browser start would deadlock on an already-consumed gate if it did.
    """
    first = naukri_server.lifespan(None)
    await asyncio.wait_for(first.__aenter__(), timeout=2.0)
    second = naukri_server.lifespan(None)
    await asyncio.wait_for(second.__aenter__(), timeout=2.0)
    try:
        assert naukri_server._lifespan_refs == 2
        assert readiness.browser_pending
    finally:
        stub_warm_up["browser_gate"].set()
        await second.__aexit__(None, None, None)
        # Refs back to 1: the browser must NOT have been torn down yet.
        assert naukri_server._lifespan_refs == 1
        await first.__aexit__(None, None, None)
        assert naukri_server._lifespan_refs == 0


async def test_browser_tool_during_warm_up_says_warming_not_login(fast_wait):
    """A browser-needing tool must name the warm-up, and must not return data.

    SHOWN FAILING AT: reverting ``_NoPagePool.acquire`` to its single
    unconditional raise -- the result comes back
    ``error_code == 'BROWSER_ERROR'`` with the message "Browser is not
    running - no page pool. Call naukri_login to start a session", which sends
    the caller at a login problem that does not exist.
    """
    readiness.begin()
    readiness.enter(PHASE_BROWSER)
    browser = NaukriBrowser()

    async def tool_body():
        async with browser.page_pool.acquire() as page:  # noqa: F841
            return {"status": "success", "jobs": []}

    out = await handle_tool_action(tool_body, "test.warming")

    assert out["status"] == "error", "a warming server must never answer with data"
    assert out["error_code"] == "SERVER_WARMING_UP", out
    assert "jobs" not in out, "no empty-list-shaped answer may leak out"
    assert out["readiness"]["phase"] == PHASE_BROWSER
    assert out["readiness"]["browser_ready"] is False
    assert out["readiness"]["eta_seconds"] is None, "never fabricate an ETA"
    assert "warming up" in out["message"].lower()


async def test_ensure_token_during_warm_up_does_not_claim_not_logged_in(fast_wait):
    """The wrong answer that looked most like a right one.

    SHOWN FAILING AT: removing the ``browser_pending`` guard from
    ``TokenManager.ensure_token`` -- it then raises
    ``NotLoggedInError('Not logged in - call naukri_login first')`` while the
    stored session is untouched and the browser is simply mid-launch. Acting on
    that advice starts a login that fights the launch for the profile lock.
    """
    readiness.begin()
    readiness.enter(PHASE_BROWSER)
    tm = TokenManager()
    assert tm._context is None and tm._token is None

    with pytest.raises(ServerWarmingUpError) as ei:
        await tm.ensure_token()

    assert "not logged in" not in str(ei.value).lower()
    assert "warming up" in str(ei.value).lower()
    # It is still a BrowserUnavailableError, so ~30 existing catch sites hold.
    from naukri_server.browser import BrowserUnavailableError

    assert isinstance(ei.value, BrowserUnavailableError)
    assert not isinstance(ei.value, NotLoggedInError)


async def test_dispatch_waits_for_the_browser_phase_before_running_the_tool(fast_wait):
    """The wait is a PRE-FLIGHT, so a slow-starting browser costs latency, not
    an error -- and the tool body is never run twice.

    SHOWN FAILING AT: deleting the ``if readiness.browser_pending: await
    readiness.wait_for_browser()`` block from ``handle_tool_action`` -- the body
    then runs while the browser is still pending and the assertion inside it
    fires, surfacing as ``error_code == 'INTERNAL_ERROR'``.
    """
    readiness.begin()
    readiness.enter(PHASE_BROWSER)
    calls = []

    async def release_soon():
        await asyncio.sleep(0.02)
        readiness.browser_up()

    async def tool_body():
        calls.append(1)
        assert not readiness.browser_pending, "body ran before the browser was up"
        return {"status": "success"}

    task = asyncio.create_task(release_soon())
    out = await handle_tool_action(tool_body, "test.preflight")
    await task

    assert out == {"status": "success"}
    assert calls == [1], "the body must run exactly once -- no retry-after-failure"


async def test_the_wait_is_bounded_and_then_refuses(fast_wait):
    """A warm-up that never finishes must not hold a caller forever.

    SHOWN FAILING AT: replacing ``asyncio.wait_for(event.wait(), timeout)`` in
    ``wait_for_browser`` with a bare ``await event.wait()`` -- the call never
    returns and the test fails on its own 5s wait_for.
    """
    readiness.begin()
    readiness.enter(PHASE_BROWSER)
    browser = NaukriBrowser()

    async def tool_body():
        async with browser.page_pool.acquire() as page:  # noqa: F841
            return {"status": "success"}

    t0 = time.monotonic()
    out = await asyncio.wait_for(handle_tool_action(tool_body, "test.bounded"), timeout=5.0)
    elapsed = time.monotonic() - t0

    assert out["error_code"] == "SERVER_WARMING_UP"
    assert elapsed >= FAST_WAIT, "it must actually have waited the budget"
    assert elapsed < 2.0, "and not much more than it"


async def test_failed_warm_up_releases_a_caller_already_waiting(fast_wait):
    """A dead warm-up must wake the callers already parked on it.

    SHOWN FAILING AT: dropping the ``self.browser_up()`` call from
    ``Readiness.failed`` -- the parked caller then burns the whole
    WARMUP_TOOL_WAIT_SECONDS on an event nothing will ever set and comes back
    False::

        assert False is True

    THE FIRST VERSION OF THIS TEST COULD NOT FAIL. It checked a caller arriving
    AFTER the failure, which short-circuits on ``_state != STATE_WARMING``
    before it ever looks at the event -- so it passed with the mutation applied
    and certified nothing. The event only matters to someone already inside the
    wait, which is what this now constructs.
    """
    readiness.begin()
    readiness.enter(PHASE_BROWSER)

    async def parked():
        t0 = time.monotonic()
        released = await readiness.wait_for_browser()
        return released, time.monotonic() - t0

    waiter = asyncio.create_task(parked())
    # Let it get all the way inside the wait before anything fails.
    for _ in range(10):
        await asyncio.sleep(0)
    readiness.failed(RuntimeError("playwright is not installed"))

    released, elapsed = await asyncio.wait_for(waiter, timeout=2.0)

    assert released is True, "the parked caller was left waiting on a dead warm-up"
    assert elapsed < FAST_WAIT, "it waited out the budget instead of being woken"
    snap = readiness.snapshot()
    assert snap["state"] == "failed"
    assert "playwright is not installed" in snap["error"]

    # And a caller arriving after the fact is not blocked either.
    assert await readiness.wait_for_browser() is True


async def test_api_tool_seam_also_reports_warming_up(fast_wait):
    """handle_tool_action is not the only door into a tool.

    SHOWN FAILING AT: removing the ``except ServerWarmingUpError`` branch from
    ``api.api_tool`` -- the broad ``except Exception`` below it then answers
    ``error_code == 'API_ERROR'``, naming the wrong subsystem and reading as
    "Naukri rejected your request".
    """
    from naukri_server.api import api_tool

    readiness.begin()
    readiness.enter(PHASE_BROWSER)

    @api_tool("Fetch something")
    async def fake_rest_tool():
        raise ServerWarmingUpError(readiness.describe())

    out = await fake_rest_tool()

    assert out["error_code"] == "SERVER_WARMING_UP", out
    assert out["readiness"]["phase"] == PHASE_BROWSER


async def test_health_check_during_warm_up_reports_one_reason_not_five(fast_wait):
    """The surface the operator watches a restart on.

    SHOWN FAILING AT: removing the ``readiness.browser_pending`` short-circuit
    from ``naukri_health_check`` -- it then runs all five API checks, every one
    of them blocked on the same unstarted browser, and answers
    ``status == 'success'`` with a summary of five failures. Five reds for one
    cause reads as an API outage.
    """
    from naukri_server.tools.health import naukri_health_check

    readiness.begin()
    readiness.enter(PHASE_BROWSER)

    out = await naukri_health_check(include_browser=False)

    assert out["error_code"] == "SERVER_WARMING_UP", out
    assert out["readiness"]["phase"] == PHASE_BROWSER
    assert "checks" not in out, "no per-check verdicts may be published mid-warm-up"


async def test_idle_readiness_changes_nothing(fast_wait):
    """The default (never-begun) state must be inert.

    SHOWN FAILING AT: making ``browser_pending`` true for STATE_IDLE -- the
    whole existing suite then waits on, or refuses for, a warm-up that is not
    running. This is the check that lets the other 3980 tests stay untouched.
    """
    assert readiness.state == "idle"
    assert readiness.is_warming is False
    assert readiness.browser_pending is False
    assert await readiness.wait_for_browser(timeout=0.0) is True

    browser = NaukriBrowser()
    with pytest.raises(Exception) as ei:
        browser.page_pool.acquire()
    # Unchanged pre-existing behaviour: the "browser is down" advice, not the
    # warm-up advice.
    assert not isinstance(ei.value, ServerWarmingUpError)
    assert "naukri_login" in str(ei.value)
