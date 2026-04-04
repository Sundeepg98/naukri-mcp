"""Event-driven pub/sub — domain events with async subscriber dispatch."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Any

logger = logging.getLogger(__name__)


@dataclass
class DomainEvent:
    """Base class for all domain events."""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ApplicationStatusChanged(DomainEvent):
    """Emitted when an application's status changes."""
    job_id: str = ""
    company: str = ""
    title: str = ""
    old_status: str = ""
    new_status: str = ""


@dataclass
class ApplicationSubmitted(DomainEvent):
    """Emitted when a new job application is submitted."""
    job_id: str = ""
    company: str = ""
    title: str = ""
    fit_score: int = 0


@dataclass
class RecruiterEngaged(DomainEvent):
    """Emitted when a recruiter takes action on your profile."""
    recruiter_name: str = ""
    company: str = ""
    action: str = ""  # VIEWED, DOWNLOADED, CONTACTED


@dataclass
class ApplicationStale(DomainEvent):
    """Emitted when an application becomes stale (no response for N days)."""
    job_id: str = ""
    company: str = ""
    days_since_applied: int = 0
    follow_up_priority: int = 0


class EventBus:
    """Simple async event bus — register subscribers, emit events."""

    def __init__(self):
        self._subscribers: dict[type, list[Callable]] = {}
        self._event_log: list[DomainEvent] = []
        self._max_log_size = 100

    def subscribe(self, event_type: type, handler: Callable):
        """Register an async handler for an event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: type, handler: Callable):
        """Remove a handler."""
        if event_type in self._subscribers:
            self._subscribers[event_type] = [h for h in self._subscribers[event_type] if h != handler]

    async def emit(self, event: DomainEvent):
        """Emit an event to all registered subscribers. Failures are logged, not raised."""
        self._event_log.append(event)
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size:]

        handlers = self._subscribers.get(type(event), [])
        if not handlers:
            return

        results = await asyncio.gather(
            *[self._safe_call(h, event) for h in handlers],
            return_exceptions=True,
        )

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning("Event handler %s failed for %s: %s",
                             handlers[i].__name__, type(event).__name__, result)

    async def _safe_call(self, handler, event):
        """Call handler, catching all exceptions."""
        return await handler(event)

    def get_log(self, event_type: type = None, limit: int = 20) -> list:
        """Get recent events, optionally filtered by type."""
        events = self._event_log
        if event_type:
            events = [e for e in events if isinstance(e, event_type)]
        return events[-limit:]

    @property
    def subscriber_count(self) -> int:
        return sum(len(handlers) for handlers in self._subscribers.values())


# Global event bus singleton
event_bus = EventBus()


# Default subscribers — log significant events
async def _log_status_change(event: ApplicationStatusChanged):
    logger.info("Status changed: %s at %s: %s → %s",
                event.job_id, event.company, event.old_status, event.new_status)


async def _log_application_submitted(event: ApplicationSubmitted):
    logger.info("Applied: %s at %s (fit: %s)", event.job_id, event.company, event.fit_score)


event_bus.subscribe(ApplicationStatusChanged, _log_status_change)
event_bus.subscribe(ApplicationSubmitted, _log_application_submitted)
