"""
Browser state — Playwright browser with multi-tab page pool and cached token manager.
"""

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Union

from playwright.async_api import async_playwright, BrowserContext, Page
from playwright._impl._errors import TargetClosedError, TimeoutError as PlaywrightTimeoutError

from naukri_server.config import (
    CHROME_PROFILE, NAUKRI_BASE, NAV_TIMEOUT, ELEMENT_TIMEOUT, MAX_TABS, CDP_PORT,
    PROFILE_API, SESSION_VALIDATE_TIMEOUT, TOKEN_RENEWAL_TIMEOUT, POOL_CHECKOUT_TIMEOUT,
    BROWSER_OP_TIMEOUT, TOKEN_REFRESH_TIMEOUT, TOKEN_LOCK_WAIT_TIMEOUT,
    PAGE_IDLE_TIMEOUT, PAGE_POOL_MIN_PAGES, PAGE_REAPER_INTERVAL,
    CONTEXT_IDLE_TIMEOUT,
    logger,
)
from naukri_server import profile_lock


class NotLoggedInError(ValueError):
    """No usable auth token is available and one could not be minted.

    Subclasses ``ValueError`` on purpose: this is the exception the token
    layer has always raised for "not logged in", and callers (plus the
    existing test suite) catch ``ValueError``. Giving it a distinct type lets
    callers CLASSIFY the failure — an expired session is not a browser fault —
    without breaking any of those callers.
    """
    pass


class AuthExpiredError(Exception):
    """Token refresh failed for an auth reason that re-trying will NOT fix:
    the session itself is gone (Naukri served a login wall / checkpoint, or the
    renewal navigation landed somewhere other than a logged-in page) so the
    short-lived JWT could not be re-minted. The caller must surface a clear
    "re-authenticate (naukri_login)" signal rather than treat it as transient.

    Distinct from a *transient* failure (navigation timeout, target closed,
    network blip) where the session may still be valid and a later retry can
    succeed — those are re-raised as their original exception type so existing
    backoff/retry logic still applies.
    """
    pass


class TokenRefreshUnavailable(NotLoggedInError):
    """A token refresh could not even be attempted, or could not finish, because
    the browser stopped answering - as distinct from "the session expired".

    Subclasses ``NotLoggedInError`` (and therefore ``ValueError``) so every
    existing caller keeps working, while remaining distinguishable in logs and
    in error classification. Raised only by the guarded refresh paths below.

    Why this exists: on 2026-08-20 an unresponsive Chrome parked
    ``TokenManager._refresh_lock`` forever. Because api.py calls
    ``ensure_token()`` before EVERY REST request, the whole server queued behind
    it - `naukri_health_check` hung, and the next unrelated tool
    (`naukri_activity_level`) hung too. A stuck refresh must now fail loudly and
    give the lock back instead of becoming a silent server-wide outage.
    """
    pass


