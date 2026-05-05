"""Unit tests for naukri_server.health.framework — registry, decorator, scheduler.

All tests are PURE: no network, no browser, no file I/O. Each test instantiates
its own HealthProbeRegistry to avoid polluting the module-level singleton.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_server.health.framework import (
    ProbeResult,
    HealthProbeRegistry,
    HealthProbeScheduler,
    _RegisteredProbe,
    health_probe,
    probe_registry,
)


# ---------------------------------------------------------------------------
# 1. ProbeResult dataclass
# ---------------------------------------------------------------------------

class TestProbeResult:
    def test_minimal_construction(self):
        r = ProbeResult(status="healthy", message="ok")
        assert r.status == "healthy"
        assert r.message == "ok"
        assert r.elapsed_ms == 0.0
        assert r.metadata == {}
        assert r.timestamp == ""

    def test_full_construction(self):
        r = ProbeResult(
            status="degraded",
            message="slow",
            elapsed_ms=123.45,
            metadata={"foo": "bar"},
            timestamp="2026-01-01T00:00:00Z",
        )
        assert r.elapsed_ms == 123.45
        assert r.metadata == {"foo": "bar"}
        assert r.timestamp == "2026-01-01T00:00:00Z"

    def test_immutable(self):
        """ProbeResult is frozen — assignment raises FrozenInstanceError."""
        r = ProbeResult(status="healthy", message="ok")
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            r.status = "unhealthy"


# ---------------------------------------------------------------------------
# 2. HealthProbeRegistry — register / lookup / filter
# ---------------------------------------------------------------------------

class TestHealthProbeRegistry:
    def _make_probe(self, name: str, criticality: str = "informational",
                    interval: float = 60.0) -> _RegisteredProbe:
        async def _stub() -> ProbeResult:
            return ProbeResult(status="healthy", message="ok")
        return _RegisteredProbe(
            name=name, description="test", criticality=criticality,
            interval_seconds=interval, execute=_stub,
            subsystem=name.split(".")[0] if "." in name else name,
        )

    def test_register_and_get(self):
        r = HealthProbeRegistry()
        p = self._make_probe("browser.liveness")
        r._register(p)
        assert r.get("browser.liveness") is p
        assert r.get("nonexistent") is None
        assert r.probe_count == 1

    def test_by_subsystem(self):
        r = HealthProbeRegistry()
        r._register(self._make_probe("browser.liveness"))
        r._register(self._make_probe("browser.token_health"))
        r._register(self._make_probe("api.latency"))
        browser_probes = r.by_subsystem("browser")
        api_probes = r.by_subsystem("api")
        assert len(browser_probes) == 2
        assert len(api_probes) == 1
        assert all(p.subsystem == "browser" for p in browser_probes)

    def test_by_criticality(self):
        r = HealthProbeRegistry()
        r._register(self._make_probe("browser.liveness", criticality="critical"))
        r._register(self._make_probe("api.latency", criticality="informational"))
        critical = r.by_criticality("critical")
        info = r.by_criticality("informational")
        assert len(critical) == 1
        assert len(info) == 1
        assert critical[0].name == "browser.liveness"

    def test_by_interval(self):
        r = HealthProbeRegistry()
        r._register(self._make_probe("a", interval=30.0))
        r._register(self._make_probe("b", interval=60.0))
        r._register(self._make_probe("c", interval=30.0))
        thirties = r.by_interval(30.0)
        sixties = r.by_interval(60.0)
        assert len(thirties) == 2
        assert len(sixties) == 1

    def test_all_results_omits_unrun(self):
        """all_results() excludes probes that haven't recorded a result."""
        r = HealthProbeRegistry()
        p1 = self._make_probe("a")
        p2 = self._make_probe("b")
        p1.last_result = ProbeResult(status="healthy", message="ok",
                                      elapsed_ms=5.0, timestamp="2026-01-01")
        r._register(p1)
        r._register(p2)
        results = r.all_results()
        assert "a" in results
        assert "b" not in results
        assert results["a"]["status"] == "healthy"
        assert results["a"]["elapsed_ms"] == 5.0

    def test_summary_aggregates_correctly(self):
        r = HealthProbeRegistry()
        # 2 healthy, 1 degraded, 1 critical-unhealthy, 1 not-run
        p1 = self._make_probe("a.healthy1")
        p1.last_result = ProbeResult(status="healthy", message="ok")
        p2 = self._make_probe("a.healthy2")
        p2.last_result = ProbeResult(status="healthy", message="ok")
        p3 = self._make_probe("b.degraded", criticality="informational")
        p3.last_result = ProbeResult(status="degraded", message="slow")
        p4 = self._make_probe("c.bad", criticality="critical")
        p4.last_result = ProbeResult(status="unhealthy", message="down")
        p5 = self._make_probe("d.notrun")  # last_result stays None
        for p in (p1, p2, p3, p4, p5):
            r._register(p)

        summary = r.summary()
        assert summary["total"] == 5
        assert summary["healthy"] == 2
        assert summary["degraded"] == 1
        assert summary["unhealthy"] == 1
        assert summary["not_run"] == 1
        assert "c.bad" in summary["critical_failures"]
        # Subsystem breakdown
        assert summary["by_subsystem"]["a"]["healthy"] == 2
        assert summary["by_subsystem"]["b"]["degraded"] == 1
        assert summary["by_subsystem"]["c"]["unhealthy"] == 1
        assert summary["by_subsystem"]["d"]["not_run"] == 1


