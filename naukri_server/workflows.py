"""Reactive workflows -- multi-step sagas triggered by events.

Each workflow factory creates a pre-configured SagaExecutor for a specific
domain scenario. Workflows use domain objects as primary types.
"""

import logging
from datetime import datetime, timezone

from naukri_server.sagas import SagaExecutor

logger = logging.getLogger(__name__)


async def interview_lifecycle_workflow(job_id: str, company: str = "", round_type: str = "") -> dict:
    """Multi-step workflow when an interview is scheduled.

    Steps:
    1. Generate interview prep package
    2. Set 3-day follow-up reminder
    3. Store notification
    """
    saga = SagaExecutor(f"interview_lifecycle_{job_id}")

    prep_result = {}

    async def step_prep():
        nonlocal prep_result
        from naukri_server.services.application_service import interview_prep
        prep_result = await interview_prep(job_id)
        return prep_result

    async def step_reminder():
        from naukri_server.tools.reminders import _set_reminder
        return await _set_reminder(
            job_id=job_id, days=3,
            note=f"Follow up on {round_type} interview at {company}",
        )

    async def step_notify():
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "InterviewLifecycle",
            "title": f"Interview prep ready: {company}",
            "body": f"Prep package generated for {round_type} at {company}. Reminder set for 3 days.",
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": job_id, "round_type": round_type},
        })
        return {"notified": True}

    saga.add_step("interview_prep", step_prep)
    saga.add_step("follow_up_reminder", step_reminder)
    saga.add_step("notification", step_notify)

    return await saga.run()


async def stale_follow_up_workflow(job_id: str, company: str = "") -> dict:
    """Multi-step workflow for high-priority stale applications.

    Steps:
    1. Draft follow-up message
    2. Store notification with draft attached
    """
    saga = SagaExecutor(f"stale_follow_up_{job_id}")

    draft_result = {}

    async def step_draft():
        nonlocal draft_result
        from naukri_server.services.application_service import draft_follow_up
        draft_result = await draft_follow_up(job_id)
        return draft_result

    async def step_notify():
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "StaleFollowUp",
            "title": f"Follow-up draft ready: {company}",
            "body": draft_result.get("draft_message", "Follow up on stale application"),
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "job_id": job_id,
                "suggested_subject": draft_result.get("suggested_subject", ""),
                "days_since_applied": draft_result.get("days_since_applied", 0),
            },
        })
        return {"notified": True}

    saga.add_step("draft_follow_up", step_draft)
    saga.add_step("notification", step_notify)

    return await saga.run()
