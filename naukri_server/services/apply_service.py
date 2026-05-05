"""Apply service — saga orchestration, composite locking, and reminder wiring for job applications.

Pure answer-handling helpers (find_user_answer, format_answer) live here too so
they can be tested without touching the tool module's I/O surface. Cache-coupled
helpers (_cache_answers, _build_apply_answers) remain in tools/apply.py because
tests patch tools.apply._cache_key against them.
"""

import asyncio
import re

from naukri_server.config import (
    logger, BATCH_APPLY_DEFAULT_DELAY_MS, BATCH_APPLY_DEFAULT_CONCURRENCY,
)

__all__ = [
    "apply_with_saga",
    "batch_apply_with_reminders",
    "find_user_answer",
    "format_answer",
]

_tracking_composite_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Pure answer-handling helpers — no I/O, no module-level patch points.
# ---------------------------------------------------------------------------

def _to_str(val) -> str:
    """Unwrap single-element lists, stringify scalars (helper for find_user_answer)."""
    if isinstance(val, list):
        return str(val[0]) if val else ""
    return str(val)


def find_user_answer(qid: str, q_name: str, answers: dict) -> str | None:
    """Find a user-provided answer by question ID or fuzzy text match.

    Resolution order:
      1. Exact question-ID match (``answers[qid]``)
      2. Bidirectional substring match between answer key and question text
      3. All-words-present match (split by space)

    Returns ``None`` if no match. Pure function — no module state.
    """
    if qid in answers:
        return _to_str(answers[qid])
    q_lower = q_name.lower()
    for key, value in answers.items():
        k = key.lower().replace("_", " ")
        if k in q_lower or q_lower in k:
            return _to_str(value)
        if all(w in q_lower for w in k.split()):
            return _to_str(value)
    return None


def format_answer(answer: str, q_type: str, options: dict) -> str | list[str]:
    """Format an answer for the Naukri apply API based on the question type.

    Pure function — given an answer string + question metadata, returns the
    wire-format value (str for text-box, list[str] for radio/checkbox/list).

    Resolution order for option-typed questions:
      1. Exact case-insensitive match against an option label
      2. Exact key-based match (answer == option dict key)
      3. Word-boundary regex match (prevents "java" matching "javascript")
      4. Fallback: wrap in single-element list
    """
    if q_type == "Text Box":
        return answer

    if not options:
        if q_type and q_type != "Text Box":
            logger.debug("Question type '%s' has no options, treating as text", q_type)
        return answer

    option_values = list(options.values())

    # Priority 1: Exact case-insensitive match
    for opt in option_values:
        if answer.lower() == opt.lower():
            return [opt]

    # Priority 2: Key-based match (answer is option dict key)
    if answer in options:
        return [options[answer]]

    # Priority 3: Word-boundary regex match (prevents "java" matching "javascript")
    for opt in option_values:
        if re.search(r'\b' + re.escape(answer.lower()) + r'\b', opt.lower()):
            return [opt]

    # Fallback: wrap in list as-is
    return [answer]


# ---------------------------------------------------------------------------
# Saga & batch orchestration (existing API preserved)
# ---------------------------------------------------------------------------


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
