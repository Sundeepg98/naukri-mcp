"""Health probes for the autonomous job-hunting agent."""

import asyncio
import time
from naukri_server.health import health_probe, ProbeResult


@health_probe(
    name="agent.status",
    interval=300,
    criticality="informational",
    description="Check agent cycle health and recent activity",
)
async def agent_status_probe() -> ProbeResult:
    """Verify agent is configured and recent cycles are healthy."""
    start = time.monotonic()
    try:
        from naukri_server.agent import load_agent_config
        from naukri_server.database import list_agent_runs

        config = await asyncio.to_thread(load_agent_config)
        recent = await list_agent_runs(limit=5)

        elapsed = (time.monotonic() - start) * 1000
        enabled = config.get("enabled", False)
        mode = config.get("mode", "unknown")

        metadata = {
            "enabled": enabled,
            "mode": mode,
            "recent_cycles": len(recent),
        }

        if not enabled:
            return ProbeResult(
                status="healthy",
                message="Agent disabled (by design)",
                elapsed_ms=elapsed,
                metadata=metadata,
            )

        if not recent:
            return ProbeResult(
                status="degraded",
                message="Agent enabled but no cycles recorded",
                elapsed_ms=elapsed,
                metadata=metadata,
            )

        # Check last cycle
        last = recent[0]
        last_status = last.get("status", "unknown")
        metadata["last_cycle_status"] = last_status
        metadata["last_cycle_id"] = last.get("cycle_id")
        metadata["last_applied"] = last.get("applied_count", 0)

        if last_status == "error":
            return ProbeResult(
                status="degraded",
                message=f"Last cycle failed: {last.get('error', 'unknown')[:100]}",
                elapsed_ms=elapsed,
                metadata=metadata,
            )

        # Check error rate in recent cycles
        errors = sum(1 for r in recent if r.get("status") == "error")
        if errors >= 3:
            return ProbeResult(
                status="unhealthy",
                message=f"{errors}/{len(recent)} recent cycles failed",
                elapsed_ms=elapsed,
                metadata=metadata,
            )

        return ProbeResult(
            status="healthy",
            message=f"Agent {mode} mode, last cycle: {last_status}, applied: {last.get('applied_count', 0)}",
            elapsed_ms=elapsed,
            metadata=metadata,
        )
    except Exception as e:
        return ProbeResult(
            status="unhealthy",
            message=f"Agent probe error: {e}",
            elapsed_ms=(time.monotonic() - start) * 1000,
        )
