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
    wd._last_restart = 0.0
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
