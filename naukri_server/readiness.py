"""Server readiness -- what is still starting, and how a tool says so.

WHY THIS EXISTS. Until 2026-08-30 the whole expensive startup sequence (Chrome
launch against the persistent profile, the JSON->SQLite migration, the browser
watchdog, 21 health probes, 10 scheduled tasks, a startup health check) ran
INSIDE the MCP lifespan, and the MCP lifespan is entered by
``mcp.server.lowlevel.server.Server.run`` BEFORE it reads the first message off
the stream. Under ``--http`` that ``Server.run`` is started per SESSION, so the
first client's ``initialize`` request sat unread in the transport for the entire
duration of that sequence -- measured at ~7 minutes on 2026-08-30. Clients do
not wait that long: they time out and drop every tool the server has.

Moving that work into a background task fixes the hang, but it buys a NEW duty.
Before, "not ready" was expressed as a client that never got an initialize
response -- unusable, but not misleading. Now the server answers immediately,
so it has to be able to say "not ready yet" IN BAND, and say it in a way no
caller can mistake for an answer. That is this module's whole job: a named
state, the phase still running, and how long it has been running. Never an
empty list, never a zero, never a default that looks like data.

This module is deliberately dependency-free (stdlib only) so that ``browser``,
``api`` and ``error_handler`` can all import it without a cycle. The exception
type lives in ``browser.py`` next to ``BrowserUnavailableError``, because
"the browser is not up yet" is a browser-availability condition and every
existing ``except BrowserUnavailableError`` site must keep catching it.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# How long a tool call waits for the background warm-up to hand it a usable
# browser before it gives up and reports SERVER_WARMING_UP.
#
# WHY A WAIT AT ALL, AND WHY THIS NUMBER:
#
#  * A WARM start is the common case -- the profile is in the OS page cache and
#    Chrome comes up in single-digit seconds. Refusing instantly would turn
#    every ordinary restart into a burst of errors a human has to interpret,
#    for the sake of a few seconds of patience.
#  * A COLD start is not coverable by ANY wait short enough to be safe.
#    MEASURED 2026-08-30 on the live server: browser.start() ran from 10:09
#    (chrome-profile.lock written) to 10:11:22 (profile fully written) -- over
#    two minutes, on a box that was still finishing its logon. No client-safe
#    budget covers that, so past the bound the only honest answer is
#    "still warming up, phase=X, elapsed=Ys".
#  * The ceiling is set by the CLIENT, not by us. An MCP client that stops
#    waiting mid-call turns our patience into a dropped tool -- precisely the
#    failure this change exists to remove. 20s sits comfortably under the 30s
#    floor that MCP clients conventionally allow a request, and three orders
#    below this server's own TOOL_WATCHDOG_TIMEOUT (600s) backstop.
#
# It deliberately does NOT scale with the number of warm-up phases: a tool call
# waits for the BROWSER phase only, because that is the only phase a tool can
# actually be blocked on. Probes and the scheduler come up behind it and no
# tool call needs them.
WARMUP_TOOL_WAIT_SECONDS = 20.0

# How long shutdown lets an in-flight warm-up finish before cancelling it.
# Long enough that a warm browser launch completes and stop() therefore has a
# coherent context to tear down; short enough that a wedged launch cannot hold
# the process open. On cancellation the profile lock is still released, by
# NaukriBrowser.stop()'s own _holds_profile_lock branch.
WARMUP_SHUTDOWN_GRACE_SECONDS = 30.0

# Warm-up phase names. Ordered as they run.
PHASE_BROWSER = "browser"
PHASE_MIGRATION = "migration"
PHASE_WATCHDOG = "watchdog"
PHASE_PROBES = "probes"
PHASE_SCHEDULER = "scheduler"

STATE_IDLE = "idle"          # warm-up never started (unit tests, stdio pre-boot)
STATE_WARMING = "warming"
STATE_READY = "ready"
STATE_FAILED = "failed"


class Readiness:
    """The server's own answer to "are you up yet, and if not, what is left".

    One process-wide instance (``readiness`` below). Default state is IDLE and
    every predicate is false in it, so importing this module changes no
    behaviour anywhere -- the gates only engage once ``begin()`` is called by
    the lifespan.
    """

    def __init__(self) -> None:
        self._state = STATE_IDLE
        self._phase: Optional[str] = None
        self._started_at: Optional[float] = None
        self._finished_at: Optional[float] = None
        self._error: Optional[str] = None
        # Created in begin(), not __init__: an asyncio.Event constructed at
        # import time on 3.10+ is loop-agnostic, but recreating them per warm-up
        # also gives reset() a clean slate between tests.
        self._browser_event: Optional[asyncio.Event] = None
        self._ready_event: Optional[asyncio.Event] = None

    # -- transitions ------------------------------------------------------

    def begin(self, phase: str = PHASE_BROWSER) -> None:
        """Warm-up has started. Called by the lifespan before it yields.

        Takes the FIRST phase rather than leaving it None. The warm-up runs as
        a task, so between ``create_task`` and that task's first step there is a
        real window in which the lifespan has already returned and a tool call
        can already have arrived. Leaving the phase blank in that window means
        the first caller of a just-started server -- the likeliest caller there
        is -- gets ``phase: null``, which is exactly the uninformative default
        this whole state object exists to avoid.
        """
        self._state = STATE_WARMING
        self._phase = phase
        self._started_at = time.monotonic()
        self._finished_at = None
        self._error = None
        self._browser_event = asyncio.Event()
        self._ready_event = asyncio.Event()

    def enter(self, phase: str) -> None:
        """Name the phase now running, so a refusal can say what it is."""
        self._phase = phase
        logger.info("Warm-up phase: %s (t+%.1fs)", phase, self.elapsed_seconds)

    def browser_up(self) -> None:
        """The browser phase finished. Releases every waiting tool call."""
        if self._browser_event is not None:
            self._browser_event.set()

    def ready(self) -> None:
        """Every phase finished."""
        self._state = STATE_READY
        self._phase = None
        self._finished_at = time.monotonic()
        self.browser_up()
        if self._ready_event is not None:
            self._ready_event.set()
        logger.info("Server warm-up complete in %.1fs", self.elapsed_seconds)

    def failed(self, error: object) -> None:
        """Warm-up died.

        Both events are SET, and that is load-bearing for exactly one group:
        the callers already parked inside ``wait_for_browser``. A caller that
        arrives after this point short-circuits on the state check and never
        looks at the event; a caller already inside the wait has nothing but
        the event, and without this would sit out the full
        WARMUP_TOOL_WAIT_SECONDS on a browser that is never coming.
        """
        self._state = STATE_FAILED
        self._finished_at = time.monotonic()
        self._error = "%s: %s" % (type(error).__name__, error) if isinstance(error, BaseException) else str(error)
        self.browser_up()
        if self._ready_event is not None:
            self._ready_event.set()
        logger.error("Server warm-up FAILED at phase %s: %s", self._phase, self._error)

    def reset(self) -> None:
        """Back to IDLE -- every predicate false, both events dropped.

        Called by the lifespan's teardown (so a restarted server does not
        inherit the last run's phase) and by tests. Written out rather than
        delegating to ``__init__`` so that adding a field without adding it here
        is a visible omission instead of a silent one.
        """
        self._state = STATE_IDLE
        self._phase = None
        self._started_at = None
        self._finished_at = None
        self._error = None
        self._browser_event = None
        self._ready_event = None

    # -- predicates -------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def phase(self) -> Optional[str]:
        return self._phase

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        end = self._finished_at if self._finished_at is not None else time.monotonic()
        return end - self._started_at

    @property
    def is_warming(self) -> bool:
        return self._state == STATE_WARMING

    @property
    def browser_pending(self) -> bool:
        """Warm-up is running and has not yet produced a usable browser.

        This -- not ``is_warming`` -- is what a browser-needing call site tests.
        Once the browser is up, probes and the scheduler still coming up behind
        it must not make a tool refuse.
        """
        if self._state != STATE_WARMING:
            return False
        return self._browser_event is None or not self._browser_event.is_set()

    # -- waiting ----------------------------------------------------------

    async def wait_for_browser(self, timeout: Optional[float] = None) -> bool:
        """Block up to ``timeout`` for the browser phase. True if it is up.

        Returns False on timeout rather than raising, so the caller decides
        what its own "not ready" looks like.

        ``timeout=None`` resolves WARMUP_TOOL_WAIT_SECONDS at CALL time, not as
        a default-argument value bound at import. A default argument would have
        frozen the number into the function object, so nothing -- an operator
        override, a test proving the bound holds -- could change it without
        rewriting the signature.
        """
        if timeout is None:
            timeout = WARMUP_TOOL_WAIT_SECONDS
        if not self.browser_pending:
            return True
        event = self._browser_event
        if event is None:  # pragma: no cover - browser_pending guarantees non-None
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # -- reporting --------------------------------------------------------

    def snapshot(self) -> dict:
        """Machine-readable readiness, for health output and error payloads.

        NOTE THE ABSENCE OF AN ETA. A first warm-up after a process start has
        no history to predict from, and that is exactly the case that matters,
        so this returns ``retry_after_seconds`` -- a retry HINT -- and never a
        predicted finish time. A fabricated ETA is the same class of lie as an
        empty list: it looks like an answer.
        """
        return {
            "state": self._state,
            "phase": self._phase,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "browser_ready": not self.browser_pending,
            "retry_after_seconds": int(WARMUP_TOOL_WAIT_SECONDS),
            "eta_seconds": None,
            "error": self._error,
        }

    def describe(self) -> str:
        """One sentence a human or an AI caller can act on."""
        return (
            "Server is still warming up (phase=%s, elapsed=%.0fs). The browser "
            "is not available yet. This is NOT a login failure and NOT an empty "
            "result -- retry in ~%ds. Call naukri_health_check for warm-up state."
            % (self._phase or "starting", self.elapsed_seconds, int(WARMUP_TOOL_WAIT_SECONDS))
        )


#: Process-wide instance. IDLE until the lifespan calls ``begin()``.
readiness = Readiness()
