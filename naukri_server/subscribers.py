"""Event subscribers — reactive side effects triggered by domain events.

Registered at server startup via import in __init__.py.
All handlers are async, fire-and-forget, and isolated (exceptions don't propagate).
"""

import logging
from datetime import datetime, timezone

from naukri_server.events import (
    event_bus, ApplicationSubmitted, ApplicationStatusChanged,
    ApplicationStale, ApplicationInterviewScheduled, SyncCompleted, ReminderDue,
)

logger = logging.getLogger(__name__)


async def _on_application_submitted(event: ApplicationSubmitted):
    """Auto-set 7-day follow-up reminder on successful apply."""
    if not event.job_id:
        return
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ApplicationSubmitted",
            "title": f"Applied to {event.title or 'job'} at {event.company or 'company'}",
            "body": f"Application submitted for job {event.job_id}. Follow up in 7 days.",
            "priority": "medium",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id, "company": event.company},
        })
        logger.info("Notification stored for ApplicationSubmitted: %s", event.job_id)
    except Exception as e:
        logger.warning("Subscriber _on_application_submitted failed: %s", e)


async def _on_status_change(event: ApplicationStatusChanged):
    """Store notification on meaningful status changes."""
    if not event.job_id or not event.new_status:
        return
    try:
        from naukri_server.domain.application import StatusTransition
        transition = StatusTransition(old_status=event.old_status, new_status=event.new_status)

        priority = "high" if transition.is_positive else "medium"
        title = f"{event.company or 'Company'}: {event.old_status} → {event.new_status}"

        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ApplicationStatusChanged",
            "title": title,
            "body": f"Application for {event.title or event.job_id} changed status",
            "priority": priority,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "job_id": event.job_id,
                "old_status": event.old_status,
                "new_status": event.new_status,
                "is_positive": transition.is_positive,
            },
        })
    except Exception as e:
        logger.warning("Subscriber _on_status_change failed: %s", e)


async def _on_application_stale(event: ApplicationStale):
    """Store notification for high-priority stale applications."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ApplicationStale",
            "title": f"Stale: {event.company or 'Company'} - needs follow-up",
            "body": f"Application {event.job_id} has been stale. Consider following up or moving on.",
            "priority": "high" if event.follow_up_priority >= 70 else "medium",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id, "company": event.company},
        })
    except Exception as e:
        logger.warning("Subscriber _on_application_stale failed: %s", e)


async def _on_interview_scheduled(event: ApplicationInterviewScheduled):
    """Auto-generate interview prep when round is scheduled."""
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "ApplicationInterviewScheduled",
            "title": f"Interview: {event.round_type} at {event.company or 'company'}",
            "body": f"Interview round scheduled for job {event.job_id}. Use naukri_interview_prep to prepare.",
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": event.job_id, "round_type": event.round_type, "date": event.date},
        })
    except Exception as e:
        logger.warning("Subscriber _on_interview_scheduled failed: %s", e)


async def _on_sync_completed(event: SyncCompleted):
    """Store notification if sync found changes."""
    if event.status_changes_count == 0 and event.new_added == 0:
        return  # No changes, no notification
    try:
        from naukri_server.database import store_notification
        parts = []
        if event.new_added:
            parts.append(f"{event.new_added} new")
        if event.status_changes_count:
            parts.append(f"{event.status_changes_count} status changes")
        await store_notification({
            "event_type": "SyncCompleted",
            "title": f"Sync: {', '.join(parts)}",
            "body": f"Synced {event.entity}: {event.new_added} new, {event.updated} updated",
            "priority": "low",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.warning("Subscriber _on_sync_completed failed: %s", e)


# ---------------------------------------------------------------------------
# Register all subscribers with the global EventBus
# ---------------------------------------------------------------------------

def register_all():
    """Register all subscribers. Called once at server startup."""
    event_bus.subscribe(ApplicationSubmitted, _on_application_submitted)
    event_bus.subscribe(ApplicationStatusChanged, _on_status_change)
    event_bus.subscribe(ApplicationStale, _on_application_stale)
    event_bus.subscribe(ApplicationInterviewScheduled, _on_interview_scheduled)
    event_bus.subscribe(SyncCompleted, _on_sync_completed)
    logger.info("Registered %d reactive subscribers", 5)


# Auto-register on import
register_all()
