"""Request correlation -- traces operations across layers."""

import uuid
import contextvars
import logging

# Context variable for the current request ID
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar('request_id', default='')


def get_request_id() -> str:
    """Get current request's correlation ID."""
    return _request_id.get() or ''


def set_request_id(rid: str = None) -> str:
    """Set correlation ID for current context. Auto-generates if not provided."""
    rid = rid or uuid.uuid4().hex[:8]
    _request_id.set(rid)
    return rid


class CorrelationFilter(logging.Filter):
    """Adds request_id to all log records."""

    def filter(self, record):
        record.request_id = get_request_id()
        return True
