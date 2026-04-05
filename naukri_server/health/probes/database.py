"""Database probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="database.connectivity", interval=300, criticality="critical",
              description="Check SQLite database is accessible")
async def database_connectivity() -> ProbeResult:
    try:
        from naukri_server.database import get_db
        db = await get_db()
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM applications")
            count = (await cursor.fetchone())[0]
            return ProbeResult(status="healthy", message=f"DB OK, {count} applications", metadata={"app_count": count})
        finally:
            await db.close()
    except Exception as e:
        return ProbeResult(status="unhealthy", message=f"DB error: {e}")
