import asyncio
import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.browser import browser, page_goto
from naukri_server.config import JOB_DETAIL_API, NAUKRI_BASE, REPORT_FRAUD_API, logger
from naukri_server.validation import validate_job_detail


# ============================================================================
# Tool 4: Get Job Details (REST API)
# ============================================================================


def _extract_job_id(job_url_or_id: str) -> str:
    """Extract numeric job ID from URL or pass through if already an ID.

    Raises ValueError if no valid numeric job ID can be extracted.
    """
    if job_url_or_id.isdigit():
        return job_url_or_id
    # URL pattern: ...-<jobId> at the end
    match = re.search(r'(\d{6,})', job_url_or_id)
    if match:
        return match.group(1)
    raise ValueError(f"Invalid job ID or URL: {job_url_or_id}")


def _parse_job_detail(details_data: dict, job_id: str, page_url: str,
                      score_data: dict = None) -> dict:
    """Parse job detail API response (v3 or v4 format) into a structured result dict."""
    job = details_data.get("jobDetails", details_data)

    salary = job.get("salaryDetail", {})
    sal_label = salary.get("label", "")
    sal_min = salary.get("minimumSalary", 0)
    sal_max = salary.get("maximumSalary", 0)
    salary_str = sal_label if sal_label else (
        f"{sal_min/100000:.1f}-{sal_max/100000:.1f} LPA" if sal_max else "Not Disclosed"
    )

    company = job.get("companyDetail", {})
    ambition = job.get("ambitionBoxData", {})
    is_applied = job.get("isApplied", False)
    external = bool(job.get("applyRedirectUrl"))

    match_score = None
    if score_data:
        match_score = score_data.get("Keyskills")

    result = {
        "status": "success",
        "job_id": job_id,
        "title": job.get("title"),
        "company": company.get("name"),
        "company_rating": ambition.get("AggregateRating") or ambition.get("Rating"),
        "company_reviews_count": ambition.get("ReviewsCount"),
        "salary": salary_str,
        "experience": f"{job.get('minimumExperience', '?')}-{job.get('maximumExperience', '?')} years",
        "location": job.get("cityName") or job.get("citySuburb"),
        "description": job.get("description", ""),
        "skills": [s.get("label", s) if isinstance(s, dict) else s for s in job.get("keySkills", [])],
        "match_score": match_score,
        "is_applied": is_applied,
        "external_apply": external,
        "can_apply": not is_applied and not external,
        "vacancies": job.get("vacany"),
        "apply_count": job.get("applyCount"),
        "hr_name": job.get("contactPerson") or job.get("createdBy"),
        "hr_email": job.get("contactEmail"),
        "url": page_url,
    }
    warnings = validate_job_detail(result)
    if warnings:
        result["warnings"] = warnings
    return result


@mcp.tool()
async def naukri_get_job(job_id_or_url: str) -> dict:
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
        return {"status": "error", "message": str(e)}

    # Build URL for display/fallback
    if job_id_or_url.isdigit():
        page_url = f"{NAUKRI_BASE}/job-listings-{job_id_or_url}"
    elif not job_id_or_url.startswith("http"):
        page_url = f"{NAUKRI_BASE}/job-listings-{job_id_or_url}"
    else:
        page_url = job_id_or_url

    # Strategy 1: Direct REST API (fast, no browser needed)
    try:
        data = await api_get(JOB_DETAIL_API + job_id)
        if data:
            result = _parse_job_detail(data, job_id, page_url)
            if result.get("title"):  # Sanity check — v3 returned meaningful data
                return result
            logger.info("REST v3 returned empty title for job %s, falling back to browser", job_id)
    except NaukriAPIError as e:
        logger.info("REST v3 failed for job %s: %s — falling back to browser", job_id, e)
    except Exception as e:
        logger.info("REST v3 error for job %s: %s — falling back to browser", job_id, e)

    # Strategy 2: Browser interception (existing fallback)
    async with browser.page_pool.acquire() as page:
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
            try:
                await page_goto(page, page_url)
                try:
                    await asyncio.wait_for(response_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    logger.warning("Job details response capture timed out after 10s for job: %s", job_id)
                # Give a moment for match score to arrive too
                if "score" not in captured:
                    await asyncio.sleep(1)
            finally:
                page.remove_listener("response", on_response_with_event)

            details_data = captured.get("details")
            if not details_data:
                return {"status": "error", "message": "Job details API response not captured. Page may not have loaded."}

            return _parse_job_detail(details_data, job_id, page_url, captured.get("score"))
        except Exception as e:
            return {"status": "error", "message": f"Get job failed: {type(e).__name__}: {e!r}"}


@mcp.tool()
@api_tool("Report fraud job")
async def naukri_report_fraud_job(job_id: str, reason: str) -> dict:
    """Report a job posting as fraudulent or suspicious on Naukri.

    Args:
        job_id: The job ID to report
        reason: Description of why this job seems fraudulent

    Returns:
        - {status: "success", job_id, message}
        - {status: "error", message}
    """
    await api_post(
        REPORT_FRAUD_API,
        body={"jobId": job_id, "reason": reason},
    )
    return {
        "status": "success",
        "job_id": job_id,
        "message": "Job reported as fraudulent.",
    }
