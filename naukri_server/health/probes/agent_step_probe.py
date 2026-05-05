"""Per-step latency probes for the autonomous agent."""

import time
from naukri_server.health.framework import health_probe, ProbeResult


@health_probe(
    name="agent.step_performance",
    interval=600,
    criticality="informational",
    description="Track per-step latency percentiles for agent cycles"
)
async def agent_step_performance_probe() -> ProbeResult:
    """Analyze step timing from recent agent cycles."""
    start = time.monotonic()
    try:
        from naukri_server.database import list_agent_runs
        import json

        runs = await list_agent_runs(limit=20)
        completed = [r for r in runs if r.get("status") == "completed" and r.get("metadata")]

        if not completed:
            return ProbeResult(
                status="healthy",
                message="No completed agent cycles to analyze",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

        # Extract step timings from metadata
        step_data: dict[str, list[float]] = {}
        for run in completed:
            try:
                meta = json.loads(run["metadata"]) if isinstance(run["metadata"], str) else run["metadata"]
                timings = meta.get("step_timings", {})
                for step_name, ms in timings.items():
                    step_data.setdefault(step_name, []).append(float(ms))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

        if not step_data:
            return ProbeResult(
                status="healthy",
                message="No step timing data available",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

        # Calculate percentiles
        def percentile(data, p):
            sorted_d = sorted(data)
            idx = int(len(sorted_d) * p / 100)
            return sorted_d[min(idx, len(sorted_d) - 1)]

        metrics = {}
        for step, values in step_data.items():
            metrics[step] = {
                "p50_ms": round(percentile(values, 50), 0),
                "p95_ms": round(percentile(values, 95), 0),
                "count": len(values),
            }

        elapsed = (time.monotonic() - start) * 1000
        return ProbeResult(
            status="healthy",
            message=f"Step timings from {len(completed)} cycles",
            elapsed_ms=elapsed,
            metadata={"steps": metrics, "cycles_analyzed": len(completed)},
        )
    except Exception as e:
        return ProbeResult(
            status="unhealthy",
            message=f"Step probe error: {e}",
            elapsed_ms=(time.monotonic() - start) * 1000,
        )