# ---------------------------------------------------------------------------
# 3. @health_probe decorator
# ---------------------------------------------------------------------------

class TestHealthProbeDecorator:
    @pytest.mark.asyncio
    async def test_decorator_registers_to_global_registry(self):
        """Decorator inserts the probe into probe_registry singleton."""
        before = probe_registry.probe_count

        @health_probe(name="test_decorator.unique_xyz", interval=30, criticality="informational")
        async def my_probe() -> ProbeResult:
            return ProbeResult(status="healthy", message="ok")

        assert probe_registry.probe_count == before + 1
        registered = probe_registry.get("test_decorator.unique_xyz")
        assert registered is not None
        assert registered.subsystem == "test_decorator"
        assert registered.criticality == "informational"
        assert registered.interval_seconds == 30

    @pytest.mark.asyncio
    async def test_decorator_wraps_stamp_elapsed_and_timestamp(self):
        """Wrapper overwrites elapsed_ms + timestamp on the returned ProbeResult."""

        async def stub() -> ProbeResult:
            # Return with bogus elapsed/timestamp — wrapper must overwrite
            return ProbeResult(status="healthy", message="ok",
                               elapsed_ms=999.0, timestamp="OLD")

        wrapper = health_probe(name="test_stamp.unique", interval=60)(stub)
        result = await wrapper()
        assert result.status == "healthy"
        # elapsed_ms is real (not 999.0) and timestamp is overwritten
        assert result.elapsed_ms != 999.0
        assert result.timestamp != "OLD"
        assert result.timestamp.startswith("20")  # ISO date

    @pytest.mark.asyncio
    async def test_decorator_catches_exception(self):
        """If the probe function raises, wrapper returns unhealthy ProbeResult."""

        async def bad_probe() -> ProbeResult:
            raise RuntimeError("boom")

        wrapper = health_probe(name="test_exception.unique", interval=60)(bad_probe)
        result = await wrapper()
        assert result.status == "unhealthy"
        assert "boom" in result.message
        assert result.elapsed_ms >= 0


# ---------------------------------------------------------------------------
# 4. HealthProbeScheduler — bucketing + execution
# ---------------------------------------------------------------------------

