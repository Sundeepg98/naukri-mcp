"""API subsystem probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="api.login_check", interval=300, criticality="critical",
              description="Verify Naukri session is active")
async def api_login_check() -> ProbeResult:
    """Answer "is the session alive?" through the SAME code naukri_auth_status uses.

    This probe used to GET /cloudgateway-myaccount/myaccount/getaccountdetails
    and call any exception a dead session. That route does not exist on Naukri's
    gateway -- it answers HTTP 404 on every call, whatever the session state
    (confirmed live 2026-08-21 through both api_client and a cookie-only fetch,
    and the path appears nowhere else in this repo). So the probe could never
    return healthy, and because it is criticality="critical" its permanent
    failure was promoted into every naukri_daily_brief as a high-priority
    "System health alert". It contradicted naukri_auth_status in the same minute.

    Delegating to auth_service.get_login_status is what makes that class of
    contradiction structurally impossible: the probe and the tool now read the
    same endpoint through the same client and share one verdict. It also buys
    the honesty bit -- ``verified`` distinguishes "PROVEN logged out" from
    "could not check", which a bare ``except Exception`` flattened into one
    alarming answer.

    Verdict map:
      healthy   -- session confirmed
      unhealthy -- PROVEN dead (no token / api_denied / 401 / 403). Critical,
                   so this is what reaches the daily brief.
      degraded  -- could not be determined (404, 5xx, network). NOT a logout
                   claim, and never a critical failure -- an unverifiable check
                   must not cry wolf.
    """
    from naukri_server.services.auth_service import get_login_status
    status = await get_login_status()
    reason = status.get("reason", "unknown")

    if status.get("logged_in"):
        return ProbeResult(status="healthy", message=f"Session active ({reason})",
                           metadata={"reason": reason})
    if status.get("verified"):
        return ProbeResult(status="unhealthy", message=f"Session is not active: {reason}",
                           metadata={"reason": reason})
    return ProbeResult(
        status="degraded",
        message=f"Could not verify the session ({reason}) - not a logout claim",
        metadata={"reason": reason},
    )


@health_probe(name="api.profile", interval=300, criticality="informational",
              description="Check profile API responds")
async def api_profile() -> ProbeResult:
    """Check the profile API responds.

    expand_level is REQUIRED: without it the endpoint answers HTTP 400 Bad
    Request every time, so this probe was another permanent failure (measured
    live 2026-08-21 alongside naukri_health_check's own profile check, which
    passes the param and returned the real profile in the same minute).
    """
    from naukri_server.interfaces import api_client
    from naukri_server.config import PROFILE_API
    try:
        data = await api_client.get(PROFILE_API, params={"expand_level": "4"})
        return ProbeResult(status="healthy", message="Profile API OK")
    except Exception as e:
        return ProbeResult(status="unhealthy", message=f"Profile API: {e}")


@health_probe(name="api.recommendations", interval=300, criticality="informational",
              description="Check recommendations API responds")
async def api_recommendations() -> ProbeResult:
    """Check the recommendations API responds.

    recom-jobs is a POST-only search endpoint: GET answers HTTP 405 Method Not
    Allowed on every call, which made this a third permanent failure. The POST
    is a read-only search with an empty body -- no side effects, and it is
    exactly the call naukri_health_check._check_recommendations_api makes.
    """
    from naukri_server.interfaces import api_client
    from naukri_server.config import RECOMMENDED_JOBS_API
    try:
        data = await api_client.post(RECOMMENDED_JOBS_API, body={})
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
