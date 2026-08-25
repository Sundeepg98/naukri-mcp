"""Browser subsystem probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="browser.liveness", interval=30, criticality="critical",
              description="Check if Chromium is alive, or intentionally suspended")
async def browser_liveness() -> ProbeResult:
    """Report the browser as running, intentionally suspended, or down.

    THREE STATES, NOT TWO (2026-08-25). The server now closes an idle context on
    purpose, so "no Chrome process" is a normal resting state rather than a
    crash. Reporting that as unhealthy would drive the watchdog to relaunch
    Chrome every minute and emit BrowserCrashed each time.

    Suspended is reported HEALTHY, and it is a distinct verdict rather than a
    disguised one: the message says so and metadata carries
    browser_state="suspended", so nothing is hidden from naukri_health_check or
    from anyone reading the probe history. It is also not a blanket amnesty -
    NaukriBrowser.liveness() verifies the suspension is real (no context, no
    Playwright, pool parked) and reports DOWN for anything that merely claims to
    be idle, so a genuine crash is still caught while suspended. Status is used
    rather than "degraded" deliberately: degraded on a CRITICAL probe would
    park the health summary in a permanent warning for a browser that is working
    exactly as designed - the same notification-noise failure as the 2342
    spurious BrowserCrashed events.

    The probe never resumes the browser: liveness() checks out with
    allow_resume=False, so monitoring cannot undo the idle state it is watching.
    """
    from naukri_server.browser import (
        browser, BROWSER_RUNNING, BROWSER_SUSPENDED,
    )
    if not browser:
        return ProbeResult(status="unhealthy", message="Browser not available")
    try:
        from naukri_server._compat import timeout as _timeout
        async with _timeout(10):
            state, message = await browser.liveness()
    except Exception as e:
        return ProbeResult(status="unhealthy", message=f"Probe failed: {e}")

    if state == BROWSER_RUNNING:
        return ProbeResult(status="healthy", message=message,
                           metadata={"browser_state": state})
    if state == BROWSER_SUSPENDED:
        return ProbeResult(status="healthy", message=message,
                           metadata={"browser_state": state, "suspended": True})
    return ProbeResult(status="unhealthy", message=message,
                       metadata={"browser_state": state})


@health_probe(name="browser.circuit_breaker", interval=30, criticality="critical",
              description="Check circuit breaker state")
async def browser_circuit_breaker() -> ProbeResult:
    from naukri_server.browser import browser
    if not browser or not browser.page_pool:
        return ProbeResult(status="unhealthy", message="No page pool")
    state = browser.page_pool.circuit_state
    if state == "closed":
        return ProbeResult(status="healthy", message="Circuit closed", metadata={"state": state})
    elif state == "half_open":
        return ProbeResult(status="degraded", message="Circuit half-open", metadata={"state": state})
    return ProbeResult(status="unhealthy", message="Circuit OPEN", metadata={"state": state})
