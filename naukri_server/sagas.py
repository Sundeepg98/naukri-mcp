"""Saga pattern -- orchestrates multi-step workflows with compensation on failure."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Any

logger = logging.getLogger(__name__)

# Default per-step timeout in seconds
DEFAULT_STEP_TIMEOUT = 30


@dataclass
class SagaStep:
    """A single step in a saga workflow."""
    name: str
    execute: Callable  # async fn() -> result
    compensate: Optional[Callable] = None  # async fn() -> None (rollback)
    timeout: float = DEFAULT_STEP_TIMEOUT  # per-step timeout in seconds


class SagaExecutor:
    """Executes saga steps sequentially with compensation on failure.

    If step N fails, compensates steps N-1, N-2, ..., 1 in reverse order.
    Each completed step records its elapsed time in milliseconds.
    Per-step timeout enforcement prevents runaway steps.
    State is tracked after each step for observability and persistence.
    """

    def __init__(self, name: str):
        self.name = name
        self.steps: list[SagaStep] = []
        self.completed: list[tuple[str, Any, int]] = []  # (name, result, elapsed_ms)
        self.errors: list[str] = []
        self.state: dict = {
            "saga_name": name,
            "started_at": None,
            "steps_completed": [],
            "current_step": None,
            "status": "pending",  # pending, running, completed, failed, compensating
        }

    def add_step(self, name: str, execute: Callable, compensate: Callable = None,
                 timeout: float = DEFAULT_STEP_TIMEOUT):
        self.steps.append(SagaStep(name=name, execute=execute, compensate=compensate, timeout=timeout))
        return self

    async def _run_step(self, step: SagaStep) -> Any:
        """Execute a single step with timeout enforcement."""
        try:
            return await asyncio.wait_for(step.execute(), timeout=step.timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Saga step '{step.name}' timed out after {step.timeout}s")

    async def run(self) -> dict:
        """Execute all steps. On failure, compensate completed steps in reverse."""
        self.state["started_at"] = datetime.now(timezone.utc).isoformat()
        self.state["status"] = "running"

        for step in self.steps:
            self.state["current_step"] = step.name
            t0 = time.monotonic()
            try:
                result = await self._run_step(step)
                elapsed = int((time.monotonic() - t0) * 1000)
                self.completed.append((step.name, result, elapsed))
                self.state["steps_completed"].append(step.name)
                logger.info("Saga '%s' step '%s' completed in %dms", self.name, step.name, elapsed)
            except Exception as e:
                elapsed = int((time.monotonic() - t0) * 1000)
                logger.error("Saga '%s' step '%s' failed after %dms: %s", self.name, step.name, elapsed, e)
                self.errors.append(f"{step.name}: {type(e).__name__}: {e}")
                self.state["status"] = "compensating"
                self.state["current_step"] = None

                # Compensate completed steps in reverse
                for comp_name, _, _ in reversed(self.completed):
                    comp_step = next((s for s in self.steps if s.name == comp_name), None)
                    if comp_step and comp_step.compensate:
                        try:
                            await comp_step.compensate()
                            logger.info("Saga '%s' compensated '%s'", self.name, comp_name)
                        except Exception as ce:
                            logger.warning("Saga '%s' compensation '%s' failed: %s", self.name, comp_name, ce)

                self.state["status"] = "failed"
                return {
                    "status": "error",
                    "saga": self.name,
                    "failed_step": step.name,
                    "completed_steps": [name for name, _, _ in self.completed],
                    "step_timings": {name: ms for name, _, ms in self.completed},
                    "errors": self.errors,
                    "state": dict(self.state),
                }

        self.state["status"] = "completed"
        self.state["current_step"] = None
        return {
            "status": "success",
            "saga": self.name,
            "completed_steps": [name for name, _, _ in self.completed],
            "step_timings": {name: ms for name, _, ms in self.completed},
            "results": {name: result for name, result, _ in self.completed},
            "state": dict(self.state),
        }
