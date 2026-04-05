"""Event bus probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="events.subscriber_health", interval=3600, criticality="informational",
              description="Check event subscribers are registered")
async def events_subscriber_health() -> ProbeResult:
    from naukri_server.events import event_bus
    counts = event_bus.subscriber_count_by_type
    total = sum(counts.values())
    if total == 0:
        return ProbeResult(status="unhealthy", message="No subscribers registered")
    return ProbeResult(status="healthy", message=f"{total} subscribers across {len(counts)} events", metadata=counts)