class TokenManager:
    """Caches JWT token + cookies from browser context. Lock-free reads, locked refresh."""

    def __init__(self):
        self._token: Optional[str] = None
        self._cookies: Optional[str] = None
        self._refresh_lock = asyncio.Lock()
        self._context: Optional[BrowserContext] = None
        # Set by NaukriBrowser._open_context. Lets the renewal path below bring
        # a SUSPENDED browser back: renewing a token is real work, not
        # monitoring, so it is allowed to resume.
        self._resume_hook = None

    def bind(self, context: BrowserContext):
        """Bind to a browser context for cookie extraction."""
        self._context = context

    _AUTH_STATE_FILE = Path(CHROME_PROFILE) / "auth_state.json"

    async def extract(self):
        """Pull token + cookies from browser context into cache."""
        if not self._context:
            return
        try:
            # context.cookies() carries NO default timeout in Playwright's async
            # API. A wedged renderer keeps the CDP socket open, so this await
            # never returned rather than raising - and extract() runs INSIDE
            # _refresh_lock on two of the three refresh paths, which is how one
            # stuck cookie read became a server-wide outage.
            cookies = await asyncio.wait_for(
                self._context.cookies(NAUKRI_BASE), timeout=BROWSER_OP_TIMEOUT
            )
            self._cookies = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            for c in cookies:
                if c["name"] == "nauk_at":
                    self._token = c["value"]
                    # write_text + replace + chmod: three blocking file ops that
                    # used to run on the event loop, while holding the lock.
                    await asyncio.to_thread(self._export_auth_state)
                    return
        except Exception as e:
            logger.debug("Token extraction failed: %s", e)
        self._token = None

    def _export_auth_state(self):
        """Write current token + cookies to file for cross-process agents."""
        try:
            state = {"token": self._token, "cookies": self._cookies or "", "exported_at": time.time(), "cdp_port": CDP_PORT}
            tmp = self._AUTH_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(self._AUTH_STATE_FILE)
            os.chmod(str(self._AUTH_STATE_FILE), 0o600)
        except Exception as e:
            logger.debug("Auth state export failed: %s", e)

    def get_token(self) -> str:
        """Get cached JWT token. Raises if not available."""
        if self._token:
            return self._token
        raise NotLoggedInError("Not logged in — call naukri_login first")

    def get_cookies(self) -> str:
        """Get cached cookie header string."""
        return self._cookies or ""

    def invalidate(self):
        """Mark token as stale. Next ensure_token() will refresh."""
        self._token = None

    async def _with_refresh_lock(self, run, what: str):
        """Run ``run()`` inside ``_refresh_lock`` with a bound on BOTH the wait
        and the hold.

        Two distinct failure modes, so two distinct bounds:

        * the WAIT (``TOKEN_LOCK_WAIT_TIMEOUT``) - a caller must never queue
          behind a stuck refresher forever. This is the bound that stops one
          wedged tool from taking every other tool down with it.
        * the HOLD (``TOKEN_REFRESH_TIMEOUT``) - the refresher itself must hand
          the lock back even when Chrome has stopped answering. Without this the
          first bound only relocates the outage: every caller would fail instead
          of hanging, which is not a fix.

        The wait budget is deliberately LARGER than the hold budget, so a queued
        caller outlives the holder's own deadline and gets a real turn rather
        than a spurious error.
        """
        try:
            await asyncio.wait_for(
                self._refresh_lock.acquire(), timeout=TOKEN_LOCK_WAIT_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(
                "%s: refresh lock held by another caller for more than %ss - "
                "refusing to queue further. The browser is not answering.",
                what, TOKEN_LOCK_WAIT_TIMEOUT,
            )
            raise TokenRefreshUnavailable(
                f"{what}: another token refresh has held the refresh lock for over "
                f"{TOKEN_LOCK_WAIT_TIMEOUT}s - the browser is not answering. "
                "Restart the browser (naukri_login) before retrying."
            )
        try:
            return await asyncio.wait_for(run(), timeout=TOKEN_REFRESH_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error(
                "%s: token refresh did not complete within %ss - releasing the "
                "refresh lock so other callers are not wedged behind it.",
                what, TOKEN_REFRESH_TIMEOUT,
            )
            raise TokenRefreshUnavailable(
                f"{what}: token refresh did not complete within "
                f"{TOKEN_REFRESH_TIMEOUT}s - the browser is not answering."
            )
        finally:
            self._refresh_lock.release()

    async def _do_refresh(self, page: Page) -> Optional[str]:
        """Reload ``page`` and re-extract the token. Caller MUST hold ``_refresh_lock``.

        Failure handling (Fix A — no silent swallow): the reload result is
        classified rather than ignored.
          * Navigation timeout / target-closed / connection blip → *transient*:
            the session may still be valid, so the original exception is
            re-raised for the caller's existing backoff/retry to handle.
          * Navigation succeeds but no ``nauk_at`` reappears → *auth expiry /
            login-wall / checkpoint*: re-trying won't help; raise
            ``AuthExpiredError`` so the caller can surface "re-authenticate".
        Both paths are LOGGED (not debug-swallowed) so a checkpoint is no longer
        indistinguishable from a transient timeout.
        """
        # RELOAD ONLY WORKS ON A NAUKRI PAGE. Reloading about:blank re-renders
        # nothing, Naukri never sees a request, no nauk_at is reissued, and the
        # code below then concludes "session expired" - a false AuthExpiredError
        # from a session that was fine. That was reachable before 2026-08-25
        # because the pool routinely held about:blank tabs, and it is reachable
        # by construction now: a resumed context starts on a blank page. So
        # navigate somewhere real unless we are already there.
        try:
            current = page.url or ""
        except Exception:
            current = ""
        try:
            if NAUKRI_BASE in current:
                await page.reload(timeout=NAV_TIMEOUT)
            else:
                logger.debug(
                    "Token refresh: page is at %r, not on Naukri - navigating instead "
                    "of reloading", current[:60],
                )
                await page.goto(
                    f"{NAUKRI_BASE}/mnjuser/homepage",
                    wait_until="domcontentloaded", timeout=NAV_TIMEOUT,
                )
        except (PlaywrightTimeoutError, TargetClosedError, asyncio.TimeoutError) as e:
            # Transient: nav didn't complete. Don't swallow — log + re-raise
            # so the API layer's retry/backoff can act on it.
            logger.warning(
                "Token refresh reload failed (transient %s) — session may still be valid, "
                "surfacing for retry", type(e).__name__,
            )
            raise
        except Exception as e:
            logger.warning("Token refresh reload failed (transient %s): %s", type(e).__name__, e)
            raise
        await self.extract()
        if not self._token:
            # Reached a page but no auth cookie reappeared → the session is
            # gone (login wall / checkpoint), not a transient timeout.
            logger.error(
                "Token refresh navigated successfully but no nauk_at cookie returned — "
                "session expired (login-wall / checkpoint). Re-authentication required."
            )
            raise AuthExpiredError(
                "Session expired — Naukri returned no auth token after reload "
                "(login-wall or checkpoint). Call naukri_login to re-authenticate."
            )
        return self._token

    async def refresh(self, page: Page) -> Optional[str]:
        """Re-extract token by reloading a page. Protected by lock to prevent parallel refreshes.

        See ``_do_refresh`` for the (non-swallowing) failure classification.
        """
        async def _run():
            # Double-check after acquiring lock — another refresh may have succeeded
            if self._token:
                return self._token
            return await self._do_refresh(page)

        return await self._with_refresh_lock(_run, "refresh")

    async def refresh_via_pool(self, page_pool, stale_token: Optional[str] = None) -> Optional[str]:
        """Full token refresh that owns BOTH the single refresh lock AND the page
        checkout, used by the API layer (Fix B).

        Why this exists: previously ``api.py`` held its OWN module-level lock,
        then *separately* called ``page_pool.acquire()`` and ``refresh(page)``
        (which grabbed a *different* lock). That meant (a) a 401 in api.py and a
        concurrent ``ensure_token()`` serialized on different locks → refresh
        storm, and (b) the pool page was checked out while a lock was held →
        deadlock risk at MAX_TABS. Routing every refresh through this one method
        unifies serialization on ``self._refresh_lock`` and ensures only ONE
        page is ever checked out for refresh at a time (the lock gates it), so
        concurrent refreshers can't exhaust the pool against each other.

        ``stale_token`` is the (now-rejected) token the caller just used. If,
        after acquiring the lock, the cached token is present AND different from
        ``stale_token``, another refresher already re-minted it — we skip the
        browser round-trip entirely (this is what collapses a concurrent storm
        to a single real refresh). Pass ``None`` to force a refresh.

        The page is acquired INSIDE the lock — but because the lock already
        serializes refreshers to one-at-a-time, at most a single refresh page is
        outstanding, so this cannot self-deadlock the pool.
        """
        async def _run():
            # Another refresher already produced a NEW token while we waited for
            # the lock → reuse it, skip the expensive browser round-trip.
            if self._token and self._token != stale_token:
                return self._token
            self._token = None  # our token is stale → force _do_refresh to reload
            async with page_pool.acquire() as page:
                return await self._do_refresh(page)

        return await self._with_refresh_lock(_run, "refresh_via_pool")

    async def ensure_token(self) -> str:
        """Get token, attempting extraction if cache is empty.

        If extraction from cached cookies fails, navigates to a Naukri page
        to trigger JWT renewal (the session cookie may still be valid even
        when the short-lived nauk_at JWT has expired). Uses _refresh_lock to
        prevent parallel navigation races.
        """
        if self._token:
            return self._token
        await self.extract()
        if self._token:
            return self._token
        # JWT missing/expired — try navigating to trigger server-side renewal.
        # The lock prevents multiple parallel callers from navigating at once;
        # _with_refresh_lock additionally bounds both the wait and the hold, so a
        # browser that stops answering can no longer park this lock and freeze
        # every REST-backed tool in the server behind it.
        async def _renew():
            # Double-check after acquiring lock — another caller may have renewed
            if self._token:
                return
            if self._context is None and self._resume_hook is not None:
                # The browser is suspended (idle). Minting a token is real work,
                # so bring it back rather than reporting "not logged in" - which
                # is what this would otherwise do the moment the cached JWT
                # expired during an idle stretch.
                try:
                    await self._resume_hook()
                except Exception as e:
                    logger.warning("Token renewal could not resume the browser: %s", e)
            if self._context:
                try:
                    pages = self._context.pages
                    # context.new_page() has NO default timeout: on a wedged
                    # renderer it never returns and never raises.
                    page = pages[0] if pages else await asyncio.wait_for(
                        self._context.new_page(), timeout=BROWSER_OP_TIMEOUT
                    )
                    await page.goto(f"{NAUKRI_BASE}/mnjuser/homepage",
                                    wait_until="domcontentloaded", timeout=TOKEN_RENEWAL_TIMEOUT)
                    await self.extract()
                    if not self._token:
                        # Reached the homepage but no auth cookie was reissued —
                        # the session is gone (login-wall / checkpoint), not a
                        # transient blip. Log at ERROR so it is distinguishable.
                        logger.error(
                            "Token renewal navigation completed but no nauk_at cookie returned — "
                            "session expired (login-wall / checkpoint). Re-authentication required."
                        )
                except (PlaywrightTimeoutError, TargetClosedError, asyncio.TimeoutError) as e:
                    # Transient navigation failure — the session may still be
                    # valid; a later call can retry. Don't silently debug-swallow.
                    logger.warning(
                        "Token renewal navigation failed (transient %s) — will retry on next call",
                        type(e).__name__,
                    )
                except Exception as e:
                    logger.warning("Token renewal navigation failed (%s): %s", type(e).__name__, e)

        if self._token:
            return self._token
        await self._with_refresh_lock(_renew, "ensure_token")
        if not self._token:
            raise NotLoggedInError("Not logged in — call naukri_login first")
        return self._token


# Seconds a liveness round-trip may take before the page is declared dead.
# Deliberately short: this runs on every page checkout.
BROWSER_LIVENESS_TIMEOUT = 5.0


async def is_page_alive(page, timeout: float = BROWSER_LIVENESS_TIMEOUT) -> bool:
    """Return True only if the page ANSWERED a round-trip from the browser.

    Why not page.url: in Playwright 1.58.0 Page.url resolves to Frame.url ->
    "self._url or empty string" (_impl/_frame.py), a cached attribute. No IPC
    leaves the process, so the read cannot raise even when the target, the
    context and the whole browser are gone. On 2026-08-20 that turned a crashed
    browser into a 37-minute silent outage: seven BrowserCrashed events, zero
    restarts, and a pool that kept handing out dead pages.

    page.evaluate("1") is the cheapest expression that must cross the CDP
    channel and come back, so a dead target raises and a wedged renderer times
    out. Both are answered False rather than propagated, because callers want a
    verdict, not an exception.
    """
    if page is None:
        return False
    try:
        if page.is_closed():
            return False
    except Exception:
        # A page that cannot even report its own closed-ness is not alive.
        return False
    try:
        await asyncio.wait_for(page.evaluate("1"), timeout=timeout)
        return True
    except Exception:
        return False


class BrowserUnavailableError(Exception):
    """Raised when circuit breaker is open — browser is known-dead."""
    pass


# The three states a liveness caller can be told about. RUNNING and DOWN are the
# pre-2026-08-25 binary; SUSPENDED is the state added so an intentionally idle
# browser stops being reported as a crash.
BROWSER_RUNNING = "running"
BROWSER_SUSPENDED = "suspended"
BROWSER_DOWN = "down"


class _NoPagePool:
    """Stand-in for ``page_pool`` while the browser is not started.

    ``PagePool.acquire`` already fails cleanly with BrowserUnavailableError when
    the circuit is open, but before ``start()`` and after ``stop()`` the pool
    attribute itself is None — so all 27 ``browser.page_pool.acquire()`` call
    sites raised a bare ``AttributeError: 'NoneType' object has no attribute
    'acquire'`` instead, which error_handler cannot map and which tells the
    caller nothing about what to do. This keeps the same failure typed.

    Deliberately FALSY: every non-acquire use of the attribute is written as
    ``if browser.page_pool`` / ``... if self.page_pool else None`` (health.py,
    probes/pool.py, probes/browser.py, browser_watchdog.py, stop()), and those
    guards must keep seeing "no pool".
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def acquire(self):
        raise BrowserUnavailableError(
            "Browser is not running — no page pool. Call naukri_login to start "
            "a session, or check naukri_health_check for watchdog state."
        )


_NO_POOL = _NoPagePool()


class PagePool:
    """Manages a pool of browser pages (tabs) with semaphore-based checkout."""

    _MAX_CRASHES = 10  # Max crashes before entering degraded mode

    def __init__(self, context: BrowserContext, max_pages: int = 3,
                 idle_timeout: float = PAGE_IDLE_TIMEOUT,
                 min_pages: int = PAGE_POOL_MIN_PAGES):
        self._context = context
        self._max_pages = max_pages
        self._semaphore = asyncio.Semaphore(max_pages)
        self._available: asyncio.Queue[Page] = asyncio.Queue()
        self._all_pages: list[Page] = []
        # Idle reaping. _last_used is keyed by the Page object and holds the
        # monotonic time the page was RETURNED to the pool - i.e. the moment it
        # went idle. A page absent from this map is treated as just-used, so an
        # unknown page is never reaped.
        self._idle_timeout = idle_timeout
        self._min_pages = max(0, min_pages)
        self._last_used: dict[Page, float] = {}
        # Suspension. The pool object OUTLIVES the context on purpose: every
        # existing guard is written as `if browser.page_pool` and two health
        # probes (pool.utilization, pool.crash_rate - the latter CRITICAL) read
        # get_stats()/circuit_state off it. Swapping in a falsy stand-in while
        # idle would park a critical probe at "degraded" forever, which is the
        # notification-noise pathology all over again. So the pool stays, keeps
        # its counters, and simply has no context until someone needs one.
        self._suspended = False
        # Set by NaukriBrowser so a checkout can bring the browser back. This is
        # the ONLY resume trigger in the system, which is what makes "probes do
        # not resume" structural: the probe path never calls acquire().
        self._on_resume = None
        # Last REAL checkout. Liveness probes pass count_as_activity=False, so
        # this clock tracks work, not monitoring - without that the 30s probes
        # would hold the browser open forever.
        self._last_activity = time.monotonic()
        # Observability counters
        self._checkouts = 0
        self._returns = 0
        self._crashes = 0
        self._max_wait_ms = 0
        self._idle_closed = 0
        # Circuit breaker state
        self._circuit_state = "closed"  # closed, open, half_open
        self._circuit_failure_count = 0
        self._circuit_failure_threshold = 3
        self._circuit_open_time = 0.0
        self._circuit_cooldown = 30.0  # seconds before half-open

    async def initialize(self, first_page: Page):
        """Seed pool with the initial page from browser context."""
        self._all_pages.append(first_page)
        self._touch(first_page)
        await self._available.put(first_page)

    def _touch(self, page: Page) -> None:
        """Stamp ``page`` as used now. Called when a page enters the pool and
        every time one is returned to it."""
        try:
            self._last_used[page] = time.monotonic()
        except TypeError:
            # An unhashable page object cannot be tracked; it simply never
            # becomes reap-eligible, which is the safe direction.
            pass

    def _forget(self, page: Page) -> None:
        """Drop idle bookkeeping for a page leaving the pool."""
        try:
            self._last_used.pop(page, None)
        except TypeError:
            pass

    def _idle_age(self, page: Page, now: float) -> float:
        """Seconds since ``page`` was last used. Unknown pages read as 0 (fresh)."""
        try:
            return now - self._last_used.get(page, now)
        except TypeError:
            return 0.0

    # -- suspension ---------------------------------------------------------

    @property
    def is_suspended(self) -> bool:
        return self._suspended

    def outstanding_leases(self) -> int:
        """Checkouts that have not yet returned. Zero means nobody holds a page."""
        return max(0, self._checkouts - self._returns)

    def idle_seconds(self) -> float:
        """Seconds since the last REAL checkout (monitoring does not count)."""
        return time.monotonic() - self._last_activity

    async def park(self) -> None:
        """Close every page and give up the context, keeping the pool object.

        Counters, circuit state and configuration all survive, so the health
        probes that read this pool keep answering truthfully across an idle
        period. Only the pages and the context reference go.
        """
        await self.close_all()
        self._context = None
        self._suspended = True

    def rebind_context(self, context: BrowserContext) -> None:
        """Point the parked pool at a freshly launched context."""
        self._context = context
        self._suspended = False
        self._last_activity = time.monotonic()

    async def _create_page(self) -> Page:
        """Create a new tab in the shared browser context.

        Same contract as _recover_page: a dead context cannot open a tab, and
        the raw TargetClosedError that used to escape here reads to a caller as
        a page-level problem rather than "the browser is gone".
        """
        try:
            # new_page() has NO default timeout. A context whose browser has
            # wedged (socket open, renderer dead) neither answers nor errors, so
            # an unbounded await here parked the caller - and, on the refresh
            # path, the process-wide _refresh_lock with it. A browser that
            # cannot open a tab inside the budget IS dead, which is exactly what
            # the except branch below already concludes.
            page = await asyncio.wait_for(
                self._context.new_page(), timeout=BROWSER_OP_TIMEOUT
            )
        except Exception as exc:
            self._mark_context_dead("%s: %s" % (type(exc).__name__, exc))
            raise BrowserUnavailableError(
                "Browser context is dead - a new tab cannot be opened. "
                "The watchdog must restart the browser (naukri_login may be "
                "needed afterwards)."
            ) from exc
        self._all_pages.append(page)
        self._touch(page)
        logger.info("PagePool: created new tab (%d/%d)", len(self._all_pages), self._max_pages)
        return page

    async def _recover_page(self, dead_page: Page) -> Page:
        """Replace a crashed page with a new one.

        Raises BrowserUnavailableError when the CONTEXT itself is gone. The old
        version called new_page() on the same dead context and let the resulting
        TargetClosedError escape, so a caller saw what looked like a second page
        fault instead of "the browser is dead". The pool does not own the
        context lifecycle, so the honest move is to declare the browser
        unavailable and let the watchdog perform the restart.
        """
        logger.warning("PagePool: recovering crashed tab")
        if dead_page in self._all_pages:
            self._all_pages.remove(dead_page)
        self._forget(dead_page)
        try:
            # new_page() has NO default timeout. A context whose browser has
            # wedged (socket open, renderer dead) neither answers nor errors, so
            # an unbounded await here parked the caller - and, on the refresh
            # path, the process-wide _refresh_lock with it. A browser that
            # cannot open a tab inside the budget IS dead, which is exactly what
            # the except branch below already concludes.
            page = await asyncio.wait_for(
                self._context.new_page(), timeout=BROWSER_OP_TIMEOUT
            )
        except Exception as exc:
            self._mark_context_dead("%s: %s" % (type(exc).__name__, exc))
            raise BrowserUnavailableError(
                "Browser context is dead - a new tab cannot be opened. "
                "The watchdog must restart the browser (naukri_login may be "
                "needed afterwards)."
            ) from exc
        self._all_pages.append(page)
        self._touch(page)
        logger.info("PagePool: recovered tab (%d total)", len(self._all_pages))
        return page

    def _mark_context_dead(self, reason: str) -> None:
        """Latch the browser as unavailable when the context is proven gone.

        browser.available used to be assigned in only three places, none of
        which fire on out-of-band death, so every health probe kept reporting
        the browser present while it was a corpse. Opening the circuit here also
        stops the pool from hammering a dead context on every call.
        """
        self._circuit_state = "open"
        self._circuit_open_time = time.monotonic()
        self._circuit_failure_count = self._circuit_failure_threshold
        owner = globals().get("browser")
        if owner is not None and getattr(owner, "page_pool", None) is self:
            owner.available = False
        logger.error("PagePool: browser context is dead (%s) - marked unavailable", reason)

    @asynccontextmanager
    async def acquire(self, *, count_as_activity: bool = True,
                      allow_resume: bool = True):
        """Check out a page from the pool. Auto-returns on exit.

        Usage:
            async with page_pool.acquire() as page:
                await page.goto(url)
                result = await page.evaluate(...)

        THIS IS THE ONLY PLACE A SUSPENDED BROWSER COMES BACK. Every one of the
        call sites that needs a page reaches it, and nothing else does - which is
        what makes "the 30s liveness probes must not resurrect the browser"
        structural rather than a rule someone has to remember. The probes reach
        NaukriBrowser.liveness(), which answers from state and never touches a
        pool while suspended.

        ``count_as_activity=False`` checks a page out WITHOUT restarting the
        idle countdown. It exists for the one caller that is monitoring rather
        than working: liveness() round-tripping a live browser. If monitoring
        counted as activity, a probe every 30s against a 600s idle timeout would
        hold the context open forever and suspension could never fire.
        """
        # Resume BEFORE anything else - notably before the semaphore, so no
        # lease is held across a relaunch and this cannot self-deadlock.
        if self._suspended:
            if not allow_resume:
                # The monitoring path. Refusing here rather than relying on the
                # caller checking state first is what makes "probes never
                # resurrect an idle browser" true even if a suspend lands in the
                # microseconds between the caller's state read and this call.
                raise BrowserUnavailableError(
                    "Browser is suspended; this caller may not resume it."
                )
            if self._on_resume is None:
                raise BrowserUnavailableError(
                    "Browser is suspended and no resume hook is installed. "
                    "Call naukri_login to start a session."
                )
            await self._on_resume()
            if self._suspended or self._context is None:
                raise BrowserUnavailableError(
                    "Browser is suspended and could not be resumed. "
                    "Check naukri_health_check for watchdog state."
                )

        if count_as_activity:
            self._last_activity = time.monotonic()
        # Circuit breaker check — fast-fail when browser is known-dead
        if self._circuit_state == "open":
            elapsed = time.monotonic() - self._circuit_open_time
            if elapsed < self._circuit_cooldown:
                raise BrowserUnavailableError(
                    f"Browser unavailable (circuit open, {self._circuit_cooldown - elapsed:.0f}s until retry)"
                )
            self._circuit_state = "half_open"

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=POOL_CHECKOUT_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError("Page pool exhausted — all tabs busy for >%ds" % POOL_CHECKOUT_TIMEOUT)
        wait_ms = int((time.monotonic() - t0) * 1000)
        if wait_ms > self._max_wait_ms:
            self._max_wait_ms = wait_ms
        self._checkouts += 1
        page = None
        try:
            # Try to get an available page
            if not self._available.empty():
                page = self._available.get_nowait()
            elif len(self._all_pages) < self._max_pages:
                page = await self._create_page()
            else:
                # All pages in use, wait for one to return. POOL_CHECKOUT_TIMEOUT
                # bounds only the semaphore above; this queue wait had no bound
                # at all, so a page lost from the queue while still counted in
                # _all_pages left the caller waiting for a tab that would never
                # arrive. Spend whatever is left of the same budget, then fail.
                remaining = max(1.0, POOL_CHECKOUT_TIMEOUT - (time.monotonic() - t0))
                try:
                    page = await asyncio.wait_for(self._available.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        "Page pool exhausted — no tab returned within %ds"
                        % POOL_CHECKOUT_TIMEOUT
                    )

            # Verify the page is alive with a REAL round-trip to the browser.
            # See is_page_alive() for why the old "_ = page.url" could not fail.
            if not await is_page_alive(page):
                self._crashes += 1
                if self._crashes > self._MAX_CRASHES:
                    logger.error("Page pool unstable (>%d crashes). Consider restarting.", self._MAX_CRASHES)
                dead_page = page
                # Drop the reference BEFORE recovery so the finally block can
                # never put a dead page back into the available queue if
                # recovery raises.
                page = None
                page = await self._recover_page(dead_page)

            yield page
            # Success — reset circuit breaker
            self._circuit_failure_count = 0
            if self._circuit_state == "half_open":
                self._circuit_state = "closed"
        except BrowserUnavailableError:
            raise
        except Exception:
            self._circuit_failure_count += 1
            if self._circuit_failure_count >= self._circuit_failure_threshold:
                self._circuit_state = "open"
                self._circuit_open_time = time.monotonic()
                logger.warning("Circuit breaker OPEN after %d failures", self._circuit_failure_count)
            raise
        finally:
            # Count the checkout as completed EXACTLY once, whether or not a
            # usable page was produced. The old code skipped _returns when the
            # checkout failed before a page was assigned, so the
            # checkouts/returns gap grew monotonically on every failure and was
            # misread as a lease leak. The semaphore was never leaked -- only
            # the counters were, which made them useless as a diagnostic.
            self._returns += 1
            if page is not None:
                try:
                    # Stamp BEFORE re-queueing: the page becomes reap-eligible
                    # only from the moment it is back in the pool, and it must
                    # never land in the queue without a fresh timestamp.
                    self._touch(page)
                    await self._available.put(page)
                except Exception:
                    pass
            self._semaphore.release()

    async def reap_idle_pages(self) -> int:
        """Close pooled tabs that have sat unused past ``_idle_timeout``.

        Returns the number of tabs closed.

        THREE PROPERTIES MAKE THIS SAFE, and each one is load-bearing:

        1. A CHECKED-OUT PAGE CANNOT BE REAPED, structurally rather than by a
           lock. ``acquire()`` removes a page from ``_available`` for the whole
           duration of the ``async with`` block and only puts it back in its
           ``finally``. This method considers *only* what is in ``_available``,
           so a page an operation is holding is not a candidate at all. There is
           no window and no new lock to deadlock on.

        2. THE BOOKKEEPING IS ATOMIC. Draining the queue, choosing keepers,
           putting them back and removing the doomed from ``_all_pages`` all
           happen below with NO await between them, so the event loop cannot
           interleave a caller into a half-updated pool. Only the actual
           ``page.close()`` calls await - and by then the doomed pages are
           reachable from neither ``_available`` nor ``_all_pages``, so no
           caller can be handed one. This is why there is no second dead-page
           path: ``acquire()``'s existing ``is_page_alive`` -> ``_recover_page``
           recovery stays the only one, and it is not even reachable from here.

        3. IT NEVER TOUCHES BROWSER LIFECYCLE. This is what keeps it clear of
           browser_watchdog and of reauth's browser_restart stage, which both
           own stop/start. The reaper closes tabs and nothing else: it never
           closes the context, never flips ``browser.available``, never opens
           the circuit. The watchdog's own restart path goes through
           ``NaukriBrowser.stop()``, which cancels the reaper before tearing the
           pool down, so no orphan sweep can run against a dead pool.

        WHAT THE FLOOR IS AND IS NOT FOR. ``_min_pages`` newest pages are kept
        whatever their age. It is tempting to say the floor is what stops the
        30s liveness probes (browser_watchdog._probe and health.probes.browser,
        where two consecutive failures trigger an automatic restart) from
        failing. MEASURED, THAT IS NOT TRUE: with an aggressive reaper running
        flat out, the probe shape saw 0/400 failures at min_pages=1 AND 0/400 at
        min_pages=0, because ``acquire()`` creates a tab on demand whenever
        ``len(_all_pages) < _max_pages``. The probes are safe at any floor.
        What the floor actually buys is (a) an interactive caller never paying
        tab-creation latency after an idle stretch, and (b) no create/close
        churn - at floor 0 an aggressive reaper produced a visible "created new
        tab" / "closed 1 idle tab" cycle on every probe.

        Skipped entirely while the circuit is open: the browser is already
        known-dead, the watchdog owns recovery, and closing corpses adds
        nothing.
        """
        if self._circuit_state == "open":
            return 0

        now = time.monotonic()

        # ---- Phase 1: bookkeeping. STRICTLY SYNCHRONOUS - do not add an await. ----
        idle: list[Page] = []
        while True:
            try:
                idle.append(self._available.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not idle:
            return 0

        # Freshest first, so the floor keeps the most recently used tabs and the
        # stalest are the ones that go. Beyond tidiness this favours keeping a
        # tab that is on a real Naukri page over one sitting at about:blank,
        # which is what TokenManager.refresh_via_pool reloads.
        idle.sort(key=lambda p: self._idle_age(p, now))

        keep: list[Page] = []
        doomed: list[Page] = []
        for page in idle:
            if len(keep) < self._min_pages or self._idle_age(page, now) < self._idle_timeout:
                keep.append(page)
            else:
                doomed.append(page)

        for page in keep:
            self._available.put_nowait(page)   # unbounded queue: cannot block
        for page in doomed:
            if page in self._all_pages:
                self._all_pages.remove(page)
            self._forget(page)
        # ---- end of the atomic section ----

        if not doomed:
            return 0

        # ---- Phase 2: closing. The doomed pages are already unreachable. ----
        for page in doomed:
            try:
                await page.close()
            except Exception as e:
                # A tab that cannot be closed is already gone; it has been
                # dropped from the pool either way.
                logger.debug("Idle tab close failed (already gone?): %s", e)

        self._idle_closed += len(doomed)
        logger.info(
            "PagePool: closed %d idle tab(s) after >%.0fs unused (%d kept, %d total)",
            len(doomed), self._idle_timeout, len(keep), len(self._all_pages),
        )
        return len(doomed)

    async def close_all(self):
        """Close all pages in the pool."""
        for page in self._all_pages:
            try:
                await page.close()
            except Exception:
                pass
        self._all_pages.clear()
        self._last_used.clear()
        while not self._available.empty():
            try:
                self._available.get_nowait()
            except asyncio.QueueEmpty:
                break

    def reset_circuit(self):
        """Called by watchdog after successful restart."""
        self._circuit_state = "closed"
        self._circuit_failure_count = 0

    @property
    def circuit_state(self) -> str:
        return self._circuit_state

    def get_stats(self) -> dict:
        """Return observability counters for the page pool."""
        return {
            "max_tabs": self._max_pages,
            "checkouts": self._checkouts,
            "returns": self._returns,
            "crashes": self._crashes,
            "max_wait_ms": self._max_wait_ms,
            "open_tabs": len(self._all_pages),
            "idle_tabs": self._available.qsize(),
            "idle_closed": self._idle_closed,
            "idle_timeout_s": self._idle_timeout,
            "min_tabs": self._min_pages,
            "suspended": self._suspended,
            "idle_seconds": round(self.idle_seconds(), 1),
            "outstanding_leases": self.outstanding_leases(),
        }


# ---------------------------------------------------------------------------
# Page-level helper functions (replace old browser.goto, browser.text, etc.)
# ---------------------------------------------------------------------------

async def page_goto(page: Page, url: str, wait: str = "domcontentloaded") -> None:
    """Navigate a page with fallback to 'commit' wait strategy."""
    try:
        await page.goto(url, wait_until=wait, timeout=NAV_TIMEOUT)
    except TargetClosedError:
        # Let PagePool handle recovery on the next acquire(). That path is now
        # reachable: acquire() round-trips to the browser instead of reading a
        # cached string, so a dead target is actually detected.
        raise
    except Exception:
        try:
            await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT)
        except TargetClosedError:
            raise


async def page_evaluate_safe(page: Page, expression, arg=None, timeout=15):
    """Evaluate JS on page with timeout protection.

    Prevents JS deadlocks from freezing the entire page pool.
    """
    try:
        if arg is not None:
            return await asyncio.wait_for(page.evaluate(expression, arg), timeout=timeout)
        return await asyncio.wait_for(page.evaluate(expression), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("page.evaluate timed out after %ds", timeout)
        return None


async def browser_retry(async_fn, max_retries=2, description="browser operation"):
    """Retry a browser operation with exponential backoff.

    Args:
        async_fn: Async callable that performs the browser operation
        max_retries: Maximum retry attempts (default 2, total 3 attempts)
        description: Operation name for logging

    Returns:
        Result from async_fn on success

    Raises:
        Last exception if all retries fail
    """
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return await async_fn()
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = (2 ** attempt) * 0.5  # 0.5s, 1s
                logger.warning(
                    "%s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                    description, attempt + 1, max_retries + 1,
                    type(e).__name__, delay
                )
                await asyncio.sleep(delay)
            else:
                logger.error("%s failed after %d attempts: %s", description, max_retries + 1, e)
    raise last_error


async def page_text(page: Page, selector: str) -> Optional[str]:
    """Get text content of an element on the page."""
    el = await page.query_selector(selector)
    if not el:
        return None
    content = await el.text_content()
    return content.strip() if content else None


async def page_exists(page: Page, selector: str) -> bool:
    """Check if an element exists on the page."""
    return await page.query_selector(selector) is not None


async def page_safe_fill(page: Page, selector: str, value: str, delay: int = 30):
    """Wait for element, click, clear, and type value."""
    el = await page.wait_for_selector(selector, timeout=ELEMENT_TIMEOUT)
    await el.click()
    await el.fill("")
    await el.type(value, delay=delay)


async def page_intercept_json(
    page: Page,
    url: str,
    url_pattern: str | None = None,
    timeout: float = 10,
    wait: str = "domcontentloaded",
) -> dict | None:
    """Navigate to a URL and capture the first matching JSON API response.

    Args:
        page: Playwright page (from page_pool.acquire())
        url: Page URL to navigate to
        url_pattern: Substring to match in response URL (None = first JSON response)
        timeout: Seconds to wait for the response (default 10)
        wait: Navigation wait strategy (default "domcontentloaded")

    Returns:
        Parsed JSON dict, or None if no matching response within timeout.
    """
    captured = {}
    event = asyncio.Event()

    async def on_response(response):
        if response.status == 200 and (url_pattern is None or url_pattern in response.url):
            try:
                captured["data"] = await response.json()
            except Exception:
                logger.warning("Failed to parse JSON from %s", response.url)
            event.set()

    page.on("response", on_response)
    try:
        await page_goto(page, url, wait=wait)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Response capture timed out after %ss for pattern: %s", timeout, url_pattern)
    finally:
        page.remove_listener("response", on_response)

    return captured.get("data")


# ---------------------------------------------------------------------------
# NaukriBrowser — orchestrator
# ---------------------------------------------------------------------------

class NaukriBrowser:
    """Playwright browser with multi-tab page pool and cached token manager."""

    def __init__(self):
        self.pw = None
        self.context: Optional[BrowserContext] = None
        self.token_manager = TokenManager()
        self.page_pool: Union[PagePool, _NoPagePool] = _NO_POOL
        self.available = False
        self._holds_profile_lock = False
        self._reaper_task: Optional[asyncio.Task] = None
        # Serializes suspend, resume and the liveness state read, so no caller
        # can observe a half-torn-down browser and mistake it for a crash.
        self._suspend_lock = asyncio.Lock()
        self._suspended = False
        self._suspends = 0
        self._resumes = 0

    async def start(self):
        # Cross-process guard: refuse to launch a SECOND instance against the
        # same persistent Chrome profile. Two instances on one user-data-dir
        # corrupt the profile — the highest-cost failure, since the profile is
        # the anti-detection moat. Re-entrant for our own PID (watchdog
        # restart) and reclaims stale locks from crashed instances.
        # ProfileLockedError propagates so the caller knows a live instance
        # already owns the profile — we do NOT touch it in that case.
        profile_lock.acquire()
        self._holds_profile_lock = True
        try:
            await self._open_context()

            logger.info("Browser started (pool: %d max tabs), token: %s",
                         MAX_TABS, "found" if self.token_manager._token else "none")

            # Validate session
            if self.token_manager._token:
                try:
                    from naukri_server.api import api_get
                    await asyncio.wait_for(
                        api_get(
                            PROFILE_API,
                            {"expand_level": "1"},
                        ),
                        timeout=SESSION_VALIDATE_TIMEOUT,
                    )
                    logger.info("Session validated — token is active")
                except asyncio.TimeoutError:
                    logger.warning("Session validation timed out — will validate lazily on first tool call")
                except Exception as e:
                    logger.warning("Token found but invalid: %s. Call naukri_login.", e)
                    self.token_manager.invalidate()

            self.available = True
            self._suspended = False
            self._start_reaper()
        except Exception as e:
            logger.error("Browser startup failed: %s. REST-only mode active.", e)
            self.available = False
            # Clean up partial initialization
            try:
                if self.page_pool:
                    await self.page_pool.close_all()
                if self.context:
                    await self.context.close()
                if self.pw:
                    await self.pw.stop()
            except Exception:
                pass
            self.page_pool = _NO_POOL
            self.context = None
            self.pw = None
            # Startup failed — we never actually held a live persistent context,
            # so release the profile lock to avoid a self-inflicted false
            # "already in use" on the next start attempt (e.g. watchdog retry).
            if self._holds_profile_lock:
                profile_lock.release()
                self._holds_profile_lock = False

    async def stop(self):
        from naukri_server.api import close_api_session

        # Mark unavailable FIRST: from here on the browser is being torn down,
        # and a probe that runs mid-teardown must not report it healthy.
        self.available = False

        # Cancel the reaper BEFORE close_all: it must not be mid-sweep, holding
        # pages it drained out of the queue, while the pool is being torn down.
        # This also makes the watchdog restart path (stop -> start) and reauth's
        # browser_restart stage clean - each start() installs a fresh reaper
        # bound to the new pool, and no orphan task survives pointing at the old
        # one.
        await self._stop_reaper()

        # Every teardown step is individually guarded. Stopping a browser that
        # already died is the NORMAL case on the watchdog restart path, and a
        # raise from any one step used to skip the profile-lock release below,
        # which is the one piece of state that outlives the process.
        for label, step in (
            ("close_api_session", close_api_session()),
            ("page_pool.close_all", self.page_pool.close_all() if self.page_pool else None),
            ("context.close", self.context.close() if self.context else None),
            ("playwright.stop", self.pw.stop() if self.pw else None),
        ):
            if step is None:
                continue
            try:
                await step
            except Exception as e:
                logger.debug("Browser stop step %s failed (expected if already dead): %s", label, e)

        self.page_pool = _NO_POOL
        self.context = None
        self.pw = None
        # A full stop is NOT a suspend: the pool is gone and the profile lock is
        # about to be released, so nothing here should read as "idle but ready".
        self._suspended = False

        # Release the cross-process profile lock so another instance (or a
        # watchdog restart in this process) can re-acquire it cleanly.
        if self._holds_profile_lock:
            try:
                profile_lock.release()
            except Exception as e:
                logger.warning("Profile lock release failed: %s", e)
            self._holds_profile_lock = False
        logger.info("Browser stopped")

    # -- context lifecycle: start / suspend / resume -------------------------

    async def _open_context(self) -> None:
        """Launch Playwright and the persistent context, and bind the pool to it.

        Shared by ``start()`` and ``_resume()`` so a resumed browser is
        configured identically to a freshly started one - notably
        ``headless=False``, which is deliberate (sign-in must be visible, and
        headless is more detectable). The fix for the idle browser is fewer live
        tabs and no idle process, never an invisible one.

        On RESUME the existing PagePool object is REUSED rather than replaced.
        Two health probes (pool.utilization and the CRITICAL pool.crash_rate)
        read counters straight off ``browser.page_pool``, and every other caller
        guards with ``if browser.page_pool``. Keeping one pool object for the
        life of the process means an idle period neither blanks those probes nor
        resets the crash history.
        """
        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--remote-debugging-port={CDP_PORT}",
            ],
        )

        # Initialize token manager
        self.token_manager.bind(self.context)
        self.token_manager._resume_hook = self._resume
        await self.token_manager.extract()

        # Initialize page pool with first page
        first_page = self.context.pages[0] if self.context.pages else await self.context.new_page()

        if isinstance(self.page_pool, PagePool):
            self.page_pool.rebind_context(self.context)
        else:
            self.page_pool = PagePool(self.context, max_pages=MAX_TABS)
        self.page_pool._on_resume = self._resume
        await self.page_pool.initialize(first_page)

    @property
    def is_suspended(self) -> bool:
        """True when the browser was closed ON PURPOSE because it was idle.

        Distinct from "down". A suspended browser has no Chrome process and
        holds no context - and is perfectly healthy: the session lives in
        chrome-profile/ on disk, and the next call that needs a page brings it
        back. Down means it should be running and is not.
        """
        return self._suspended

    async def _suspend(self) -> None:
        """Close the context because nothing has used it. Caller holds the lock.

        Deliberately NOT a full stop(). The profile lock is KEPT (we still own
        the profile, we are simply not running Chrome against it, and holding it
        means our own resume cannot lose a race to another instance). The
        aiohttp API session is KEPT, so REST calls keep working off the cached
        token while the browser is away. The PagePool object is KEPT so the
        health probes that read it keep answering.

        The reaper task is left running on purpose: this method is called FROM
        that task, so cancelling it here would cancel its own caller.
        """
        t0 = time.monotonic()
        idle_for = self.page_pool.idle_seconds() if isinstance(self.page_pool, PagePool) else 0.0

        # available goes False first: from here the browser cannot serve a page
        # until it is resumed. _suspended is set only at the END, once the
        # teardown is real. liveness() reads both under the same lock, so no
        # caller can observe the half-torn-down middle and call it a crash.
        self.available = False

        for label, step in (
            ("pool.park", self.page_pool.park() if isinstance(self.page_pool, PagePool) else None),
            ("context.close", self.context.close() if self.context else None),
            ("playwright.stop", self.pw.stop() if self.pw else None),
        ):
            if step is None:
                continue
            try:
                await step
            except Exception as e:
                logger.debug("Browser suspend step %s failed: %s", label, e)

        self.context = None
        self.pw = None
        self._suspended = True
        self._suspends += 1
        logger.info(
            "Browser suspended after %.0fs idle (teardown %.2fs) - no Chrome process; "
            "session is on disk and the next page checkout resumes it",
            idle_for, time.monotonic() - t0,
        )

    async def _resume(self) -> None:
        """Bring a suspended browser back. Idempotent and safe to call
        concurrently - the losers of the lock find the work already done.

        Reached ONLY from PagePool.acquire() and from the token layer's renewal
        path, which is equally real work. Monitoring never gets here.
        """
        async with self._suspend_lock:
            if not self._suspended:
                return
            t0 = time.monotonic()
            try:
                await self._open_context()
            except Exception as e:
                # A browser that cannot come back is NOT healthy, and leaving it
                # flagged suspended would report it as healthy forever. Demote
                # to DOWN so the very next probe says unhealthy and the watchdog
                # takes over with a real restart.
                self._suspended = False
                self.available = False
                logger.error(
                    "Browser resume FAILED (%s) - leaving the browser DOWN so the "
                    "watchdog restarts it", e,
                )
                raise BrowserUnavailableError("Browser resume failed: %s" % e) from e
            self._suspended = False
            self.available = True
            self._resumes += 1
            self._start_reaper()
            logger.info("Browser resumed from suspend in %.2fs", time.monotonic() - t0)

    async def liveness(self) -> tuple:
        """Authoritative browser state for MONITORING. Returns (state, message).

        Single source of truth for browser_watchdog._probe and the
        browser.liveness health probe, so the two can never disagree about
        whether an idle browser is a crash.

        IT NEVER RESUMES. The suspended branch touches no pool at all, and the
        running branch checks out with allow_resume=False, so even a suspend
        landing between the state read and the checkout cannot turn a probe into
        a relaunch. Without that, the 30s probes would resurrect the browser
        forever and it could never stay down.

        HOW SUSPENDED IS TOLD APART FROM CRASHED - the whole risk of this
        feature, since suspended is reported as healthy:
          * ``_suspended`` is set ONLY by _suspend(), only under _suspend_lock,
            only with zero outstanding page leases, and only after the teardown
            has actually run.
          * It is then NOT TAKEN ON TRUST. A genuine suspension has no context,
            no Playwright and a parked pool; anything claiming to be suspended
            while still holding one of those is a broken teardown and is
            reported DOWN. That is the control against a suspend state
            swallowing a real crash.
          * A failed resume clears the flag and leaves available=False, so it
            reports DOWN on the very next probe.
          * A crash while RUNNING is untouched by any of this: the round-trip
            still runs and still answers DOWN.
        """
        async with self._suspend_lock:
            if self._suspended:
                if self.context is not None or self.pw is not None:
                    return (BROWSER_DOWN,
                            "Suspend state is inconsistent: a context is still open. "
                            "Treating as a crash, not as idle.")
                if getattr(self.page_pool, "is_suspended", False) is not True:
                    return (BROWSER_DOWN,
                            "Suspend state is inconsistent: the page pool is not parked. "
                            "Treating as a crash, not as idle.")
                return (BROWSER_SUSPENDED,
                        "Browser suspended (idle) - no Chrome process; session is on "
                        "disk and the next page checkout resumes it")
            if not self.available:
                return (BROWSER_DOWN, "Browser not available")
            if not self.page_pool:
                return (BROWSER_DOWN, "Browser has no page pool")

        try:
            async with self.page_pool.acquire(
                count_as_activity=False, allow_resume=False
            ) as page:
                if await is_page_alive(page):
                    return (BROWSER_RUNNING, "Browser alive")
                return (BROWSER_DOWN,
                        "Page did not answer a liveness round-trip (target closed or wedged)")
        except Exception as e:
            if self._suspended:
                # Suspended between the state read and the checkout. Benign.
                return (BROWSER_SUSPENDED, "Browser suspended (idle) during the probe")
            return (BROWSER_DOWN, "Probe failed: %s" % e)

    async def _maybe_suspend(self) -> bool:
        """Suspend when the pool has been quiet long enough. True if it did."""
        if CONTEXT_IDLE_TIMEOUT <= 0:
            return False
        pool = self.page_pool
        if not isinstance(pool, PagePool) or pool.is_suspended:
            return False
        if not self.available or self._suspended:
            return False
        if pool.outstanding_leases() > 0:
            return False
        if pool.idle_seconds() < CONTEXT_IDLE_TIMEOUT:
            return False
        async with self._suspend_lock:
            # Re-check everything under the lock: a caller may have checked a
            # page out while we waited for it, and suspending with a lease
            # outstanding would close a page from under a live operation.
            if self._suspended or not self.available:
                return False
            if pool.is_suspended or pool.outstanding_leases() > 0:
                return False
            if pool.idle_seconds() < CONTEXT_IDLE_TIMEOUT:
                return False
            await self._suspend()
            return True

    def _start_reaper(self) -> None:
        """Install the background idle-tab reaper for the CURRENT pool."""
        if PAGE_REAPER_INTERVAL <= 0:
            return
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        try:
            self._reaper_task = asyncio.create_task(self._reaper_loop())
        except RuntimeError:
            # No running loop (import-time / sync test context). The pool still
            # works; it just does not self-trim.
            self._reaper_task = None

    async def _stop_reaper(self) -> None:
        """Cancel and await the reaper. Safe to call when it was never started."""
        task, self._reaper_task = self._reaper_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _reaper_loop(self) -> None:
        """Periodically close pooled tabs nobody has touched.

        Two jobs, in order: trim idle tabs, then suspend the whole context once
        the pool has been quiet for CONTEXT_IDLE_TIMEOUT.

        It never opens the circuit and never restarts anything - suspension is
        not recovery. The watchdog and reauth still own CRASH recovery; this
        owns only the idle path, and the two are kept apart by liveness()
        reporting a suspended browser as healthy so no restart is ever
        triggered for it. One cycle failing must never kill the loop.
        """
        while True:
            try:
                await asyncio.sleep(PAGE_REAPER_INTERVAL)
                pool = self.page_pool
                if not pool or not self.available or self._suspended:
                    continue
                await pool.reap_idle_pages()
                # Then, if the whole pool has gone quiet, close the context too.
                # Ordering matters: tabs trim at PAGE_IDLE_TIMEOUT, the context
                # goes at the longer CONTEXT_IDLE_TIMEOUT.
                await self._maybe_suspend()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Idle tab reaper cycle failed (continuing): %s", e)

    async def get_profile_name(self) -> str:
        """Get profile name via profile API."""
        from naukri_server.api import api_get
        try:
            data = await api_get(
                PROFILE_API,
                {"expand_level": "4"},
            )
            profiles = data.get("profile") or []
            name = profiles[0].get("name") if profiles and isinstance(profiles[0], dict) else None
            if name:
                logger.info("Profile name from API: %s", name)
                return name
        except Exception as e:
            logger.warning("Profile name API failed: %s: %s", type(e).__name__, e)
        return "unknown"


browser = NaukriBrowser()
