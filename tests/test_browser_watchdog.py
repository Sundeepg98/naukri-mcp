"""Unit tests for naukri_server.browser_watchdog — BrowserWatchdog logic.

All tests are PURE: no real browser. We mock the browser singleton + event_bus.
"""

from contextlib import asynccontextmanager
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_server.browser_watchdog import BrowserWatchdog


# ---------------------------------------------------------------------------
# 1. Construction + stats
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_default_construction(self):
        w = BrowserWatchdog()
        assert w._check_interval == 30.0
        assert w._max_restarts == 3
        assert w._restart_count == 0
        assert w._consecutive_failures == 0
        assert not w._running
        assert w._task is None

    def test_custom_construction(self):
        w = BrowserWatchdog(check_interval=5.0, max_restart_attempts=10)
        assert w._check_interval == 5.0
        assert w._max_restarts == 10

    def test_stats_initial(self):
        w = BrowserWatchdog()
        s = w.stats
        assert s["restart_count"] == 0
        assert s["consecutive_failures"] == 0
        assert s["running"] is False
        assert isinstance(s["last_healthy_seconds_ago"], float)


# ---------------------------------------------------------------------------
# 2. _probe — liveness check
# ---------------------------------------------------------------------------

class TestProbe:
    @pytest.mark.asyncio
    async def test_probe_returns_false_when_browser_unavailable(self):
        """If browser singleton has available=False, probe returns False."""
        w = BrowserWatchdog()
        with patch("naukri_server.browser.browser") as mock_browser:
            mock_browser.available = False
            result = await w._probe()
        assert result is False

    @pytest.mark.asyncio
    async def test_probe_returns_true_when_page_acquires(self):
        """Successful page_pool.acquire returns True."""
        w = BrowserWatchdog()
        mock_page = MagicMock()
        mock_page.url = "https://www.naukri.com"

        @asynccontextmanager
        async def fake_acquire():
            yield mock_page

        with patch("naukri_server.browser.browser") as mock_browser:
            mock_browser.available = True
            mock_browser.page_pool.acquire = fake_acquire
            result = await w._probe()
        assert result is True

    @pytest.mark.asyncio
    async def test_probe_returns_false_on_exception(self):
        """Any exception (acquire fail, etc.) returns False, not raise."""
        w = BrowserWatchdog()

        @asynccontextmanager
        async def bad_acquire():
            raise RuntimeError("page pool dead")
            yield  # unreachable

        with patch("naukri_server.browser.browser") as mock_browser:
            mock_browser.available = True
            mock_browser.page_pool.acquire = bad_acquire
            result = await w._probe()
        assert result is False


# ---------------------------------------------------------------------------
# 3. _handle_failure — emits event, conditionally restarts
# ---------------------------------------------------------------------------

class TestHandleFailure:
    @pytest.mark.asyncio
    async def test_handle_failure_emits_browser_crashed(self):
        """Every failure emits BrowserCrashed regardless of restart decision."""
        w = BrowserWatchdog()
        w._consecutive_failures = 1  # below restart threshold (2)
        with patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            await w._handle_failure()
            mock_bus.emit.assert_awaited_once()
            event = mock_bus.emit.await_args[0][0]
            # BrowserCrashed shape
            assert hasattr(event, "consecutive_failures")
            assert event.consecutive_failures == 1
            assert event.crash_count == 0  # restart_count

    @pytest.mark.asyncio
    async def test_handle_failure_below_threshold_no_restart(self):
        """1 consecutive failure does not trigger restart (threshold is 2)."""
        w = BrowserWatchdog()
        w._consecutive_failures = 1
        with patch("naukri_server.events.event_bus") as mock_bus, \
             patch.object(w, "_attempt_restart", new_callable=AsyncMock) as mock_restart:
            mock_bus.emit = AsyncMock()
            await w._handle_failure()
            mock_restart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_failure_at_threshold_restarts(self):
        """2+ consecutive failures triggers _attempt_restart (when under cap)."""
        w = BrowserWatchdog(max_restart_attempts=3)
        w._consecutive_failures = 2
        w._restart_count = 0
        with patch("naukri_server.events.event_bus") as mock_bus, \
             patch.object(w, "_attempt_restart", new_callable=AsyncMock) as mock_restart:
            mock_bus.emit = AsyncMock()
            await w._handle_failure()
            mock_restart.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_failure_at_max_restarts_no_more_restart(self):
        """When restart_count == max_restarts, no further restart attempts."""
        w = BrowserWatchdog(max_restart_attempts=3)
        w._consecutive_failures = 5  # well above threshold
        w._restart_count = 3  # already at cap
        with patch("naukri_server.events.event_bus") as mock_bus, \
             patch.object(w, "_attempt_restart", new_callable=AsyncMock) as mock_restart:
            mock_bus.emit = AsyncMock()
            await w._handle_failure()
            mock_restart.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. _attempt_restart — stop/start cycle + recovery event
# ---------------------------------------------------------------------------

class TestAttemptRestart:
    @pytest.mark.asyncio
    async def test_successful_restart_emits_recovered(self):
        """Successful start() emits BrowserRecovered with downtime."""
        w = BrowserWatchdog()
        w._consecutive_failures = 5
        with patch("naukri_server.browser.browser") as mock_browser, \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_browser.stop = AsyncMock()
            mock_browser.start = AsyncMock()
            mock_browser.page_pool = MagicMock()
            mock_browser.page_pool.reset_circuit = MagicMock()
            mock_bus.emit = AsyncMock()

            await w._attempt_restart()

            mock_browser.stop.assert_awaited_once()
            mock_browser.start.assert_awaited_once()
            mock_browser.page_pool.reset_circuit.assert_called_once()
            assert w._restart_count == 1
            assert w._consecutive_failures == 0
            mock_bus.emit.assert_awaited_once()
            event = mock_bus.emit.await_args[0][0]
            assert hasattr(event, "downtime_seconds")
            assert event.restart_count == 1

    @pytest.mark.asyncio
    async def test_restart_failure_does_not_increment_count(self):
        """If start() raises, restart_count stays unchanged, no recovery event."""
        w = BrowserWatchdog()
        with patch("naukri_server.browser.browser") as mock_browser, \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_browser.stop = AsyncMock()
            mock_browser.start = AsyncMock(side_effect=RuntimeError("start failed"))
            mock_bus.emit = AsyncMock()

            await w._attempt_restart()

            assert w._restart_count == 0
            mock_bus.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_failure_during_restart_is_swallowed(self):
        """stop() exception is logged-and-ignored; restart still proceeds."""
        w = BrowserWatchdog()
        with patch("naukri_server.browser.browser") as mock_browser, \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_browser.stop = AsyncMock(side_effect=RuntimeError("already dead"))
            mock_browser.start = AsyncMock()
            mock_browser.page_pool = MagicMock()
            mock_browser.page_pool.reset_circuit = MagicMock()
            mock_bus.emit = AsyncMock()

            await w._attempt_restart()

            mock_browser.start.assert_awaited_once()
            assert w._restart_count == 1
