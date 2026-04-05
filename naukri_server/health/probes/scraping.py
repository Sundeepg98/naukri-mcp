"""AmbitionBox scraping probes."""
import time

from naukri_server.health import health_probe, ProbeResult


@health_probe(name="scraping.ambitionbox_cookies", interval=3600, criticality="informational",
              description="Check AmbitionBox cookie cache")
async def ambitionbox_cookies() -> ProbeResult:
    try:
        from naukri_server.tools.ambitionbox_rest import _ab_cookies, _ab_cookies_ts
        from naukri_server.config import AB_COOKIE_TTL
        if _ab_cookies and (time.time() - _ab_cookies_ts) < AB_COOKIE_TTL:
            return ProbeResult(status="healthy", message="AB cookies cached")
        return ProbeResult(status="degraded", message="AB cookies expired/missing")
    except Exception:
        return ProbeResult(status="degraded", message="AB REST module not available")
