"""Unified decorator-based registry — subscribers, scheduled tasks, action dispatchers.

Pattern lifted from naukri_server/health/framework.py (proven with 20 health probes).
Single source of truth for three patterns:
  - @subscriber(EventType) — auto-registers event handler
  - @scheduled_task(name, interval_seconds, ...) — auto-registers scheduled task
  - @action_dispatcher(tool, actions, required_params=...) — auto-registers tool action
"""
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Iterable, Any

logger = logging.getLogger(__name__)


@dataclass
class _RegisteredItem:
    kind: str           # "subscriber" | "scheduled_task" | "action"
    key: Any            # event-type | task-name | (tool_name, action)
    fn: Callable
    metadata: dict = field(default_factory=dict)


class _Registry:
    """Single source of truth — three buckets keyed by kind."""

    def __init__(self):
        self._items: dict[str, list[_RegisteredItem]] = {
            "subscriber": [],
            "scheduled_task": [],
            "action": [],
        }

    def add(self, item: _RegisteredItem):
        self._items[item.kind].append(item)

    def by_kind(self, kind: str) -> list[_RegisteredItem]:
        return list(self._items[kind])

    def actions_for(self, tool: str) -> dict[str, _RegisteredItem]:
        """Return {action_name: item} for a given tool."""
        return {it.key[1]: it for it in self._items["action"] if it.key[0] == tool}

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._items.items()}


registry = _Registry()


# ============================================================================
# Decorators
# ============================================================================

def subscriber(event_type: type) -> Callable:
    """Register an event handler. Equivalent to event_bus.subscribe()."""
    def deco(fn):
        registry.add(_RegisteredItem("subscriber", event_type, fn, {}))
        return fn
    return deco


def scheduled_task(
    name: str,
    interval_seconds: float,
    *,
    description: str = "",
    timeout_seconds: float = 120.0,
    run_at_hour: Optional[int] = None,
    enabled: bool = True,
) -> Callable:
    """Register a scheduled task. Equivalent to ScheduledTask(...) in TASK_DEFINITIONS."""
    def deco(fn):
        meta = {
            "name": name,
            "interval_seconds": interval_seconds,
            "description": description,
            "timeout_seconds": timeout_seconds,
            "run_at_hour": run_at_hour,
            "enabled": enabled,
        }
        registry.add(_RegisteredItem("scheduled_task", name, fn, meta))
        return fn
    return deco


def action_dispatcher(
    tool: str,
    actions: Iterable[str],
    *,
    required_params: Optional[dict[str, set[str]]] = None,
) -> Callable:
    """Register a function as the handler for one or more actions of a tool.

    Example:
        @action_dispatcher("applications", actions=["detail"], required_params={"detail": {"job_id"}})
        async def _h_detail(**kw): return await _get_application_detail(kw["job_id"])
    """
    def deco(fn):
        for action in actions:
            meta = {"required": (required_params or {}).get(action, set())}
            registry.add(_RegisteredItem("action", (tool, action), fn, meta))
        return fn
    return deco


# ============================================================================
# Wiring helpers (called once at startup)
# ============================================================================

def wire_subscribers(event_bus) -> int:
    """Subscribe all @subscriber-decorated handlers to the event bus."""
    items = registry.by_kind("subscriber")
    for it in items:
        event_bus.subscribe(it.key, it.fn)
    return len(items)


def wire_scheduled_tasks(scheduler) -> int:
    """Register all @scheduled_task-decorated functions with the scheduler."""
    from naukri_server.scheduler import ScheduledTask
    items = registry.by_kind("scheduled_task")
    for it in items:
        scheduler.register(ScheduledTask(fn=it.fn, **it.metadata))
    return len(items)


async def dispatch_action(tool: str, action: str, kwargs: dict, action_name: str) -> dict:
    """Drop-in replacement for hand-written if-elif dispatch tails.

    Looks up the @action_dispatcher-decorated handler for (tool, action),
    validates required params, and invokes via handle_tool_action for
    standardized error handling.
    """
    from naukri_server.error_handler import handle_tool_action
    handlers = registry.actions_for(tool)
    handler = handlers.get(action)
    if not handler:
        return {
            "status": "error",
            "error_code": "VALIDATION_ERROR",
            "message": f"Unknown action '{action}'. Use: {', '.join(sorted(handlers))}",
        }
    required = handler.metadata.get("required", set())
    missing = [p for p in required if not kwargs.get(p)]
    if missing:
        return {
            "status": "error",
            "error_code": "VALIDATION_ERROR",
            "message": f"{action} requires {', '.join(missing)}.",
        }
    return await handle_tool_action(lambda: handler.fn(**kwargs), action_name)
