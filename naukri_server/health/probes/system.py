"""System-level probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="system.data_dir", interval=3600, criticality="informational",
              description="Check data directory is writable")
async def system_data_dir() -> ProbeResult:
    from naukri_server.config import DATA_DIR
    import os
    if not DATA_DIR.exists():
        return ProbeResult(status="unhealthy", message=f"DATA_DIR missing: {DATA_DIR}")
    if not os.access(str(DATA_DIR), os.W_OK):
        return ProbeResult(status="unhealthy", message=f"DATA_DIR not writable: {DATA_DIR}")
    return ProbeResult(status="healthy", message=f"DATA_DIR OK: {DATA_DIR}")
