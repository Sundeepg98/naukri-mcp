"""Application domain objects — staleness scoring, follow-up priority, and status transitions."""

from dataclasses import dataclass
from datetime import datetime, timezone

from naukri_server.config import STALE_THRESHOLD_DAYS


@dataclass(frozen=True)
class StalenessReport:
    """Immutable value object for staleness scoring of a job application.

    Encapsulates the 5-signal staleness scoring logic:
    1. Job closed (is_open == False)
    2. Applied long ago with no views
    3. Zero recruiter activity on posting
    4. Low ARS match score
    5. Recruiter inactive for extended period
    """
    score: int        # 0-100
    reasons: list[str]
    recommendation: str

    @classmethod
    def compute(cls, app: dict,
                days_threshold: int = STALE_THRESHOLD_DAYS) -> "StalenessReport":
        """Compute staleness from an application dict using the 5-signal model.

        Args:
            app: Application dict with keys: is_open, view_count, days_since_applied,
                 job_activity, ars_score, job_activity_date.
            days_threshold: Days after which an unviewed application is considered stale.

        Returns:
            StalenessReport with score (0-100), reasons list, and recommendation string.
        """
        days_since_apply = int(app.get("days_since_applied", 0))
        now = datetime.now(timezone.utc)

        stale_score = 0
        reasons = []

        # Signal 1: Job closed
        is_open = app.get("is_open")
        if is_open is False:
            stale_score += 100
            reasons.append("Job closed")

        # Signal 2: Applied long ago with no views / applied very long ago
        view_count = app.get("view_count")
        has_been_viewed = view_count and view_count > 0
        if days_since_apply > days_threshold and not has_been_viewed:
            stale_score += 60
            reasons.append(f"Applied {days_since_apply}d ago, never viewed")
        elif days_since_apply > days_threshold * 2:
            stale_score += 30
            reasons.append(f"Applied {days_since_apply}d ago")

        # Signal 3: Zero recruiter activity on posting
        job_activity = app.get("job_activity")
        if job_activity is not None and job_activity == 0:
            stale_score += 30
            reasons.append("Zero recruiter activity on posting")

        # Signal 4: Low ARS match score
        ars_score = app.get("ars_score")
        if ars_score is not None and ars_score < 30:
            stale_score += 20
            reasons.append(f"Low match score ({ars_score}%)")

        # Signal 5: Recruiter inactive for extended period
        job_activity_date = app.get("job_activity_date")
        if job_activity_date:
            try:
                if "T" in str(job_activity_date):
                    act_dt = datetime.fromisoformat(str(job_activity_date).replace("Z", "+00:00"))
                else:
                    act_dt = datetime.strptime(str(job_activity_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days_since_activity = (now - act_dt).days
                if days_since_activity > 14:
                    stale_score += 25
                    reasons.append(f"Recruiter inactive for {days_since_activity}d")
            except (ValueError, TypeError):
                pass

        # Recommendation based on composite score
        if stale_score >= 100:
            recommendation = "Move on \u2014 job is closed or very stale"
        elif stale_score >= 60:
            recommendation = "Follow up or move on"
        elif stale_score >= 40:
            recommendation = "Consider following up"
        else:
            recommendation = "Still active \u2014 wait"

        return cls(
            score=min(stale_score, 100),
            reasons=reasons,
            recommendation=recommendation,
        )


@dataclass(frozen=True)
class StatusTransition:
    """Immutable value object classifying an application status transition.

    Encapsulates the POSITIVE_TRANSITIONS set from insights_service to determine
    whether a status change represents forward progress in the hiring pipeline.
    """
    old_status: str
    new_status: str

    POSITIVE_TRANSITIONS = frozenset({
        ("applied", "viewed"), ("applied", "viewed_by_recruiter"),
        ("applied", "interview"), ("applied", "shortlisted"),
        ("viewed", "interview"), ("viewed", "shortlisted"),
        ("viewed_by_recruiter", "interview"), ("viewed_by_recruiter", "shortlisted"),
        ("interview", "offered"), ("interview", "hired"),
        ("shortlisted", "offered"), ("shortlisted", "hired"),
        ("shortlisted", "interview"),
    })

    @property
    def is_positive(self) -> bool:
        """True if this transition represents forward progress in the hiring pipeline."""
        return (self.old_status, self.new_status) in self.POSITIVE_TRANSITIONS

    @property
    def transition_type(self) -> str:
        """Classify as 'positive' or 'neutral'."""
        return "positive" if self.is_positive else "neutral"


def compute_follow_up_priority(app: dict) -> int:
    """Score how worthwhile it is to follow up on this application (0-100).

    5-factor scoring model:
    1. Job activity (+20 if recruiter is active on the posting)
    2. ARS match score (+5/+10/+15 based on tier)
    3. Company rating (+5/+10 from AmbitionBox)
    4. Recency penalty (-10/-20 for very old applications)
    5. Base score of 50

    Args:
        app: Application dict with keys: job_activity, ars_score,
             company_rating, applied_at.

    Returns:
        Priority score clamped to 0-100.
    """
    priority = 50

    # Factor 1: Job activity
    job_activity = app.get("job_activity", 0)
    if isinstance(job_activity, (int, float)) and job_activity > 0:
        priority += 20

    # Factor 2: ARS match score
    ars = app.get("ars_score")
    if isinstance(ars, (int, float)):
        if ars >= 70:
            priority += 15
        elif ars >= 50:
            priority += 10
        elif ars >= 30:
            priority += 5

    # Factor 3: Company rating
    rating = app.get("company_rating")
    if isinstance(rating, dict):
        rating = rating.get("AggregateRating")
    if rating:
        try:
            r = float(rating)
            if r >= 4.0:
                priority += 10
            elif r >= 3.5:
                priority += 5
        except (ValueError, TypeError):
            pass

    # Factor 4: Recency penalty
    try:
        applied = datetime.fromisoformat(
            app.get("applied_at", "").replace("+00:00", "+00:00")
        )
        days = (datetime.now(timezone.utc) - applied).days
        if days > 60:
            priority -= 20
        elif days > 45:
            priority -= 10
    except Exception:
        pass

    return max(0, min(100, priority))
