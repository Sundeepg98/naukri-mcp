"""Notification queue probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="notifications.backlog", interval=300, criticality="informational",
              description="Check notification queue depth")
async def notifications_backlog() -> ProbeResult:
    try:
        from naukri_server.database import count_undelivered_notifications
        count = await count_undelivered_notifications()
        if count > 100:
            return ProbeResult(status="degraded", message=f"Backlog: {count} undelivered", metadata={"count": count})
        return ProbeResult(status="healthy", message=f"{count} pending notifications", metadata={"count": count})
    except Exception as e:
        return ProbeResult(status="degraded", message=f"Can't check: {e}")
