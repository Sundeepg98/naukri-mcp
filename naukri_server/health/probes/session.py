"""Session/auth probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="session.token_valid", interval=60, criticality="critical",
              description="Check if auth token is present")
async def session_token_valid() -> ProbeResult:
    from naukri_server.browser import browser
    if not browser or not browser.token_manager:
        return ProbeResult(status="unhealthy", message="No token manager")
    token = browser.token_manager.get_token()
    if token:
        return ProbeResult(status="healthy", message="Token present", metadata={"token_length": len(token)})
    return ProbeResult(status="unhealthy", message="No token — login required")


@health_probe(name="session.refresh_rate", interval=60, criticality="informational",
              description="Monitor token refresh frequency")
async def session_refresh_rate() -> ProbeResult:
    from naukri_server.api import api_metrics
    refreshes = api_metrics.auth_refreshes
    total = api_metrics.total
    if total == 0:
        return ProbeResult(status="healthy", message="No requests yet")
    rate = refreshes / max(total, 1)
    if rate > 0.1:
        return ProbeResult(status="degraded", message=f"High refresh rate: {refreshes}/{total}", metadata={"rate": round(rate, 3)})
    return ProbeResult(status="healthy", message=f"Refresh rate normal: {refreshes}/{total}")
