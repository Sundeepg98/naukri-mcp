"""Use cases — explicit business operations following Clean Architecture."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SearchJobsCommand:
    """Command to search for jobs."""
    keywords: str
    location: Optional[str] = None
    experience: Optional[int] = None
    salary_min: Optional[int] = None
    limit: int = 20


@dataclass
class ApplyToJobCommand:
    """Command to apply to a specific job."""
    job_id: str
    answers: Optional[dict] = None
    set_reminder_days: Optional[int] = None


@dataclass
class AssessFitCommand:
    """Command to assess fit for a job without applying."""
    job_id: str
    min_fit_score: int = 60
