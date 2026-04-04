"""Domain models — type-safe representations replacing raw strings/dicts."""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Any


class ApplicationStatus(Enum):
    """Canonical application statuses used across tracking, sync, and apply."""
    APPLIED = "applied"
    NEEDS_INPUT = "needs_input"
    ALREADY_APPLIED = "already_applied"
    VIEWED = "viewed"
    VIEWED_BY_RECRUITER = "viewed_by_recruiter"
    SHORTLISTED = "shortlisted"
    INTERVIEW = "interview"
    OFFERED = "offered"
    REJECTED = "rejected"
    ERROR = "error"
    UNKNOWN = "unknown"

    @classmethod
    def from_string(cls, value: str) -> "ApplicationStatus":
        """Parse a status string, returning UNKNOWN for unrecognized values."""
        if not value:
            return cls.UNKNOWN
        normalized = value.lower().strip().replace(" ", "_").replace("-", "_")
        for member in cls:
            if member.value == normalized:
                return member
        return cls.UNKNOWN


class ErrorCode(Enum):
    """Standardized error codes for all MCP tool responses."""
    API_ERROR = "API_ERROR"
    BROWSER_ERROR = "BROWSER_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    AUTH_ERROR = "AUTH_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    TIMEOUT = "TIMEOUT"


def validate_action_params(action: str, provided: dict, valid_per_action: dict) -> list:
    """Return list of parameter names that are irrelevant for this action.

    Does NOT raise — just returns warnings for transparency.
    Helps enforce Interface Segregation within consolidated tools:
    callers learn which params actually matter for each action.

    Args:
        action: The action/insight_type being invoked.
        provided: Dict of all parameter values (e.g. from locals()).
        valid_per_action: Mapping of action -> set of valid param names.

    Returns:
        List of parameter names that were provided (non-None) but are
        not relevant for the chosen action. Empty list if all params valid.
    """
    if action not in valid_per_action:
        return []
    valid = valid_per_action[action]
    unused = [k for k, v in provided.items() if v is not None and k not in valid and k != "action"]
    return unused


def paginate(items: list, page: int = 1, limit: int = 50) -> tuple:
    """Standard pagination helper — returns consistent {total, count, page, has_more, items}.

    Use this instead of repeating pagination logic in every tool.

    Returns:
        Tuple of (pagination_dict, page_items) where pagination_dict contains
        {total, count, page, has_more}.
    """
    total = len(items)
    offset = (page - 1) * limit
    page_items = items[offset:offset + limit]
    return {
        "total": total,
        "count": len(page_items),
        "page": page,
        "has_more": (offset + limit) < total,
    }, page_items
