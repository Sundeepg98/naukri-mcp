"""Core health probe framework — registry, decorator, scheduler."""

import asyncio
import logging
import time
from naukri_server._compat import timeout as _timeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeResult:
    """Immutable result returned by every health probe."""
    status: str          # "healthy", "degraded", "unhealthy"
    message: str
    elapsed_ms: float = 0.0
    metadata: dict = field(default_factory=dict)
    timestamp: str = ""


@dataclass
class _RegisteredProbe:
    """Internal bookkeeping for a registered probe."""
    name: str
    description: str
    criticality: str        # "critical" or "informational"
    interval_seconds: float
    execute: Callable
    subsystem: str
    last_result: Optional[ProbeResult] = None
    previous_status: Optional[str] = None


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def health_probe(
    name: str,
    interval: float = 60,
    criticality: str = "informational",
    description: str = "",
):
    """Register an async function as a health probe.

    The wrapper measures elapsed time, catches exceptions, and stamps the
    result with ``elapsed_ms`` and ``timestamp`` before storing it in the
    global :pydata:`probe_registry`.
    """

    def decorator(fn: Callable) -> Callable:
        subsystem = name.split(".")[0] if "." in name else name
        desc = description or fn.__doc__ or ""

        async def wrapper(*args, **kwargs) -> ProbeResult:
            t0 = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                return ProbeResult(
                    status="unhealthy",
                    message=f"Probe exception: {exc}",
                    elapsed_ms=round(elapsed, 2),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            elapsed = (time.monotonic() - t0) * 1000
            # Overwrite elapsed_ms and timestamp on the returned result
            return ProbeResult(
                status=result.status,
                message=result.message,
                elapsed_ms=round(elapsed, 2),
                metadata=result.metadata,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        wrapper.__name__ = fn.__name__
        wrapper.__qualname__ = fn.__qualname__

        registered = _RegisteredProbe(
            name=name,
            description=desc,
            criticality=criticality,
            interval_seconds=interval,
            execute=wrapper,
            subsystem=subsystem,
        )
        probe_registry._register(registered)
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Registry (singleton)
# ---------------------------------------------------------------------------

class HealthProbeRegistry:
    """Global store of all registered health probes."""

    def __init__(self):
        self._probes: dict[str, _RegisteredProbe] = {}

    def _register(self, probe: _RegisteredProbe):
        """Add a probe to the registry (called by the decorator)."""
        self._probes[probe.name] = probe
        logger.debug("Registered health probe: %s (interval=%ss, criticality=%s)",
                      probe.name, probe.interval_seconds, probe.criticality)

    def get(self, name: str) -> Optional[_RegisteredProbe]:
        """Look up a probe by fully-qualified name."""
        return self._probes.get(name)

    def by_subsystem(self, subsystem: str) -> list[_RegisteredProbe]:
        """Return all probes belonging to *subsystem* (the prefix before '.')."""
        return [p for p in self._probes.values() if p.subsystem == subsystem]

    def by_criticality(self, criticality: str) -> list[_RegisteredProbe]:
        """Return all probes with the given criticality level."""
        return [p for p in self._probes.values() if p.criticality == criticality]

    def by_interval(self, interval: float) -> list[_RegisteredProbe]:
        """Return all probes whose interval matches *interval*."""
        return [p for p in self._probes.values() if p.interval_seconds == interval]

    def all_results(self) -> dict:
        """Snapshot of the latest result for every probe.

        Returns a dict keyed by probe name with status, message, elapsed_ms,
        metadata, and timestamp.  Probes that have never run are omitted.
        """
        out: dict = {}
        for name, probe in self._probes.items():
            if probe.last_result is not None:
                r = probe.last_result
                out[name] = {
                    "status": r.status,
                    "message": r.message,
                    "elapsed_ms": r.elapsed_ms,
                    "metadata": r.metadata,
                    "timestamp": r.timestamp,
                }
        return out

    def summary(self) -> dict:
        """Aggregate health summary across all probes.

        Returns counts by status, a per-subsystem breakdown, and a list of
        critical probes that are currently unhealthy.
        """
        total = len(self._probes)
        healthy = degraded = unhealthy = not_run = 0
        by_subsystem: dict[str, dict[str, int]] = {}
        critical_failures: list[str] = []

        for name, probe in self._probes.items():
            sub = probe.subsystem
            if sub not in by_subsystem:
                by_subsystem[sub] = {"healthy": 0, "degraded": 0, "unhealthy": 0, "not_run": 0}

            if probe.last_result is None:
                not_run += 1
                by_subsystem[sub]["not_run"] += 1
                continue

            s = probe.last_result.status
            if s == "healthy":
                healthy += 1
                by_subsystem[sub]["healthy"] += 1
            elif s == "degraded":
                degraded += 1
                by_subsystem[sub]["degraded"] += 1
            else:
                unhealthy += 1
                by_subsystem[sub]["unhealthy"] += 1
                if probe.criticality == "critical":
                    critical_failures.append(name)

        return {
            "total": total,
            "healthy": healthy,
            "degraded": degraded,
            "unhealthy": unhealthy,
            "not_run": not_run,
            "by_subsystem": by_subsystem,
            "critical_failures": critical_failures,
        }

    @property
    def probe_count(self) -> int:
        return len(self._probes)


probe_registry = HealthProbeRegistry()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class HealthProbeScheduler:
    """Runs registered probes on their declared intervals."""

    INTERVALS = {30, 60, 300, 3600}

    def __init__(self, registry: HealthProbeRegistry, watchdog=None):
        self._registry = registry
        self._watchdog = watchdog
        self._tasks: list[asyncio.Task] = []
        self._running = False

    async def start(self):
        """Create one asyncio task per interval bucket."""
        self._running = True
        buckets: dict[float, list[_RegisteredProbe]] = {}
        for probe in self._registry._probes.values():
            buckets.setdefault(probe.interval_seconds, []).append(probe)

        for interval, probes in buckets.items():
            task = asyncio.create_task(
                self._run_interval(interval, probes),
                name=f"health-{interval}s",
            )
            self._tasks.append(task)

        logger.info(
            "HealthProbeScheduler started: %d probes in %d interval buckets",
            self._registry.probe_count,
            len(buckets),
        )

    async def stop(self):
        """Cancel all running interval tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("HealthProbeScheduler stopped")

    # -- internal -----------------------------------------------------------

    async def _run_interval(self, interval: float, probes: list[_RegisteredProbe]):
        """Loop: sleep → run all probes in this bucket concurrently → process."""
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break
                await asyncio.gather(
                    *[self._execute_probe(p) for p in probes],
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Scheduler interval %ss error: %s", interval, exc)

    async def _execute_probe(self, probe: _RegisteredProbe):
        """Run a single probe with a timeout, then process the result."""
        timeout = min(probe.interval_seconds * 0.8, 30)
        try:
            async with _timeout(timeout):
                result = await probe.execute()
        except asyncio.TimeoutError:
            result = ProbeResult(
                status="unhealthy",
                message=f"Probe timed out after {timeout:.0f}s",
                elapsed_ms=timeout * 1000,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            result = ProbeResult(
                status="unhealthy",
                message=f"Probe execution error: {exc}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        await self._on_result(probe, result)

    async def _on_result(self, probe: _RegisteredProbe, result: ProbeResult):
        """Store result, detect state changes, trigger watchdog if needed."""
        old_status = probe.previous_status
        probe.last_result = result
        probe.previous_status = result.status

        # State change detection
        if old_status is not None and old_status != result.status:
            try:
                from naukri_server.events import event_bus, ProbeStateChanged
                await event_bus.emit(ProbeStateChanged(
                    probe_name=probe.name,
                    old_status=old_status,
                    new_status=result.status,
                    message=result.message,
                    criticality=probe.criticality,
                ))
            except Exception as exc:
                logger.debug("Failed to emit ProbeStateChanged: %s", exc)

        # Special case: browser liveness failure triggers watchdog
        if (
            probe.name == "browser.liveness"
            and result.status == "unhealthy"
            and self._watchdog is not None
        ):
            try:
                await self._watchdog._handle_failure()
            except Exception as exc:
                logger.debug("Watchdog _handle_failure error: %s", exc)
