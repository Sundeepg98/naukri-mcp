"""System-level probes."""
from naukri_server.health import health_probe, ProbeResult


@health_probe(name="system.data_dir", interval=3600, criticality="informational",
              description="Check data directory is writable")
async def system_data_dir() -> ProbeResult:
    from naukri_server.config import DATA_DIR
    import os
    # The verdict is the whole payload here. Naming DATA_DIR added nothing a
    # caller can act on and put an absolute local path in every health check.
    if not DATA_DIR.exists():
        return ProbeResult(status="unhealthy", message="Data directory is missing")
    if not os.access(str(DATA_DIR), os.W_OK):
        return ProbeResult(status="unhealthy", message="Data directory is not writable")
    return ProbeResult(status="healthy", message="Data directory present and writable")
