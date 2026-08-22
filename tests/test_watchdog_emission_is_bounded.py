"""The watchdog emitted a BrowserCrashed notification on EVERY probe cycle.

Measured on his account 2026-08-20..22. One expired session produced **2342**
BrowserCrashed notifications over two days -- ~4/min while live -- with
`restart_budget_remaining` pinned at 0 and only 4 actual restarts. That was
**93% of his entire notification table** (2342 of 2480 unread), and his daily
brief's top recommended action had become "Review 10 high-priority
notification(s): Browser crashed (1124 failures)". The signal he actually needed
was underneath it.

The emit sat ABOVE the budget check and was unconditional, so exhausting the
restart budget stopped the restarts and nothing else. The crash was only the
trigger -- the emission is the defect, and it stands on its own: the next dead
session would do exactly this again.

Bound is now TWO events per outage: one when the browser first goes down, one
when the watchdog gives up. `crash_count` still increments every cycle, so
nothing observable is lost -- only the repetition.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server.browser_watchdog import BrowserWatchdog


async def _run_outage(wd, cycles):
    """Drive `cycles` consecutive failed probes, collecting emitted events."""
    emitted = []

    async def capture(event):
        emitted.append(event)

    with patch("naukri_server.events.event_bus.emit", new=AsyncMock(side_effect=capture)), \
         patch.object(wd, "_attempt_restart", new=AsyncMock()) as restart:
        for _ in range(cycles):
            wd._consecutive_failures += 1
            await wd._handle_failure()
    return emitted, restart


@pytest.mark.asyncio
async def test_a_long_outage_does_not_produce_one_notification_per_cycle():
    """THE regression. 200 failed probes used to bank 200 notifications."""
    wd = BrowserWatchdog()
    wd._restart_count = wd._max_restarts        # budget already exhausted
    emitted, restart = await _run_outage(wd, cycles=200)

    assert len(emitted) <= 2, (
        "%d notifications for one outage -- this is the storm" % len(emitted))
    # and the counter is still exact, so nothing measurable was lost
    assert wd._crash_count == 200
    restart.assert_not_called(), "budget was exhausted; nothing should restart"


@pytest.mark.asyncio
async def test_the_first_failure_is_always_announced():
    """Silencing the storm must not silence the ALARM."""
    wd = BrowserWatchdog()
    emitted, _ = await _run_outage(wd, cycles=1)

    assert len(emitted) == 1
    assert "Health probe failed" in emitted[0].reason


@pytest.mark.asyncio
async def test_giving_up_is_announced_exactly_once():
    """Budget exhaustion is real news -- from then on nothing supervises the
    browser -- so it is worth one event, and only one."""
    wd = BrowserWatchdog()
    emitted, _ = await _run_outage(wd, cycles=3)          # 3 failures, budget intact
    assert not any("budget exhausted" in e.reason for e in emitted)

    wd._restart_count = wd._max_restarts
    more, _ = await _run_outage(wd, cycles=50)
    exhausted = [e for e in more if "budget exhausted" in e.reason]
    assert len(exhausted) == 1, "expected exactly one give-up notice, got %d" % len(exhausted)


@pytest.mark.asyncio
async def test_a_second_outage_after_recovery_is_announced_again():
    """CONTROL: the suppression must not be permanent. A watchdog that goes
    quiet forever is a worse failure than the storm."""
    wd = BrowserWatchdog()
    wd._restart_count = wd._max_restarts
    first, _ = await _run_outage(wd, cycles=20)
    assert first, "first outage must be announced"

    # Browser comes back: the monitor loop zeroes the failure count, and a
    # sustained healthy stretch restores the budget.
    wd._consecutive_failures = 0
    # "The last restart was longer ago than the budget window" -- expressed
    # RELATIVE TO THE CLOCK, never as the literal 0.0.
    #
    # This was `wd._last_restart = 0.0`, and it encoded a MACHINE UPTIME
    # assumption nobody meant to make. `_maybe_restore_restart_budget` compares
    # `time.monotonic() - self._last_restart` against 600s, and monotonic()
    # counts from SYSTEM BOOT -- so 0.0 means "long ago" only on a box that has
    # already been up ten minutes. On this dev box (up ~95h) it passed; on a
    # GitHub Actions container, seconds old, `monotonic() - 0.0` is under 600
    # and the restore correctly declines, leaving _restart_count at 3.
    # `assert 3 == 0`, red on ubuntu 3.10/3.11/3.12 and green here.
    #
    # Not a Windows-vs-Linux difference: a freshly booted Windows box fails it
    # too. The PRODUCTION code is correct -- `_restart_count` and
    # `_last_restart` are assigned together in `_attempt_restart`, so a nonzero
    # count always carries a real timestamp and the 0.0 initialiser is
    # unreachable with a nonzero count.
    wd._last_restart = time.monotonic() - wd._RESTART_BUDGET_RESET_SECONDS - 1
    wd._maybe_restore_restart_budget()
    assert wd._restart_count == 0
    assert wd._announced_exhausted is False, "the give-up notice must re-arm"

    wd._restart_count = wd._max_restarts
    second, _ = await _run_outage(wd, cycles=20)
    assert second, "a NEW outage must be announced, not swallowed"


@pytest.mark.asyncio
async def test_recovery_rearms_the_notice_too():
    """The other reset path: a successful restart, not a budget restore."""
    wd = BrowserWatchdog()
    wd._announced_exhausted = True
    wd._restart_count = 1

    from naukri_server import browser_watchdog as mod

    fake_browser = type("B", (), {"page_pool": None})()
    with patch.object(mod, "BROWSER_RESTART_TIMEOUT", 5), \
         patch("naukri_server.browser.browser", fake_browser), \
         patch("naukri_server.events.event_bus.emit", new=AsyncMock()):
        fake_browser.stop = AsyncMock()
        fake_browser.start = AsyncMock()
        await wd._attempt_restart()

    assert wd._announced_exhausted is False
    assert wd._consecutive_failures == 0


# =====================================================================
# The clock assumption, pinned
# =====================================================================
# `test_a_second_outage_after_recovery_is_announced_again` set
# `_last_restart = 0.0` to mean "long ago". `time.monotonic()` counts from
# SYSTEM BOOT, so that is only true on a machine already up past the 600s
# budget window. It passed on a dev box up 95 hours and was red on every
# ubuntu runner in CI, which is where it had never once run green.
#
# These are GUARDS, not the reproduction -- the reproduction is the CI failure
# plus a local run with monotonic() pinned to a fresh-boot value. They exist so
# the assumption cannot be re-introduced silently.


class TestTheBudgetRestoreIgnoresTheAbsoluteClock:

    @pytest.mark.parametrize("now", [700.0, 1200.0, 344_000.0])
    @pytest.mark.parametrize("elapsed,expect_restore", [(599.0, False), (601.0, True)])
    def test_the_decision_depends_on_elapsed_time_not_machine_uptime(
            self, now, elapsed, expect_restore):
        """Same elapsed time must give the same verdict on any uptime.

        Three absolute clocks spanning a seconds-old container to a box up four
        days; the verdict must track only `now - _last_restart`.
        """
        from naukri_server import browser_watchdog as mod

        wd = BrowserWatchdog()
        wd._restart_count = wd._max_restarts
        wd._last_restart = now - elapsed

        with patch.object(mod.time, "monotonic", return_value=now):
            wd._maybe_restore_restart_budget()

        assert (wd._restart_count == 0) is expect_restore, (
            "uptime %.0fs, elapsed %.0fs: restore=%s, expected %s"
            % (now, elapsed, wd._restart_count == 0, expect_restore)
        )

    def test_a_fresh_boot_clock_does_not_fake_a_budget_restore(self):
        """The exact CI condition, asserted as correct CODE behaviour.

        45 seconds after boot the watchdog must NOT hand the budget back just
        because `_last_restart` happens to read 0.0. The code declining here is
        right; the old test asserting the opposite is what was wrong.
        """
        from naukri_server import browser_watchdog as mod

        wd = BrowserWatchdog()
        wd._restart_count = wd._max_restarts
        wd._last_restart = 0.0

        with patch.object(mod.time, "monotonic", return_value=45.0):
            wd._maybe_restore_restart_budget()

        assert wd._restart_count == wd._max_restarts, (
            "the budget was restored 45s after boot -- 0.0 is not a timestamp "
            "meaning 'long ago', it is the initialiser"
        )

    @pytest.mark.asyncio
    async def test_a_successful_restart_stamps_the_time_with_the_count(self):
        """WHY the 0.0 initialiser is unreachable in production.

        The whole diagnosis rests on this invariant: `_attempt_restart` sets
        `_restart_count` and `_last_restart` together, so a nonzero count always
        carries a real timestamp. If anything ever increments the count without
        stamping the clock, the 0.0 initialiser becomes reachable and the
        production bug the old test appeared to be reporting becomes real.
        """
        from naukri_server import browser_watchdog as mod

        wd = BrowserWatchdog()
        assert wd._restart_count == 0
        assert wd._last_restart == 0.0

        fake_browser = type("B", (), {"page_pool": None})()
        fake_browser.stop = AsyncMock()
        fake_browser.start = AsyncMock()

        with patch.object(mod, "BROWSER_RESTART_TIMEOUT", 5), \
             patch("naukri_server.browser.browser", fake_browser), \
             patch("naukri_server.events.event_bus.emit", new=AsyncMock()):
            await wd._attempt_restart()

        assert wd._restart_count == 1, "the restart did not count"
        assert wd._last_restart > 0.0, (
            "_restart_count moved without stamping _last_restart -- the 0.0 "
            "initialiser is now reachable with a nonzero count, which makes "
            "the budget restore depend on machine uptime for real"
        )