class TestHealthProbeScheduler:
    def _make_async_probe(self, name: str, status: str = "healthy",
                          interval: float = 60.0,
                          delay_s: float = 0.0,
                          raise_exc: bool = False) -> _RegisteredProbe:
        async def _stub() -> ProbeResult:
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            if raise_exc:
                raise RuntimeError("simulated probe failure")
            return ProbeResult(status=status, message="stub ok")
        return _RegisteredProbe(
            name=name, description="test", criticality="informational",
            interval_seconds=interval, execute=_stub,
            subsystem=name.split(".")[0] if "." in name else name,
        )

    @pytest.mark.asyncio
    async def test_start_buckets_by_interval(self):
        """start() creates one asyncio.Task per unique interval."""
        r = HealthProbeRegistry()
        r._register(self._make_async_probe("a", interval=30.0))
        r._register(self._make_async_probe("b", interval=30.0))
        r._register(self._make_async_probe("c", interval=60.0))

        sched = HealthProbeScheduler(r)
        # We patch create_task to avoid spawning real loops; close the unawaited
        # coroutine on each call so we don't leak warnings.
        def _capture_and_close(coro, name=None):
            coro.close()  # Cancel without ever awaiting → no RuntimeWarning
            return MagicMock(name=name)

        with patch("naukri_server.health.framework.asyncio.create_task",
                   side_effect=_capture_and_close) as mock_create:
            await sched.start()
            # Two unique intervals (30, 60) => 2 create_task calls
            assert mock_create.call_count == 2
            # Verify they were named correctly via kwargs
            names = [call.kwargs.get("name") for call in mock_create.call_args_list]
            assert "health-30.0s" in names
            assert "health-60.0s" in names
        # cleanup
        sched._tasks = []
        await sched.stop()

    @pytest.mark.asyncio
    async def test_execute_probe_records_result(self):
        """_execute_probe runs the probe and stores the result on the probe."""
        r = HealthProbeRegistry()
        p = self._make_async_probe("a.test", status="degraded")
        r._register(p)
        sched = HealthProbeScheduler(r)
        await sched._execute_probe(p)
        assert p.last_result is not None
        assert p.last_result.status == "degraded"
        assert p.previous_status == "degraded"

    @pytest.mark.asyncio
    async def test_execute_probe_handles_timeout(self):
        """If a probe exceeds its timeout, scheduler records unhealthy."""
        r = HealthProbeRegistry()
        # Tiny interval => tiny timeout (interval * 0.8). Probe sleeps longer.
        p = self._make_async_probe("a.slow", interval=0.1, delay_s=1.0)
        r._register(p)
        sched = HealthProbeScheduler(r)
        await sched._execute_probe(p)
        assert p.last_result.status == "unhealthy"
        assert "timed out" in p.last_result.message.lower()

    @pytest.mark.asyncio
    async def test_execute_probe_handles_exception(self):
        """If a probe raises, scheduler records unhealthy with the error."""
        r = HealthProbeRegistry()
        p = self._make_async_probe("a.crashy", raise_exc=True)
        r._register(p)
        sched = HealthProbeScheduler(r)
        await sched._execute_probe(p)
        # Note: the @health_probe decorator catches exceptions internally,
        # but raw probes (no decorator) bubble. The `execute=_stub` here is raw.
        assert p.last_result.status == "unhealthy"

    @pytest.mark.asyncio
    async def test_state_change_emits_event(self):
        """When status changes, ProbeStateChanged event is emitted via event_bus."""
        r = HealthProbeRegistry()
        p = self._make_async_probe("a.flap", status="healthy")
        # Set previous status so we trigger a change
        p.previous_status = "unhealthy"
        r._register(p)
        sched = HealthProbeScheduler(r)

        with patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            await sched._on_result(p, ProbeResult(status="healthy", message="recovered"))
            assert mock_bus.emit.await_count == 1
            event = mock_bus.emit.await_args[0][0]
            assert event.probe_name == "a.flap"
            assert event.old_status == "unhealthy"
            assert event.new_status == "healthy"

    @pytest.mark.asyncio
    async def test_browser_liveness_failure_triggers_watchdog(self):
        """browser.liveness going unhealthy calls watchdog._handle_failure."""
        r = HealthProbeRegistry()
        p = self._make_async_probe("browser.liveness", status="unhealthy")
        r._register(p)
        mock_watchdog = MagicMock()
        mock_watchdog._handle_failure = AsyncMock()
        sched = HealthProbeScheduler(r, watchdog=mock_watchdog)
        await sched._on_result(p, ProbeResult(status="unhealthy", message="dead"))
        mock_watchdog._handle_failure.assert_awaited_once()
