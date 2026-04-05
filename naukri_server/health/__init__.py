"""Health probe plugin framework — self-monitoring infrastructure.

Usage:
    from naukri_server.health import health_probe, ProbeResult

    @health_probe(name="my.probe", interval=60, criticality="informational")
    async def my_probe() -> ProbeResult:
        return ProbeResult(status="healthy", message="All good")
"""

from naukri_server.health.framework import (
    ProbeResult,
    health_probe,
    probe_registry,
    HealthProbeScheduler,
)

__all__ = ["ProbeResult", "health_probe", "probe_registry", "HealthProbeScheduler"]
