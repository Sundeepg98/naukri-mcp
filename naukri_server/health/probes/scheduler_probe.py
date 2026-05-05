"""Health probes for the background task scheduler."""

import time
from naukri_server.health import health_probe, ProbeResult


@health_probe(
    name="scheduler.status",
    interval=60,
    criticality="warning",
    description="Check scheduler is running and tasks are executing",
)
async def scheduler_status_probe() -> ProbeResult:
    """Verify scheduler is running and tasks have completed recently."""
    start = time.monotonic()
    try:
        import naukri_server.scheduler as _mod

        if _mod.scheduler is None:
            return ProbeResult(
                status="unhealthy",
                message="Scheduler not initialized",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

        sched = _mod.scheduler
        if not sched._running:
            return ProbeResult(
                status="unhealthy",
                message="Scheduler is stopped",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

        # Check last run times
        from naukri_server.database import list_scheduled_runs

        recent = await list_scheduled_runs(limit=20)

        if not recent:
            return ProbeResult(
                status="degraded",
                message="No scheduled runs recorded yet",
                elapsed_ms=(time.monotonic() - start) * 1000,
                metadata={"total_tasks": sched.status.get("total_tasks", 0)},
            )

        # Check for failures in last 10 runs
        failed = [r for r in recent[:10] if r.get("status") == "failed"]
        fail_rate = len(failed) / min(len(recent), 10) * 100

        elapsed = (time.monotonic() - start) * 1000
        metadata = {
            "total_tasks": sched.status.get("total_tasks", 0),
            "enabled_tasks": sched.status.get("enabled_tasks", 0),
            "recent_runs": len(recent),
            "recent_failures": len(failed),
            "fail_rate_pct": round(fail_rate, 1),
        }

        if fail_rate > 50:
            return ProbeResult(
                status="unhealthy",
                message=f"High failure rate: {fail_rate:.0f}%",
                elapsed_ms=elapsed,
                metadata=metadata,
            )
        if fail_rate > 20:
            return ProbeResult(
                status="degraded",
                message=f"Elevated failure rate: {fail_rate:.0f}%",
                elapsed_ms=elapsed,
                metadata=metadata,
            )

        return ProbeResult(
            status="healthy",
            message=f"Scheduler running, {len(recent)} recent runs, {fail_rate:.0f}% fail rate",
            elapsed_ms=elapsed,
            metadata=metadata,
        )
    except Exception as e:
        return ProbeResult(
            status="unhealthy",
            message=f"Scheduler probe error: {e}",
            elapsed_ms=(time.monotonic() - start) * 1000,
        )
