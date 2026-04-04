"""Domain models — type-safe representations replacing raw strings/dicts."""

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Any, TypedDict, NotRequired


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


# ---------------------------------------------------------------------------
# Response TypedDicts — document the shape of MCP tool return values
# ---------------------------------------------------------------------------


class ErrorResult(TypedDict):
    """Standard error response from any MCP tool."""
    status: str  # "error"
    message: str
    error_code: str  # ErrorCode value


class PaginatedResult(TypedDict):
    """Standard pagination metadata included in list responses."""
    total: int
    count: int
    page: int
    has_more: bool


class ApplicationDict(TypedDict, total=False):
    """Shape of an application record from SQLite/API."""
    job_id: str
    title: str
    company: str
    status: str
    applied_at: str
    source: str
    ars_score: int
    star_rating: str
    job_activity: int
    company_rating: str
    is_open: bool
    view_count: int
    follow_up_priority: int
    last_synced: str
    is_stale: bool
    days_since_applied: int
    has_recruiter_interest: bool


class JobDict(TypedDict, total=False):
    """Shape of a parsed job listing from Naukri API."""
    job_id: str
    title: str
    company: str
    salary: str
    salary_min_lakhs: float
    salary_max_lakhs: float
    location: str
    experience: str
    experience_min: int
    experience_max: int
    tags: list[str]
    is_applied: bool
    posted_date: str
    url: str
    work_mode: str
    company_rating: str
    company_reviews_count: int
    vacancies: int
    apply_count: int
    is_agent_eligible: bool
    group_id: str
    company_id: str


class SavedJobDict(TypedDict, total=False):
    """Shape of a saved/bookmarked job record."""
    job_id: str
    title: str
    company: str
    notes: str
    saved_at: str
    source: str


class ReminderDict(TypedDict, total=False):
    """Shape of a follow-up reminder record."""
    job_id: str
    title: str
    company: str
    remind_at: str
    note: str
    is_due: bool
    days_until_due: int
    created_at: str


class InterviewRoundDict(TypedDict, total=False):
    """Shape of an interview round record."""
    id: int
    job_id: str
    round_type: str  # phone_screen, technical, system_design, hr, offer, other
    date: str
    notes: str
    status: str  # scheduled, completed, cancelled
    created_at: str


class FitScoreResult(TypedDict):
    """Shape of a fit score computation result."""
    overall_score: int
    skill_match: dict  # {matched: [...], missing: [...], match_pct: float}
    experience_match: dict
    recommendation: str
    bonuses: NotRequired[dict]
    reasons: NotRequired[list[str]]


class StaleApplicationDict(TypedDict, total=False):
    """Shape of a stale application detection result."""
    job_id: str
    title: str
    company: str
    stale_score: int
    follow_up_priority: int
    reasons: list[str]
    recommendation: str
    applied_date: str
    is_open: bool
    view_count: int
    job_activity: int
    ars_score: int


class ApplicationDetailResult(TypedDict, total=False):
    """Shape of detailed application status from Naukri API."""
    status: str
    job_id: str
    title: str
    company: str
    location: str
    is_open: bool
    total_applicants: int
    recruiter_activity: int
    match_rating: int
    status_timeline: list[dict]
    current_status: str
    application_date: str
    view_count: int
    ars_score: int
    star_rating: int
    screening_questions: list[dict]
    recruiter: dict
    local_tracking: dict


class ProfileResult(TypedDict, total=False):
    """Shape of profile data from Naukri API."""
    status: str
    name: str
    email: str
    phone: str
    current_location: str
    total_experience: str
    current_ctc: str
    expected_ctc: str
    notice_period: str
    key_skills: list[str]
    headline: str
    profile_score: int
