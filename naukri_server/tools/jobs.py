import asyncio
import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import NaukriAPIError
from naukri_server.browser import AuthExpiredError, NotLoggedInError
from naukri_server.interfaces import api_client, browser_provider
from naukri_server.config import BULK_JOBS_API, JOB_DETAIL_API, JOB_DETAIL_V1_API, JOB_MATCH_SCORE_API, NAUKRI_BASE, REPORT_FRAUD_API, SIMILAR_JOBS_API, LAKHS_MULTIPLIER, INTERCEPT_WAIT_TIMEOUT, BROWSER_PAGE_SETTLE, logger
from naukri_server.error_handler import handle_tool_action
from naukri_server.tools.job_parsing import _parse_job_list
from naukri_server.validation import validate_job_detail


# ============================================================================
# Tool 4: Get Job Details (REST API)
# ============================================================================


# ── Service delegates — canonical logic lives in search_service ──────────
from naukri_server.services.search_service import (
    extract_job_id as _extract_job_id,
    extract_skills as _extract_skills,
    format_education as _format_education,
    extract_placeholder as _extract_placeholder,
    parse_salary_data as _parse_salary_data,
    parse_company_data as _parse_company_data,
    parse_match_score as _parse_match_score,
    parse_job_detail as _parse_job_detail,
    parse_job_detail_v1,
    parse_similar_jobs_rest,
    flatten_match_score_rest,
)


async def _fetch_match_score(job_id: str) -> dict | None:
    """Fetch per-job match score — optional enrichment, returns None on failure."""
    try:
        score_data = await api_client.get(f"{JOB_MATCH_SCORE_API}{job_id}/matchscore")
        return flatten_match_score_rest(score_data)
    except Exception:
        return None


