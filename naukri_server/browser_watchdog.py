"""Browser watchdog — background health monitor with auto-restart.

Runs a periodic health probe. On failure, emits BrowserCrashed event
and attempts automatic restart. On recovery, emits BrowserRecovered.
Tools are completely unaware of this — it's transparent infrastructure.
"""

import asyncio
import logging
import time
from naukri_server._compat import timeout as _timeout
from typing import Optional

logger = logging.getLogger(__name__)


class BrowserWatchdog:
    """Background monitor that detects browser death and triggers recovery."""

    # How long the browser must stay continuously healthy before the restart
    # budget is handed back. Without this the watchdog spends its 3 restarts
    # over the lifetime of the process and then supervises nothing -- which is
    # the same silent outage in a slower form.
    _RESTART_BUDGET_RESET_SECONDS = 600.0

    def __init__(self, check_interval: float = 30.0, max_restart_attempts: int = 3):
        self._check_interval = check_interval
        self._max_restarts = max_restart_attempts
        self._restart_count = 0
        self._crash_count = 0
        self._consecutive_failures = 0
        self._task: Optional[asyncio.Task] = None
        self._last_healthy: float = time.monotonic()
        self._last_restart: float = 0.0
        self._running = False

    async def start(self):
        """Start the background health check loop."""
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("BrowserWatchdog started (interval=%ss, max_restarts=%d)",
                     self._check_interval, self._max_restarts)

    async def stop(self):
        """Cancel the background task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("BrowserWatchdog stopped (restarts=%d)", self._restart_count)

    async def _monitor_loop(self):
        """Periodically probe browser health."""
        while self._running:
            try:
                await asyncio.sleep(self._check_interval)
                if not self._running:
                    break
                healthy = await self._probe()
                if healthy:
                    self._consecutive_failures = 0
                    self._last_healthy = time.monotonic()
                    self._maybe_restore_restart_budget()
                else:
                    self._consecutive_failures += 1
                    await self._handle_failure()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Watchdog loop error: %s", e)

    async def _probe(self) -> bool:
        """Liveness check - acquire a page and make it ANSWER.

        This used to end in `_ = page.url`, a cached attribute read that cannot
        raise on a dead target (see browser.is_page_alive). The watchdog
        therefore reported healthy against a corpse: 7 BrowserCrashed events on
        2026-08-20 produced 0 restarts. The probe now round-trips to the
        browser, so it is capable of returning the failing verdict.
        """
        try:
            from naukri_server.browser import browser, is_page_alive
            if not browser or not browser.available:
                return False
            # Try to acquire a page with a short timeout
            async with _timeout(10):
                async with browser.page_pool.acquire() as page:
                    return await is_page_alive(page)
        except Exception as e:
            logger.debug("Browser probe failed: %s", e)
            return False

    def _maybe_restore_restart_budget(self):
        """Hand the restart budget back after a sustained healthy stretch."""
        if self._restart_count == 0:
            return
        if time.monotonic() - self._last_restart < self._RESTART_BUDGET_RESET_SECONDS:
            return
        logger.info(
            "Browser healthy for %.0fs since last restart - restoring restart budget "
            "(was %d/%d used)",
            self._RESTART_BUDGET_RESET_SECONDS, self._restart_count, self._max_restarts,
        )
        self._restart_count = 0

    async def _handle_failure(self):
        """Emit crash event, attempt restart if under limit."""
        from naukri_server.events import event_bus, BrowserCrashed

        # crash_count used to be `self._restart_count`, which is 0 until a
        # restart SUCCEEDS -- so every crash event reported 0 crashes and any
        # circuit breaker keyed on it could never trip. Count crashes.
        self._crash_count += 1

        await event_bus.emit(BrowserCrashed(
            reason=f"Health probe failed ({self._consecutive_failures} consecutive)",
            crash_count=self._crash_count,
            consecutive_failures=self._consecutive_failures,
        ))

        if self._consecutive_failures >= 2:
            if self._restart_count < self._max_restarts:
                await self._attempt_restart()
            else:
                # Loud, because from here on nothing is supervising the browser
                # until the budget is restored or the process is restarted.
                logger.error(
                    "Browser is down and the restart budget is exhausted (%d/%d used, "
                    "%d consecutive probe failures). No further automatic restarts "
                    "until the browser is healthy again for %.0fs or the server is "
                    "restarted.",
                    self._restart_count, self._max_restarts,
                    self._consecutive_failures, self._RESTART_BUDGET_RESET_SECONDS,
                )

    async def _attempt_restart(self):
        """Stop and restart the browser. Emit recovery event on success."""
        from naukri_server.browser import browser
        from naukri_server.events import event_bus, BrowserRecovered

        t0 = time.monotonic()
        logger.warning("Attempting browser restart (#%d)...", self._restart_count + 1)

        try:
            await browser.stop()
        except Exception as e:
            logger.debug("Browser stop error (expected): %s", e)

        try:
            await browser.start()
            self._restart_count += 1
            self._last_restart = time.monotonic()
            self._consecutive_failures = 0
            downtime = time.monotonic() - t0

            # Reset circuit breaker after successful restart
            if browser.page_pool:
                browser.page_pool.reset_circuit()

            await event_bus.emit(BrowserRecovered(
                downtime_seconds=round(downtime, 1),
                restart_count=self._restart_count,
            ))
            logger.info("Browser restarted successfully (downtime=%.1fs)", downtime)
        except Exception as e:
            logger.error("Browser restart failed: %s", e)

    @property
    def stats(self) -> dict:
        """Return watchdog stats for health check."""
        return {
            "restart_count": self._restart_count,
            "crash_count": self._crash_count,
            "consecutive_failures": self._consecutive_failures,
            "last_healthy_seconds_ago": round(time.monotonic() - self._last_healthy, 1),
            "restart_budget_remaining": max(0, self._max_restarts - self._restart_count),
            "running": self._running,
        }


# Module-level singleton (initialized in lifespan)
watchdog: Optional[BrowserWatchdog] = None
