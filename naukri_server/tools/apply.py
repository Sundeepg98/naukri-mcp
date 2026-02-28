import asyncio
import json
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_post
from naukri_server.cache import _cache_lock, _load_cache, _save_cache, _cache_key
from naukri_server.config import APPLY_TRAILER
from naukri_server.tools.jobs import _extract_job_id
from naukri_server.tools.tracking import record_application


# ============================================================================
# Tool 5: Apply to Job (REST API — two-phase)
# ============================================================================


async def _apply_single(job_id: str, answers: Optional[dict] = None,
                         title: str = None, company: str = None) -> dict:
    """Core apply logic — POST to apply endpoint, handle questions from cache.

    Used by both naukri_apply (single) and naukri_batch_apply (parallel).
    """
    if answers is None:
        answers = {}
    job_id = _extract_job_id(job_id)

    try:
        async with _cache_lock:
            cache = _load_cache()

        body = {
            "strJobsarr": [job_id],
            **APPLY_TRAILER,
        }

        if answers:
            apply_answers = _build_apply_answers(job_id, answers, cache)
            if apply_answers:
                body["applyData"] = {job_id: {"answers": apply_answers}}

        data = await api_post(
            "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply",
            body,
        )

        jobs = data.get("jobs", [])
        if not jobs:
            msg = data.get("message", "")
            if "already" in msg.lower():
                return {"status": "already_applied", "job_id": job_id}
            return {"status": "error", "job_id": job_id, "message": msg or "Unexpected response"}

        job_result = jobs[0]
        status_code = job_result.get("status")
        questionnaire = job_result.get("questionnaire", [])

        if status_code == 200:
            if questionnaire and answers:
                async with _cache_lock:
                    cache = _load_cache()
                    _cache_answers(questionnaire, answers, cache)
                    _save_cache(cache)
            await record_application(job_id, title=title, company=company, status="applied")
            return {
                "status": "applied",
                "job_id": job_id,
                "daily_applied": data.get("quotaDetails", {}).get("dailyApplied"),
            }

        if questionnaire:
            pending = []
            auto_answers = {}

            # Single lock for the whole cache read-modify-write cycle
            async with _cache_lock:
                cache = _load_cache()

                for q in questionnaire:
                    qid = str(q.get("questionId", ""))
                    q_name = q.get("questionName", "")
                    q_type = q.get("questionType", "")
                    options = q.get("answerOption", {})
                    cache_k = _cache_key(q_name, options)

                    cached = cache.get(cache_k)
                    if cached:
                        auto_answers[qid] = cached["answer"]
                        continue

                    user_answer = _find_user_answer(qid, q_name, answers)
                    if user_answer is not None:
                        formatted = _format_answer(user_answer, q_type, options)
                        auto_answers[qid] = formatted
                        cache[cache_k] = {
                            "questionType": q_type,
                            "questionName": q_name,
                            "answer": formatted,
                        }
                        continue

                    pending.append({
                        "question_id": qid,
                        "question": q_name,
                        "type": q_type,
                        "options": options,
                    })

                if not pending and auto_answers:
                    _save_cache(cache)
                elif pending:
                    _save_cache(cache)

            # Network calls OUTSIDE the lock
            if not pending and auto_answers:
                body["applyData"] = {job_id: {"answers": auto_answers}}
                data2 = await api_post(
                    "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply",
                    body,
                )
                jobs2 = data2.get("jobs", [])
                if jobs2 and jobs2[0].get("status") == 200:
                    await record_application(job_id, title=title, company=company, status="applied")
                    return {
                        "status": "applied",
                        "job_id": job_id,
                        "questions_answered": len(auto_answers),
                        "daily_applied": data2.get("quotaDetails", {}).get("dailyApplied"),
                    }

            if pending:
                return {
                    "status": "needs_input",
                    "job_id": job_id,
                    "questions": pending,
                    "auto_answered": len(auto_answers),
                }

        return {"status": "error", "job_id": job_id, "message": f"Apply returned status {status_code}"}

    except ValueError as e:
        return {"status": "error", "job_id": job_id, "message": str(e)}
    except Exception as e:
        return {"status": "error", "job_id": job_id, "message": f"{type(e).__name__}: {e!r}"}


@mcp.tool()
async def naukri_apply(
    job_id: str,
    answers: Optional[dict] = None,
) -> dict:
    """Apply to a Naukri job via API. Two-phase flow:

    Phase 1 (no answers): Sends apply request. If the job has screening questions,
    returns them with options. If no questions, applies instantly.

    Phase 2 (with answers): Provide answers dict keyed by question text or question ID.
    Submits answers and completes application.

    Args:
        job_id: Naukri job ID (numeric string, from search results or get_job)
        answers: Dict of answers for screening questions. Keys can be:
                 - Question ID (from phase 1 response): {"12345": "5"}
                 - Question text substring: {"current ctc": "16", "notice period": "30"}
                 For radio/list/checkbox: value is the option text to select.
                 For text box: value is the string to enter.

    Returns:
        - {status: "applied"} — success
        - {status: "needs_input", questions: [...]} — answer the questions and call again
        - {status: "already_applied"} — already applied
        - {status: "error", message: "..."} — failure
    """
    return await _apply_single(job_id, answers)


