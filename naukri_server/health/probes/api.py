"""API subsystem probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="api.login_check", interval=300, criticality="critical",
              description="Verify Naukri session is active")
async def api_login_check() -> ProbeResult:
    from naukri_server.interfaces import api_client
    from naukri_server.config import NAUKRI_BASE
    try:
        data = await api_client.get(f"{NAUKRI_BASE}/cloudgateway-myaccount/myaccount/getaccountdetails")
        name = data.get("name", "unknown")
        return ProbeResult(status="healthy", message=f"Logged in as {name}")
    except Exception as e:
        return ProbeResult(status="unhealthy", message=f"Login check failed: {e}")


@health_probe(name="api.profile", interval=300, criticality="informational",
              description="Check profile API responds")
async def api_profile() -> ProbeResult:
    from naukri_server.interfaces import api_client
    from naukri_server.config import PROFILE_API
    try:
        data = await api_client.get(PROFILE_API)
        return ProbeResult(status="healthy", message="Profile API OK")
    except Exception as e:
        return ProbeResult(status="unhealthy", message=f"Profile API: {e}")


@health_probe(name="api.recommendations", interval=300, criticality="informational",
              description="Check recommendations API responds")
async def api_recommendations() -> ProbeResult:
    from naukri_server.interfaces import api_client
    from naukri_server.config import RECOMMENDED_JOBS_API
    try:
        data = await api_client.get(RECOMMENDED_JOBS_API, params={"pageNo": "1"})
        count = len(data.get("jobDetails", []))
        return ProbeResult(status="healthy", message=f"Recommendations: {count} jobs", metadata={"count": count})
    except Exception as e:
        return ProbeResult(status="degraded", message=f"Recommendations: {e}")


@health_probe(name="api.dashboard", interval=300, criticality="informational",
              description="Check dashboard API responds")
async def api_dashboard() -> ProbeResult:
    from naukri_server.interfaces import api_client
    from naukri_server.config import DASHBOARD_API
    try:
        data = await api_client.get(DASHBOARD_API)
        return ProbeResult(status="healthy", message="Dashboard API OK")
    except Exception as e:
        return ProbeResult(status="degraded", message=f"Dashboard: {e}")


@health_probe(name="api.metrics", interval=60, criticality="informational",
              description="Check API error rate")
async def api_metrics_check() -> ProbeResult:
    from naukri_server.api import api_metrics
    total = api_metrics.total
    if total == 0:
        return ProbeResult(status="healthy", message="No API calls yet")
    error_rate = api_metrics.errors / total
    if error_rate > 0.2:
        return ProbeResult(status="unhealthy", message=f"Error rate {error_rate:.0%}", metadata={"error_rate": round(error_rate, 3)})
    elif error_rate > 0.05:
        return ProbeResult(status="degraded", message=f"Error rate {error_rate:.0%}", metadata={"error_rate": round(error_rate, 3)})
    return ProbeResult(status="healthy", message=f"Error rate {error_rate:.0%} ({total} calls)")
