"""REST API endpoints for the monitoring dashboard."""

import logging
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse

from naukri_server import mcp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple TTL cache for expensive endpoints
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 60  # seconds
HEALTH_CACHE_TTL = 30  # seconds

_template_cache: str | None = None


def _get_cached(key: str, ttl: int = CACHE_TTL) -> dict | None:
    """Return cached value if not expired."""
    if key in _cache:
        ts, data = _cache[key]
        if time.monotonic() - ts < ttl:
            return data
    return None


def _set_cache(key: str, data: dict):
    """Store value in cache."""
    _cache[key] = (time.monotonic(), data)


def _reset_caches():
    """Clear all route caches. Called between test runs."""
    global _template_cache
    _cache.clear()
    _template_cache = None


@mcp.custom_route("/api/dashboard", methods=["GET"])
async def api_dashboard(request: Request) -> JSONResponse:
    """Morning brief summary — aggregates 20 data sources."""
    cached = _get_cached("dashboard")
    if cached:
        return JSONResponse(cached)
    from naukri_server.tools.daily_brief import naukri_daily_brief
    try:
        result = await naukri_daily_brief()
        _set_cache("dashboard", result)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Dashboard API error")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@mcp.custom_route("/api/funnel", methods=["GET"])
async def api_funnel(request: Request) -> JSONResponse:
    """Application conversion funnel."""
    try:
        days = int(request.query_params.get("days", "30"))
        days = max(1, min(days, 365))  # Clamp to 1-365
    except (ValueError, TypeError):
        days = 30
    cache_key = f"funnel:{days}"
    cached = _get_cached(cache_key)
    if cached:
        return JSONResponse(cached)
    from naukri_server.services.insights_service import conversion_funnel
    try:
        result = await conversion_funnel(days)
        _set_cache(cache_key, result)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Funnel API error")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@mcp.custom_route("/api/health", methods=["GET"])
async def api_health(request: Request) -> JSONResponse:
    """Health probe status for all subsystems."""
    cached = _get_cached("health", ttl=HEALTH_CACHE_TTL)
    if cached:
        return JSONResponse(cached)
    from naukri_server.health import probe_registry
    try:
        probes = {}
        for name, probe in probe_registry._probes.items():
            last = probe.last_result
            probes[name] = {
                "status": last.status if last else "unknown",
                "message": last.message if last else "No data yet",
                "elapsed_ms": last.elapsed_ms if last else None,
                "timestamp": last.timestamp if last else None,
                "criticality": probe.criticality,
            }
        # Overall status
        statuses = [p["status"] for p in probes.values()]
        overall = "healthy"
        if any(s == "unhealthy" for s in statuses):
            overall = "unhealthy"
        elif any(s == "degraded" for s in statuses):
            overall = "degraded"
        result = {
            "status": "success",
            "overall": overall,
            "probe_count": len(probes),
            "probes": probes,
        }
        _set_cache("health", result)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Health API error")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@mcp.custom_route("/api/stale", methods=["GET"])
async def api_stale(request: Request) -> JSONResponse:
    """Stale applications needing follow-up."""
    try:
        days = int(request.query_params.get("days", "14"))
        days = max(1, min(days, 365))
        score = int(request.query_params.get("score", "40"))
        score = max(0, min(score, 100))
    except (ValueError, TypeError):
        days, score = 14, 40
    cache_key = f"stale:{days}:{score}"
    cached = _get_cached(cache_key)
    if cached:
        return JSONResponse(cached)
    from naukri_server.services.application_service import get_stale_applications
    try:
        result = await get_stale_applications(days_threshold=days, min_stale_score=score)
        _set_cache(cache_key, result)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Stale API error")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@mcp.custom_route("/api/notifications", methods=["GET"])
async def api_notifications(request: Request) -> JSONResponse:
    """Recent notifications from unified notify API."""
    from naukri_server.tools.notifications import _get_unified_notify
    try:
        result = await _get_unified_notify()
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Notifications API error")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@mcp.custom_route("/api/scheduler", methods=["GET"])
async def api_scheduler(request: Request) -> JSONResponse:
    """Background task scheduler status."""
    import naukri_server.scheduler as _mod
    from naukri_server.database import get_last_run
    try:
        if _mod.scheduler is None:
            return JSONResponse(
                {"status": "error", "message": "Scheduler not initialized"},
                status_code=503,
            )
        status = _mod.scheduler.status
        # Enrich each task with its last run info
        tasks = {}
        for name, info in status.get("tasks", {}).items():
            last = await get_last_run(name)
            tasks[name] = {
                **info,
                "last_run": {
                    "started_at": last["started_at"],
                    "status": last["status"],
                    "duration_ms": last.get("duration_ms"),
                } if last else None,
            }
        status["tasks"] = tasks
        return JSONResponse({"status": "success", **status})
    except Exception as e:
        logger.exception("Scheduler API error")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@mcp.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> JSONResponse:
    """Application statistics and counts."""
    from naukri_server.database import count_applications_by_status, list_scheduled_runs
    try:
        app_counts = await count_applications_by_status()
        recent_runs = await list_scheduled_runs(limit=10)
        return JSONResponse({
            "status": "success",
            "applications_by_status": app_counts,
            "recent_scheduled_runs": recent_runs,
        })
    except Exception as e:
        logger.exception("Stats API error")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@mcp.custom_route("/api/diagnostics", methods=["GET"])
async def api_diagnostics(request: Request) -> JSONResponse:
    """Agent and scheduler diagnostics — step timings, skip stats, performance."""
    try:
        from naukri_server.database import list_agent_runs, get_agent_metrics, get_agent_skip_stats

        metrics = await get_agent_metrics(days=7)
        skip_stats = await get_agent_skip_stats(days=7)
        recent_runs = await list_agent_runs(limit=10)

        return JSONResponse({
            "status": "success",
            "agent_metrics_7d": metrics,
            "skip_reasons_7d": skip_stats,
            "recent_runs": recent_runs,
        })
    except Exception as e:
        logger.exception("Diagnostics API error")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_html(request: Request) -> HTMLResponse:
    """Serve the monitoring dashboard HTML page."""
    global _template_cache
    if _template_cache is None:
        template_path = Path(__file__).parent / "template.html"
        try:
            _template_cache = template_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return HTMLResponse("<h1>Dashboard template not found</h1>", status_code=500)
    return HTMLResponse(_template_cache)
