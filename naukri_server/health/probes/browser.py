"""Browser subsystem probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="browser.liveness", interval=30, criticality="critical",
              description="Check if Chromium process is alive")
async def browser_liveness() -> ProbeResult:
    from naukri_server.browser import browser
    if not browser or not browser.available:
        return ProbeResult(status="unhealthy", message="Browser not available")
    try:
        from naukri_server._compat import timeout as _timeout
        from naukri_server.browser import is_page_alive
        async with _timeout(10):
            async with browser.page_pool.acquire() as page:
                # `_ = page.url` here was a cached string read that could not
                # raise, so this probe reported "Browser alive" for 37 minutes
                # against a dead Chromium on 2026-08-20. Round-trip instead.
                if await is_page_alive(page):
                    return ProbeResult(status="healthy", message="Browser alive")
                return ProbeResult(
                    status="unhealthy",
                    message="Page did not answer a liveness round-trip (target closed or wedged)",
                )
    except Exception as e:
        return ProbeResult(status="unhealthy", message=f"Probe failed: {e}")


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
