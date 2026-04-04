"""Saga pattern -- orchestrates multi-step workflows with compensation on failure."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Any

logger = logging.getLogger(__name__)


@dataclass
class SagaStep:
    """A single step in a saga workflow."""
    name: str
    execute: Callable  # async fn() -> result
    compensate: Optional[Callable] = None  # async fn() -> None (rollback)


class SagaExecutor:
    """Executes saga steps sequentially with compensation on failure.

    If step N fails, compensates steps N-1, N-2, ..., 1 in reverse order.
    """

    def __init__(self, name: str):
        self.name = name
        self.steps: list[SagaStep] = []
        self.completed: list[tuple[str, Any]] = []
        self.errors: list[str] = []

    def add_step(self, name: str, execute: Callable, compensate: Callable = None):
        self.steps.append(SagaStep(name=name, execute=execute, compensate=compensate))
        return self

    async def run(self) -> dict:
        """Execute all steps. On failure, compensate completed steps in reverse."""
        for step in self.steps:
            try:
                result = await step.execute()
                self.completed.append((step.name, result))
                logger.info("Saga '%s' step '%s' succeeded", self.name, step.name)
            except Exception as e:
                logger.error("Saga '%s' step '%s' failed: %s", self.name, step.name, e)
                self.errors.append(f"{step.name}: {type(e).__name__}: {e}")

                # Compensate completed steps in reverse
                for comp_name, _ in reversed(self.completed):
                    comp_step = next((s for s in self.steps if s.name == comp_name), None)
                    if comp_step and comp_step.compensate:
                        try:
                            await comp_step.compensate()
                            logger.info("Saga '%s' compensated '%s'", self.name, comp_name)
                        except Exception as ce:
                            logger.warning("Saga '%s' compensation '%s' failed: %s", self.name, comp_name, ce)

                return {
                    "status": "error",
                    "saga": self.name,
                    "failed_step": step.name,
                    "completed_steps": [name for name, _ in self.completed],
                    "errors": self.errors,
                }

        return {
            "status": "success",
            "saga": self.name,
            "completed_steps": [name for name, _ in self.completed],
            "results": {name: result for name, result in self.completed},
        }
