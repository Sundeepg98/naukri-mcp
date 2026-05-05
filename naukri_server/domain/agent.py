"""Agent domain objects — encapsulate autonomous agent business rules."""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


# IST offset (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class AgentBlocklist:
    """Value object for company/title blocklist rules.

    Encapsulates blocklist matching logic.
    """
    companies: tuple[str, ...] = ()
    title_keywords: tuple[str, ...] = ()
    enabled: bool = True

    @classmethod
    def from_config(cls, config: dict) -> "AgentBlocklist":
        """Create from config dict."""
        bl = config.get("blocklist", {})
        return cls(
            companies=tuple(bl.get("companies", [])),
            title_keywords=tuple(bl.get("title_keywords", [])),
            enabled=bl.get("enabled", True),
        )

    def check(self, company: str, title: str) -> Optional[str]:
        """Check if job matches blocklist. Returns reason string or None."""
        if not self.enabled:
            return None
        company_lower = (company or "").lower()
        title_lower = (title or "").lower()
        for blocked in self.companies:
            if blocked.lower() in company_lower:
                return f"company_blocklist:{blocked}"
        for keyword in self.title_keywords:
            if keyword.lower() in title_lower:
                return f"title_blocklist:{keyword}"
        return None


@dataclass(frozen=True)
class QuietHours:
    """Value object for quiet hours schedule.

    Handles midnight-spanning windows (e.g., 20:00-08:00).
    """
    enabled: bool = True
    start_hour: int = 20  # IST
    end_hour: int = 8     # IST

    @classmethod
    def from_config(cls, config: dict) -> "QuietHours":
        """Create from config dict."""
        qh = config.get("quiet_hours", {})
        return cls(
            enabled=qh.get("enabled", True),
            start_hour=qh.get("start_hour", 20),
            end_hour=qh.get("end_hour", 8),
        )

    @property
    def is_active(self) -> bool:
        """Check if current IST time is within quiet hours."""
        if not self.enabled:
            return False
        now_ist = datetime.now(IST)
        if self.start_hour > self.end_hour:  # Spans midnight
            return now_ist.hour >= self.start_hour or now_ist.hour < self.end_hour
        else:  # Same day
            return self.start_hour <= now_ist.hour < self.end_hour


@dataclass(frozen=True)
class AgentCandidate:
    """Value object for a job candidate selected by the agent.

    Immutable — created during decide step, consumed by act step.
    """
    job_id: str
    company: str
    title: str
    fit_score: int
    search_name: str

    @classmethod
    def from_hunt_result(cls, job: dict, search_name: str) -> "AgentCandidate":
        """Factory from auto_hunt ranked_jobs entry."""
        return cls(
            job_id=str(job.get("job_id", "")),
            company=job.get("company", ""),
            title=job.get("title", ""),
            fit_score=job.get("fit_score", 0),
            search_name=search_name,
        )


@dataclass(frozen=True)
class AgentDecisionRecord:
    """Value object for an agent's decision about a job.

    Records what the agent decided and why.
    """
    job_id: str
    company: str
    title: str
    search_query: str
    fit_score: int
    decision: str  # "apply" or "skip"
    skip_reason: Optional[str] = None
    apply_status: Optional[str] = None  # "pending", "applied", "error", "rejected"


@dataclass
class AgentCycleResult:
    """Mutable aggregate for tracking agent cycle results.

    Accumulates metrics across observe→decide→act→learn.
    """
    cycle_id: str
    mode: str
    searches_run: int = 0
    total_found: int = 0
    total_matched: int = 0
    applied_count: int = 0
    skipped_count: int = 0
    candidates: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    step_timings: dict = field(default_factory=dict)

    def to_summary(self) -> dict:
        """Convert to API response dict."""
        return {
            "cycle_id": self.cycle_id,
            "mode": self.mode,
            "applied": self.applied_count,
            "skipped": self.skipped_count,
            "searches_run": self.searches_run,
            "total_found": self.total_found,
            "total_matched": self.total_matched,
            "errors": self.errors[:5],
            "step_timings": self.step_timings,
        }


def validate_agent_config(config: dict) -> list[str]:
    """Validate agent config. Returns list of errors (empty = valid).

    Validates mode, daily limits, fit score bounds, search definitions,
    and quiet hours settings.
    """
    errors = []
    if not isinstance(config.get("mode"), str) or config["mode"] not in ("dry_run", "approval", "auto"):
        errors.append(f"Invalid mode: {config.get('mode')} (must be dry_run/approval/auto)")
    max_daily = config.get("max_daily_applications")
    if not isinstance(max_daily, int) or not 1 <= max_daily <= 100:
        errors.append("max_daily_applications must be 1-100")
    min_fit = config.get("min_fit_score")
    if not isinstance(min_fit, int) or not 0 <= min_fit <= 100:
        errors.append("min_fit_score must be 0-100")
    searches = config.get("searches")
    if not isinstance(searches, list) or not searches:
        errors.append("searches must be a non-empty list")
    else:
        for i, s in enumerate(searches):
            if not s.get("keywords"):
                errors.append(f"Search {i}: keywords required")
    qh = config.get("quiet_hours", {})
    if qh.get("enabled"):
        if not 0 <= qh.get("start_hour", 0) <= 23:
            errors.append("quiet_hours.start_hour must be 0-23")
        if not 0 <= qh.get("end_hour", 0) <= 23:
            errors.append("quiet_hours.end_hour must be 0-23")
    return errors