async def _get_job(job_id_or_url: str) -> dict:
    """Get full details for a specific Naukri job — description, skills, salary, match score, apply status.

    Requires: a job URL or numeric job_id from search results.

    Args:
        job_id_or_url: Naukri job URL or job ID (numeric)

    Returns:
        - {status: "success", job_id, title, company, salary, experience, location, description, skills, match_score, is_applied, can_apply, url, ...}
        - {status: "error", message}
    """
    try:
        job_id = _extract_job_id(job_id_or_url)
    except ValueError as e:
        return {"status": "error", "message": str(e), "error_code": "API_ERROR"}

    # Build URL for display/fallback
    if job_id_or_url.isdigit():
        page_url = f"{NAUKRI_BASE}/job-listings-{job_id_or_url}"
    elif not job_id_or_url.startswith("http"):
        page_url = f"{NAUKRI_BASE}/job-listings-{job_id_or_url}"
    else:
        page_url = job_id_or_url

    # Strategy 1: Direct REST API (fast, no browser needed)
    try:
        detail_task = api_client.get(JOB_DETAIL_API + job_id)
        score_task = _fetch_match_score(job_id)
        data, match_score = await asyncio.gather(detail_task, score_task)
        if data:
            result = _parse_job_detail(data, job_id, page_url)
            if result.get("title"):  # Sanity check — v3 returned meaningful data
                if match_score:
                    result["match_score"] = match_score
                return result
            logger.info("REST v3 returned empty title for job %s, falling back to browser", job_id)
    except (NotLoggedInError, AuthExpiredError) as e:
        # An expired session is NOT a browser fault, and the browser fallback
        # below cannot succeed without a token -- entering it would only turn a
        # clear auth error into a second-order browser error (this is exactly
        # how a live probe mis-attributed 16 of 18 failures). Stop here.
        logger.error(
            "REST v3 failed for job %s: auth session expired (%s: %s). "
            "Not falling back to browser -- re-authentication required.",
            job_id, type(e).__name__, e,
        )
        return {
            "status": "error",
            "message": (
                "Naukri session expired or not logged in. The browser fallback "
                "cannot work without a valid session -- call naukri_login to "
                "re-authenticate."
            ),
            "error_code": "AUTH_ERROR",
        }
    except NaukriAPIError as e:
        logger.warning(
            "REST v3 failed for job %s (NaukriAPIError http %s: %s), falling back to browser",
            job_id, e.status, e,
        )
    except Exception as e:
        logger.warning(
            "REST v3 error for job %s (%s: %s), falling back to browser",
            job_id, type(e).__name__, e,
        )

    # Strategy 2: Browser interception (existing fallback).
    # Page object is opaque to BrowserProvider; we use Playwright's native
    # event API (page.on / page.remove_listener) directly because we need
    # to capture two separate XHR responses (details + match score) which
    # the simpler intercept_json() helper can't model.
    async with browser_provider.acquire_page() as page:
        try:
            # Intercept job details + match score API responses
            captured = {}

            response_event = asyncio.Event()

            async def on_response_with_event(response):
                try:
                    if f"/jobapi/v4/job/{job_id}" in response.url and response.status == 200:
                        captured["details"] = await response.json()
                        response_event.set()
                    elif f"/job/{job_id}/matchscore" in response.url and response.status == 200:
                        captured["score"] = await response.json()
                except Exception as e:
                    logger.debug("job response parse failed: %s", e)

            page.on("response", on_response_with_event)
            # safe_goto's return value used to be DISCARDED: a failed navigation
            # still burned the full intercept timeout and then blamed the page
            # ("Page may not have loaded"), hiding a dead browser behind an
            # API_ERROR. Capture it and short-circuit honestly instead.
            navigated = False
            try:
                navigated = await browser_provider.safe_goto(page, page_url)
                if navigated:
                    try:
                        await asyncio.wait_for(response_event.wait(), timeout=INTERCEPT_WAIT_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning("Job details response capture timed out after %ss for job: %s", INTERCEPT_WAIT_TIMEOUT, job_id)
                    # Give a moment for match score to arrive too
                    if "score" not in captured:
                        await asyncio.sleep(BROWSER_PAGE_SETTLE)
                else:
                    logger.error(
                        "Browser navigation to the job page failed for job %s (%s) "
                        "- skipping the intercept wait, no response can arrive.",
                        job_id, page_url,
                    )
            finally:
                page.remove_listener("response", on_response_with_event)

            if not navigated:
                return {
                    "status": "error",
                    "message": (
                        f"Browser navigation to {page_url} failed - the job page never "
                        f"loaded. The browser may be dead or the URL unreachable."
                    ),
                    "error_code": "BROWSER_ERROR",
                }

            details_data = captured.get("details")
            if not details_data:
                return {"status": "error", "message": f"Job details API response was not captured within {INTERCEPT_WAIT_TIMEOUT}s of a successful page load.", "error_code": "API_ERROR"}

            return _parse_job_detail(details_data, job_id, page_url, captured.get("score"))
        except Exception as e:
            return {"status": "error", "message": f"Get job failed: {type(e).__name__}: {e!r}", "error_code": "API_ERROR"}


naukri_get_job = _get_job


async def _report_fraud(job_id: str, reason: str) -> dict:
    """Report a job posting as fraudulent or suspicious on Naukri.

    Args:
        job_id: The job ID to report
        reason: Description of why this job seems fraudulent

    Returns:
        - {status: "success", job_id, message}
        - {status: "error", message}
    """
    await api_client.post(
        REPORT_FRAUD_API,
        body={"jobId": job_id, "reason": reason},
    )
    return {
        "status": "success",
        "job_id": job_id,
        "message": "Job reported as fraudulent.",
    }


naukri_report_fraud_job = _report_fraud


async def _bulk_fetch_jobs(job_ids: list) -> dict:
    """Fetch multiple jobs in a single API call. Max efficiency for batch operations."""
    if not job_ids or len(job_ids) < 1:
        return {"status": "error", "message": "Provide at least 1 job_id", "error_code": "VALIDATION_ERROR"}
    if len(job_ids) > 20:
        job_ids = job_ids[:20]

    data = await api_client.post(BULK_JOBS_API, {"jobIds": job_ids})
    raw_jobs = data.get("jobDetails", [])

    jobs = _parse_job_list(raw_jobs, len(raw_jobs))

    return {
        "status": "success",
        "count": len(jobs),
        "jobs": jobs,
    }


async def _get_job_v1(job_id: str) -> dict:
    """Get enriched job detail from V1 API — walk-in, contact, metrics, closing date.

    Thin orchestrator: fetches the raw response, then delegates field extraction
    to ``parse_job_detail_v1`` (search_service). All field reads route through
    ``safe_get`` for anti-corruption-layer logging.
    """
    if not job_id:
        return {"status": "error", "message": "job_id is required", "error_code": "VALIDATION_ERROR"}

    data = await api_client.get(JOB_DETAIL_V1_API + job_id)
    return parse_job_detail_v1(data)


async def _get_similar_jobs_rest(job_id: str, limit: int = 10) -> dict:
    """Fetch similar jobs via v2 REST API (no browser needed).

    Uses /jobapi/v2/search/simjobs/{jobId}. Parsing/normalization is delegated
    to ``parse_similar_jobs_rest`` (search_service).

    Args:
        job_id: Naukri job ID
        limit: Max similar jobs to return (default 10)

    Returns:
        {status, job_id, source, total, count, jobs: [...]}
    """
    data = await api_client.get(f"{SIMILAR_JOBS_API}{job_id}", params={
        "noOfResults": str(limit),
        "searchType": "sim",
    })
    return parse_similar_jobs_rest(data, job_id, limit)


# ---------------------------------------------------------------------------
# Helpers used by the atomic single-purpose tools below
# ---------------------------------------------------------------------------

async def _do_similar(**kw) -> dict:
    """REST-first similar jobs with browser fallback."""
    job_id, limit, page = kw["job_id"], kw.get("limit", 10), kw.get("page", 1)
    try:
        return await _get_similar_jobs_rest(job_id, limit)
    except Exception:
        pass
    from naukri_server.tools.search import _get_similar_jobs
    return await _get_similar_jobs(job_id=job_id, limit=limit, page=page)


async def _do_compare(**kw) -> dict:
    """Compare 2-5 jobs side-by-side with fit scores."""
    from naukri_server.tools.compare import _compare_jobs
    return await _compare_jobs(job_ids=kw["job_ids"], timeout_seconds=kw.get("timeout_seconds", 120))


# ============================================================================
# Single-purpose job tools
# ============================================================================


@mcp.tool(name="naukri_get_job")
async def _tool_get_job(job_id: str) -> dict:
    """Fetch full job details -- description, skills, salary, match score, apply status.

    Args:
        job_id: Naukri job ID or full job URL

    Returns:
        {status, title, company, salary, experience, location, skills, match_score, is_applied, can_apply, url, ...}
    """
    if not job_id:
        return {"status": "error", "message": "job_id is required", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(lambda: _get_job(job_id_or_url=job_id), "jobs.get")


@mcp.tool(name="naukri_similar_jobs")
async def _tool_similar_jobs(job_id: str, limit: int = 10, page: int = 1) -> dict:
    """Find jobs similar to a given job posting.

    Args:
        job_id: Naukri job ID to find similar jobs for
        limit: Max similar jobs to return (default 10)
        page: Page number (default 1)

    Returns:
        {status, job_id, source, total, count, jobs: [...]}
    """
    if not job_id:
        return {"status": "error", "message": "job_id is required", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(lambda: _do_similar(job_id=job_id, limit=limit, page=page), "jobs.similar")


@mcp.tool(name="naukri_compare_jobs")
async def _tool_compare_jobs(job_ids: list[str], timeout_seconds: int = 120) -> dict:
    """Compare 2-5 jobs side-by-side with fit scores.

    Args:
        job_ids: List of 2-5 job IDs to compare
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        {status, count, jobs, common_skills, all_skills, best_match_job_id, average_fit_score}
    """
    if not job_ids:
        return {"status": "error", "message": "job_ids is required (list of 2-5 IDs)", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(lambda: _do_compare(job_ids=job_ids, timeout_seconds=timeout_seconds), "jobs.compare")


@mcp.tool(name="naukri_report_fraud")
async def _tool_report_fraud(job_id: str, reason: str = "") -> dict:
    """Report a fraudulent or suspicious job listing.

    Args:
        job_id: The job ID to report
        reason: Description of why this job seems fraudulent

    Returns:
        {status, job_id, message}
    """
    if not job_id:
        return {"status": "error", "message": "job_id is required", "error_code": "VALIDATION_ERROR"}
    if not reason:
        return {"status": "error", "message": "reason is required", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(lambda: _report_fraud(job_id=job_id, reason=reason), "jobs.report_fraud")


@mcp.tool(name="naukri_job_detail_v1")
async def _tool_job_detail_v1(job_id: str) -> dict:
    """Get V1 job detail -- walk-in info, contact details, jd views/applies, closing date.

    Args:
        job_id: Naukri job ID

    Returns:
        {status, job_id, title, company, is_walk_in, contact_name, jd_views, jd_applies, closing_date, ...}
    """
    if not job_id:
        return {"status": "error", "message": "job_id is required", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(lambda: _get_job_v1(job_id=job_id), "jobs.detail_v1")


@mcp.tool(name="naukri_bulk_fetch_jobs")
async def _tool_bulk_fetch_jobs(job_ids: list[str]) -> dict:
    """Bulk fetch multiple job details in one API call (up to 20).

    Args:
        job_ids: List of 1-20 job IDs to fetch

    Returns:
        {status, count, jobs: [...]}
    """
    if not job_ids:
        return {"status": "error", "message": "job_ids is required (list of 1-20 IDs)", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(lambda: _bulk_fetch_jobs(job_ids=job_ids), "jobs.bulk")