@mcp.tool()
async def naukri_batch_apply(
    keywords: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    limit: int = 5,
    answers: Optional[dict] = None,
) -> dict:
    """Search and apply to multiple jobs in parallel. Like Naukri Pro's auto-apply.

    Searches for jobs, filters out already-applied ones, then applies to all
    remaining jobs concurrently. Screening questions are auto-answered from
    the cache or the provided answers dict.

    Args:
        keywords: Job title or skills (e.g., "python developer", "react node.js")
        location: City name (e.g., "Bangalore", "Mumbai", "Remote")
        experience: Years of experience filter
        limit: Max jobs to apply to (default 5, max 20)
        answers: Pre-filled answers for common screening questions.
                 Keys are question text substrings: {"current ctc": "16", "notice period": "15 days"}
    """
    from naukri_server.tools.search import naukri_search_jobs

    if answers is None:
        answers = {}
    limit = min(limit, 20)

    # Step 1: Search
    search_result = await naukri_search_jobs(keywords, location, experience, limit)
    if search_result.get("status") != "success":
        return {"status": "error", "message": "Search failed", "search_result": search_result}

    all_jobs = search_result.get("jobs", [])
    if not all_jobs:
        return {"status": "error", "message": "No jobs found for this search"}

    # Step 2: Filter out already-applied
    to_apply = [j for j in all_jobs if not j.get("is_applied")]
    if not to_apply:
        return {
            "status": "completed",
            "message": f"All {len(all_jobs)} jobs already applied to",
            "searched": len(all_jobs),
            "filtered": 0,
        }

    # Step 3: Parallel apply (Phase 1 + auto-answer from cache)
    apply_tasks = [_apply_single(j["job_id"], answers, j.get("title"), j.get("company")) for j in to_apply]
    results = await asyncio.gather(*apply_tasks, return_exceptions=True)

    # Step 4: Collect results
    applied = 0
    needs_input = 0
    errors = 0
    already = 0
    pending_questions = {}  # deduplicated: question_text -> {info, job_ids}
    per_job = []

    for job_info, result in zip(to_apply, results):
        if isinstance(result, Exception):
            errors += 1
            per_job.append({
                "job_id": job_info["job_id"],
                "title": job_info.get("title"),
                "company": job_info.get("company"),
                "status": "error",
                "message": str(result),
            })
            continue

        status = result.get("status")
        entry = {
            "job_id": job_info["job_id"],
            "title": job_info.get("title"),
            "company": job_info.get("company"),
            "salary": job_info.get("salary"),
            "location": job_info.get("location"),
            "status": status,
        }

        if status == "applied":
            applied += 1
        elif status == "already_applied":
            already += 1
        elif status == "needs_input":
            needs_input += 1
            entry["questions"] = result.get("questions", [])
            # Deduplicate questions across jobs
            for q in result.get("questions", []):
                q_key = q["question"]
                if q_key not in pending_questions:
                    pending_questions[q_key] = {
                        "question": q["question"],
                        "type": q["type"],
                        "options": q.get("options", {}),
                        "affects_jobs": [],
                    }
                pending_questions[q_key]["affects_jobs"].append(job_info["job_id"])
        else:
            errors += 1
            entry["message"] = result.get("message")

        per_job.append(entry)

    return {
        "status": "completed",
        "searched": len(all_jobs),
        "filtered": len(to_apply),
        "applied": applied,
        "already_applied": already,
        "needs_input": needs_input,
        "errors": errors,
        "pending_questions": list(pending_questions.values()),
        "results": per_job,
    }


def _find_user_answer(qid: str, q_name: str, answers: dict) -> Optional[str]:
    """Find a user-provided answer by question ID or fuzzy text match."""
    # Exact ID match
    if qid in answers:
        return str(answers[qid])

    # Fuzzy text match on question name
    q_lower = q_name.lower()
    for key, value in answers.items():
        k = key.lower().replace("_", " ")
        if k in q_lower or q_lower in k:
            return str(value)
        if all(w in q_lower for w in k.split()):
            return str(value)
    return None


def _format_answer(answer: str, q_type: str, options: dict) -> any:
    """Format answer for the Naukri API based on question type."""
    if q_type == "Text Box":
        return answer

    if not options:
        return answer  # Treat as text when no options exist

    option_values = list(options.values())

    # Priority 1: Exact case-insensitive match
    for opt in option_values:
        if answer.lower() == opt.lower():
            return [opt]

    # Priority 2: Key-based match (answer is option dict key)
    if answer in options:
        return [options[answer]]

    # Priority 3: Word-boundary regex match (prevents "java" matching "javascript")
    import re
    for opt in option_values:
        if re.search(r'\b' + re.escape(answer.lower()) + r'\b', opt.lower()):
            return [opt]

    # Fallback: wrap in list as-is
    return [answer]


def _build_apply_answers(job_id: str, answers: dict, cache: dict) -> dict:
    """Build answers dict from user-provided answers (for direct submission)."""
    result = {}
    for key, value in answers.items():
        # If key looks like a question ID (numeric), use directly
        if str(key).isdigit():
            result[str(key)] = value if isinstance(value, list) else str(value)
    return result


def _cache_answers(questionnaire: list, answers: dict, cache: dict):
    """Cache successfully answered questions."""
    for q in questionnaire:
        qid = str(q.get("questionId", ""))
        q_name = q.get("questionName", "")
        q_type = q.get("questionType", "")
        options = q.get("answerOption", {})

        answer = _find_user_answer(qid, q_name, answers)
        if answer:
            cache_k = _cache_key(q_name, options)
            if cache_k not in cache:
                cache[cache_k] = {
                    "questionType": q_type,
                    "questionName": q_name,
                    "answer": _format_answer(answer, q_type, options),
                }
