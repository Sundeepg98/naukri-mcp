"""Apply service — saga orchestration, composite locking, and reminder wiring for job applications."""

import asyncio

from naukri_server.config import (
    logger, BATCH_APPLY_DEFAULT_DELAY_MS, BATCH_APPLY_DEFAULT_CONCURRENCY,
)

__all__ = [
    "apply_with_saga",
    "batch_apply_with_reminders",
]

_tracking_composite_lock = asyncio.Lock()


async def apply_with_saga(**kw) -> dict:
    """Wrap apply with duplicate check, composite lock, saga orchestration, and optional reminder."""
    from naukri_server.tools.apply import _apply_single
    from naukri_server.tools.jobs import _extract_job_id
    from naukri_server.sagas import SagaExecutor

    job_id = _extract_job_id(kw["job_id"])
    from naukri_server.database import get_application
    existing = await get_application(str(job_id))
    if existing:
        return {
            "status": "already_applied",
            "message": "You have already applied to this job (from local tracking).",
            "job_id": job_id,
        }

    async with _tracking_composite_lock:
        saga = SagaExecutor("apply_workflow")
        apply_result = {}

        async def do_apply():
            nonlocal apply_result
            apply_result = await _apply_single(job_id, kw.get("answers"), tracking_extra={"source": "single"})
            return apply_result

        saga.add_step("apply", do_apply)

        set_reminder_days = kw.get("set_reminder_days")
        if set_reminder_days:
            async def do_reminder():
                if apply_result.get("status") == "applied":
                    from naukri_server.tools.reminders import _set_reminder
                    await _set_reminder(
                        job_id=job_id,
                        days=set_reminder_days,
                        note=f"Follow up on application to {apply_result.get('company', 'unknown')}",
                    )
                    return {"reminder_set": True, "reminder_days": set_reminder_days}
                return {"reminder_set": False}

            saga.add_step("reminder", do_reminder)

        saga_result = await saga.run()

        if saga_result["status"] == "success" and apply_result:
            apply_result["saga_steps"] = saga_result["completed_steps"]
            reminder_result = saga_result.get("results", {}).get("reminder")
            if reminder_result:
                apply_result.update(reminder_result)
            return apply_result

        if saga_result["status"] == "error":
            if apply_result:
                apply_result["saga_steps"] = saga_result.get("completed_steps", [])
                apply_result["saga_errors"] = saga_result.get("errors", [])
                return apply_result
            return saga_result

    return apply_result


async def batch_apply_with_reminders(**kw) -> dict:
    """Wrap batch_apply with composite lock and optional reminders."""
    from naukri_server.tools.apply import _batch_apply

    async with _tracking_composite_lock:
        result = await _batch_apply(
            keywords=kw.get("keywords"), location=kw.get("location"), experience=kw.get("experience"),
            salary_min=kw.get("salary_min"), salary_max=kw.get("salary_max"), sort_by=kw.get("sort_by"),
            freshness=kw.get("freshness"), work_mode=kw.get("work_mode"), job_type=kw.get("job_type"),
            company_type=kw.get("company_type"), limit=kw.get("limit", 50), answers=kw.get("answers"),
            delay_ms=kw.get("delay_ms", BATCH_APPLY_DEFAULT_DELAY_MS),
            max_concurrent=kw.get("max_concurrent", BATCH_APPLY_DEFAULT_CONCURRENCY),
        )
        set_reminder_days = kw.get("set_reminder_days")
        if set_reminder_days and result.get("results"):
            from naukri_server.tools.reminders import _set_reminder
            reminder_count = 0
            for r in result["results"]:
                if r.get("status") == "applied":
                    try:
                        await _set_reminder(
                            job_id=r.get("job_id", ""),
                            days=set_reminder_days,
                            note=f"Follow up on application to {r.get('company', 'unknown')}",
                        )
                        reminder_count += 1
                    except Exception as e:
                        logger.debug("Failed to set reminder for job %s: %s", r.get("job_id", ""), e)
            if reminder_count:
                result["reminders_set"] = reminder_count
                result["reminder_days"] = set_reminder_days
    return result
