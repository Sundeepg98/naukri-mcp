"""Page pool probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="pool.utilization", interval=30, criticality="informational",
              description="Check page pool stats")
async def pool_utilization() -> ProbeResult:
    from naukri_server.browser import browser
    if not browser or not browser.page_pool:
        return ProbeResult(status="degraded", message="No page pool")
    stats = browser.page_pool.get_stats()
    return ProbeResult(status="healthy", message=f"Pool: {stats.get('checkouts', 0)} checkouts", metadata=stats)


@health_probe(name="pool.crash_rate", interval=60, criticality="critical",
              description="Monitor tab crash frequency")
async def pool_crash_rate() -> ProbeResult:
    from naukri_server.browser import browser
    if not browser or not browser.page_pool:
        return ProbeResult(status="degraded", message="No page pool")
    stats = browser.page_pool.get_stats()
    crashes = stats.get("crashes", 0)
    max_crashes = stats.get("max_crashes", 10)
    if crashes >= max_crashes:
        return ProbeResult(status="unhealthy", message=f"Crash limit reached: {crashes}/{max_crashes}")
    elif crashes > max_crashes * 0.5:
        return ProbeResult(status="degraded", message=f"High crash rate: {crashes}/{max_crashes}")
    return ProbeResult(status="healthy", message=f"Crashes: {crashes}/{max_crashes}")
