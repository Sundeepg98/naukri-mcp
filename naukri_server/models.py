"""Domain models — type-safe representations replacing raw strings/dicts."""

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Rich domain entities (behavior-carrying, NOT ORMs)
# ---------------------------------------------------------------------------


@dataclass
class Job:
    """Domain entity for a job listing."""
    job_id: str
    title: str = ""
    company: str = ""
    salary: str = ""
    salary_min_lakhs: Optional[float] = None
    salary_max_lakhs: Optional[float] = None
    location: str = ""
    experience: str = ""
    experience_min: Optional[int] = None
    experience_max: Optional[int] = None
    tags: list = field(default_factory=list)
    is_applied: bool = False
    is_agent_eligible: bool = False
    work_mode: str = ""
    company_rating: Optional[str] = None

    @property
    def salary_disclosed(self) -> bool:
        return self.salary_min_lakhs is not None and self.salary_min_lakhs > 0

    @property
    def experience_range(self) -> str:
        if self.experience_min is not None and self.experience_max is not None:
            return f"{self.experience_min}-{self.experience_max} Yrs"
        return self.experience

    def matches_experience(self, years: float) -> bool:
        if self.experience_min is not None and self.experience_max is not None:
            return self.experience_min <= years <= self.experience_max
        return True

    @classmethod
    def from_api_dict(cls, data: dict) -> "Job":
        """Create Job from Naukri API response dict."""
        return cls(
            job_id=str(data.get("job_id", data.get("jobId", ""))),
            title=data.get("title", ""),
            company=data.get("company", data.get("companyName", "")),
            salary=data.get("salary", "Not Disclosed"),
            salary_min_lakhs=data.get("salary_min_lakhs"),
            salary_max_lakhs=data.get("salary_max_lakhs"),
            location=data.get("location", ""),
            experience=data.get("experience", ""),
            experience_min=data.get("experience_min"),
            experience_max=data.get("experience_max"),
            tags=data.get("tags", []),
            is_applied=data.get("is_applied", False),
            is_agent_eligible=data.get("is_agent_eligible", False),
            work_mode=data.get("work_mode", ""),
            company_rating=data.get("company_rating"),
        )

    def to_dict(self) -> dict:
        """Serialize for API response."""
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Application:
    """Domain entity for a tracked job application."""
    job_id: str
    title: str = ""
    company: str = ""
    status: ApplicationStatus = ApplicationStatus.UNKNOWN
    applied_at: str = ""
    source: str = "manual"
    ars_score: Optional[int] = None
    view_count: Optional[int] = None
    is_open: Optional[bool] = None
    follow_up_priority: int = 50

    @property
    def is_stale(self) -> bool:
        if not self.applied_at:
            return False
        try:
            applied = datetime.fromisoformat(self.applied_at.replace("+00:00", "+00:00"))
            days = (datetime.now(timezone.utc) - applied).days
            return days > 14 and self.status == ApplicationStatus.APPLIED
        except Exception:
            return False

    @property
    def days_since_applied(self) -> int:
        try:
            applied = datetime.fromisoformat(self.applied_at.replace("+00:00", "+00:00"))
            return (datetime.now(timezone.utc) - applied).days
        except Exception:
            return 0

    @property
    def has_recruiter_interest(self) -> bool:
        return (self.view_count or 0) > 0 or (self.ars_score or 0) >= 70

    @classmethod
    def from_dict(cls, data: dict) -> "Application":
        return cls(
            job_id=str(data.get("job_id", "")),
            title=data.get("title", ""),
            company=data.get("company", ""),
            status=ApplicationStatus.from_string(data.get("status", "")),
            applied_at=data.get("applied_at", ""),
            source=data.get("source", "manual"),
            ars_score=data.get("ars_score"),
            view_count=data.get("view_count"),
            is_open=data.get("is_open"),
            follow_up_priority=data.get("follow_up_priority", 50),
        )

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["status"] = self.status.value
        return {k: v for k, v in d.items() if v is not None}
