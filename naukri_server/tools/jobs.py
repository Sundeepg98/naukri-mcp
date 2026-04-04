import asyncio
import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import NaukriAPIError
from naukri_server.interfaces import api_client
from naukri_server.browser import browser, page_goto
from naukri_server.config import BULK_JOBS_API, JOB_DETAIL_API, JOB_DETAIL_V1_API, JOB_MATCH_SCORE_API, NAUKRI_BASE, REPORT_FRAUD_API, SIMILAR_JOBS_API, LAKHS_MULTIPLIER, INTERCEPT_WAIT_TIMEOUT, BROWSER_PAGE_SETTLE, logger
from naukri_server.error_handler import handle_tool_action
from naukri_server.models import validate_action_params
from naukri_server.tools.job_parsing import _parse_job_list
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


def _extract_skills(raw_skills) -> list:
    """Extract skill labels from keySkills — handles both list and dict formats."""
    if isinstance(raw_skills, dict):
        # v3 format: {"preferred": [{label: "Python"}, ...], "other": [...]}
        skills = []
        for category_skills in raw_skills.values():
            if isinstance(category_skills, list):
                for s in category_skills:
                    label = s.get("label", s) if isinstance(s, dict) else s
                    if label and isinstance(label, str):
                        skills.append(label)
        return skills
    elif isinstance(raw_skills, list):
        return [s.get("label", s) if isinstance(s, dict) else s for s in raw_skills]
    return []


def _format_education(edu) -> str | None:
    """Format the v4 education object into a readable string."""
    if not edu or not isinstance(edu, dict):
        return None
    parts = []
    ug = edu.get("ugQualification") or edu.get("ug")
    pg = edu.get("pgQualification") or edu.get("pg")
    if ug:
        parts.append(f"UG: {ug}")
    if pg:
        parts.append(f"PG: {pg}")
    return ", ".join(parts) if parts else None


def _extract_placeholder(placeholders: list, ptype: str) -> str | None:
    """Extract a label from Naukri's placeholders array by type."""
    for p in placeholders:
        if isinstance(p, dict) and p.get("type") == ptype:
            return p.get("label")
    return None


def _parse_salary_data(job: dict) -> dict:
    """Extract salary info from job data."""
    salary = job.get("salaryDetail", {})
    sal_label = salary.get("label", "")
    sal_min = salary.get("minimumSalary", 0)
    sal_max = salary.get("maximumSalary", 0)
    salary_str = sal_label if sal_label else (
        f"{sal_min/LAKHS_MULTIPLIER:.1f}-{sal_max/LAKHS_MULTIPLIER:.1f} LPA" if sal_max else "Not Disclosed"
    )
    return {
        "salary": salary_str,
        "salary_min": sal_min,
        "salary_max": sal_max,
    }


def _parse_company_data(job: dict, details_data: dict) -> dict:
    """Extract company details + ambitionbox rating."""
    company = job.get("companyDetail", {})
    ambition = job.get("ambitionBoxData") or {}
    ab_details = details_data.get("ambitionBoxDetails", {})
    if not ambition and ab_details:
        ci = ab_details.get("companyInfo") or {}
        ambition = {
            "AggregateRating": ci.get("rating"),
            "ReviewsCount": ci.get("reviewsCount"),
        }
    return {
        "company_name": company.get("name"),
        "company_rating": ambition.get("AggregateRating") or ambition.get("Rating"),
        "company_reviews_count": ambition.get("ReviewsCount"),
        "company_website": company.get("websiteUrl") if company else None,
        "group_id": company.get("groupId") if company else None,
    }


def _parse_match_score(score_data: dict) -> dict:
    """Build normalized match_score dict from job scoring data (browser or REST)."""
    if not score_data:
        return {"match_score": None, "match_details": None}

    skill_mismatch_str = score_data.get("skillMismatch") or ""
    # Normalize to consistent dict format (same shape as _fetch_match_score REST path)
    match_score = {
        "education": (score_data.get("education", {}).get("userMatching", False)
                      if isinstance(score_data.get("education"), dict)
                      else score_data.get("education")),
        "functional_area": (score_data.get("functionalArea", {}).get("userMatching", False)
                            if isinstance(score_data.get("functionalArea"), dict)
                            else score_data.get("functionalArea")),
        "key_skills": score_data.get("Keyskills"),
        "work_experience": (score_data.get("Experience", {}).get("userMatching", False)
                            if isinstance(score_data.get("Experience"), dict)
                            else score_data.get("workExperience")),
        "industry": (score_data.get("Industry", {}).get("userMatching", False)
                     if isinstance(score_data.get("Industry"), dict)
                     else score_data.get("industry")),
        "location": (score_data.get("Location", {}).get("userMatching", False)
                     if isinstance(score_data.get("Location"), dict)
                     else score_data.get("location")),
        "early_applicant": score_data.get("earlyApplicant", False),
    }
    match_details = {
        "skill_mismatch": [s.strip() for s in skill_mismatch_str.split(",") if s.strip()],
    }
    return {"match_score": match_score, "match_details": match_details}


def _parse_job_detail(details_data: dict, job_id: str, page_url: str,
                      score_data: dict = None) -> dict:
    """Parse job detail API response (v3 or v4 format) into a structured result dict."""
    job = details_data.get("jobDetails", details_data)

    salary_data = _parse_salary_data(job)
    company_data = _parse_company_data(job, details_data)
    match_data = _parse_match_score(score_data)

    is_applied = job.get("isApplied", False)
    external = bool(job.get("applyRedirectUrl"))

    result = {
        "status": "success",
        "job_id": job_id,
        "title": job.get("title"),
        "company": company_data["company_name"],
        "company_rating": company_data["company_rating"],
        "company_reviews_count": company_data["company_reviews_count"],
        "salary": salary_data["salary"],
        "experience": f"{job.get('minimumExperience', '?')}-{job.get('maximumExperience', '?')} years",
        "experience_min": job.get("minimumExperience"),
        "experience_max": job.get("maximumExperience"),
        "candidates_count": job.get("candidatesCount"),
        "location": (
            job.get("cityName")
            or job.get("citySuburb")
            or _extract_placeholder(job.get("placeholders", []), "location")
            or job.get("location")
        ),
        "description": job.get("description", ""),
        "skills": _extract_skills(job.get("keySkills", [])),
        "match_score": match_data["match_score"],
        "match_details": match_data["match_details"],
        "is_applied": is_applied,
        "external_apply": external,
        "can_apply": not is_applied and not external,
        "vacancies": job.get("vacany"),
        "apply_count": job.get("applyCount"),
        "hr_name": job.get("contactPerson") or job.get("createdBy"),
        "hr_email": job.get("contactEmail"),
        "external_apply_url": job.get("applyRedirectUrl"),
        "company_website": company_data["company_website"],
        "notice_period": job.get("noticePeriod"),
        "benefits": job.get("benefits"),
        "group_id": company_data["group_id"],
        "work_mode": job.get("workMode") or job.get("wfhType"),
        "posted_date": job.get("createdDate"),
        "industry": (
            job.get("industryType")
            or (job.get("industryTypeGid", {}).get("label")
                if isinstance(job.get("industryTypeGid"), dict) else None)
        ),
        "role_category": job.get("roleCategory"),
        "education": _format_education(job.get("education")) or job.get("qualification"),
        "job_role": job.get("jobRole"),
        "employment_type": job.get("employmentType"),
        "hybrid_detail": job.get("hybridWfhDetail"),
        "saved": bool(job.get("savedJobFlag")),
        "valid_through": job.get("validThrough") or job.get("expiryDate"),
        "department": job.get("department") or job.get("functionalArea"),
        "url": page_url,
    }
    # AmbitionBox enrichment from top-level v4 response
    ab = details_data.get("ambitionBoxDetails", {})
    if ab:
        salaries = ab.get("salaries", {})
        if salaries:
            result["salary_benchmarks"] = {
                "average": salaries.get("averageSalary"),
                "min": salaries.get("minSalary"),
                "max": salaries.get("maxSalary"),
            }
        benefits_data = ab.get("benefits", {})
        if isinstance(benefits_data, dict) and benefits_data.get("benefitsList"):
            result["benefits_list"] = benefits_data["benefitsList"]
        reviews_list = ab.get("reviews", [])
        if reviews_list:
            result["top_reviews"] = [
                {"rating": r.get("rating"), "role": r.get("role"), "title": r.get("title")}
                for r in reviews_list[:3] if isinstance(r, dict)
            ]

    warnings = validate_job_detail(result)
    if warnings:
        result["warnings"] = warnings
    return result


async def _fetch_match_score(job_id: str) -> dict | None:
    """Fetch per-job match score — optional enrichment, returns None on failure."""
    try:
        score_data = await api_client.get(f"{JOB_MATCH_SCORE_API}{job_id}/matchscore")
        return {
            "education": score_data.get("education"),
            "functional_area": score_data.get("functionalArea"),
            "key_skills": score_data.get("Keyskills"),
            "work_experience": score_data.get("workExperience"),
            "industry": score_data.get("industry"),
            "location": score_data.get("location"),
            "early_applicant": score_data.get("earlyApplicant"),
        }
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
                    await asyncio.wait_for(response_event.wait(), timeout=INTERCEPT_WAIT_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("Job details response capture timed out after %ss for job: %s", INTERCEPT_WAIT_TIMEOUT, job_id)
                # Give a moment for match score to arrive too
                if "score" not in captured:
                    await asyncio.sleep(BROWSER_PAGE_SETTLE)
            finally:
                page.remove_listener("response", on_response_with_event)

            details_data = captured.get("details")
            if not details_data:
                return {"status": "error", "message": "Job details API response not captured. Page may not have loaded.", "error_code": "API_ERROR"}

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
    """Get enriched job detail from V1 API — walk-in, contact, metrics, closing date."""
    if not job_id:
        return {"status": "error", "message": "job_id is required", "error_code": "VALIDATION_ERROR"}

    data = await api_client.get(JOB_DETAIL_V1_API + job_id)
    job = data.get("job", data)

    result = {
        "status": "success",
        "job_id": job.get("jobId"),
        "title": job.get("post"),
        "company": job.get("companyName"),
        "company_id": job.get("companyId"),
        "group_id": job.get("groupId"),
        # Walk-in (V1-unique)
        "is_walk_in": job.get("isWalkIn", False),
        "walkin_time": job.get("walkinTime"),
        "walkin_venue": job.get("walkinVenue"),
        "walkin_date_from": job.get("walkingDateFrom"),
        "walkin_date_to": job.get("walkingDateTo"),
        # Contact (V1-unique)
        "contact_name": job.get("contactName"),
        "contact_email": job.get("email") or None,
        "contact_phone": job.get("tel") or None,
        "contact_designation": job.get("CONTDESIG"),
        # Metrics (V1-unique)
        "jd_views": job.get("jdViews"),
        "jd_applies": job.get("jdApplies"),
        "vacancy": job.get("noOfVacancy") or None,
        # Dates
        "closing_date": job.get("closingDate"),
        "is_expired": job.get("isExpiredJob", False),
        "posted_date": job.get("addDate"),
        # Salary
        "salary_min": job.get("minSal"),
        "salary_max": job.get("maxSal"),
        "salary_hidden": job.get("showSal") == "n",
        # Experience
        "experience_min": job.get("minExp"),
        "experience_max": job.get("maxExp"),
        # Location
        "location": job.get("city"),
        "work_mode": {"0": "office", "2": "remote", "3": "hybrid"}.get(job.get("wfhType", "0"), "unknown"),
        # Content
        "description": job.get("jobDesc"),
        "company_profile": job.get("companyProfile") or None,
        "keywords": job.get("keywords"),
        "employment_type": job.get("employmentType"),
        # Education
        "ug_course": job.get("ugCourse"),
        "pg_course": job.get("pgCourse"),
        # Internship (V1-unique)
        "internship_start": job.get("internshipStartDate"),
        "internship_duration": job.get("internshipDuration"),
        "internship_ppo": job.get("internshipPpo"),
        # Status
        "is_saved": bool(job.get("isSavedJob", 0)),
        "is_consultant": job.get("cons") == "y",
        "hiring_for": job.get("hiringFor") or None,
    }

    # Strip None values
    return {k: v for k, v in result.items() if v is not None}


async def _get_similar_jobs_rest(job_id: str, limit: int = 10) -> dict:
    """Fetch similar jobs via v2 REST API (no browser needed).

    Uses /jobapi/v2/search/simjobs/{jobId} with lightweight parsing.

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
    sim = data.get("simJobDetails", {})
    jobs_raw = sim.get("content", []) + sim.get("collaborative", [])
    jobs = []
    for j in jobs_raw:
        placeholders = j.get("placeholders", [])
        location = ""
        for ph in placeholders:
            if ph.get("type") == "location":
                location = ph.get("label", "")
                break
        if not location and placeholders:
            location = placeholders[0].get("label", "")

        tags_raw = j.get("tagsAndSkills") or ""
        tags = [s.strip() for s in tags_raw.split(",") if s.strip()][:5] if isinstance(tags_raw, str) else tags_raw[:5]

        jobs.append({
            "job_id": j.get("jobId"),
            "title": j.get("title"),
            "company": j.get("companyName"),
            "salary": j.get("salaryDetail", {}).get("label", "Not Disclosed"),
            "location": location,
            "experience": j.get("experienceText", ""),
            "tags": tags,
            "url": j.get("jdURL", ""),
        })
    return {
        "status": "success",
        "job_id": job_id,
        "source": "rest_api",
        "total": data.get("noOfJobs", len(jobs)),
        "count": len(jobs[:limit]),
        "jobs": jobs[:limit],
    }


_VALID_PARAMS_PER_ACTION = {
    "get": {"job_id"},
    "similar": {"job_id", "limit", "page"},
    "compare": {"job_ids", "timeout_seconds"},
    "report_fraud": {"job_id", "reason"},
    "detail_v1": {"job_id"},
    "bulk": {"job_ids"},
}


@mcp.tool()
async def naukri_jobs(
    action: str = "get",
    job_id: Optional[str] = None,
    reason: Optional[str] = None,
    # similar params
    limit: int = 10,
    page: int = 1,
    # compare params
    job_ids: Optional[list[str]] = None,
    timeout_seconds: int = 120,
) -> dict:
    """Unified job operations — fetch details, find similar, compare, or report fraud.

    Actions:
        get: Fetch full job details (REST-first, browser fallback). Accepts job_id or full URL.
        similar: Find jobs similar to a given job_id.
        compare: Compare 2-5 jobs side-by-side with fit scores.
        report_fraud: Report a fraudulent job listing.
        detail_v1: Get V1 job detail — walk-in info, contact details, jd views/applies, closing date
        bulk: Fetch multiple jobs at once — up to 20 job IDs in one call

    Args:
        action: "get", "similar", "compare", "report_fraud", "detail_v1", or "bulk"
        job_id: Job ID or full Naukri URL (required for get, similar, report_fraud, detail_v1)
        reason: Reason for fraud report (required for report_fraud)
        limit: Max similar jobs to return (default 10, for similar action)
        page: Page number for similar jobs (default 1, for similar action)
        job_ids: List of 2-5 job IDs to compare (required for compare action), or up to 20 for bulk
        timeout_seconds: Max seconds for compare before timeout (default 120)

    Returns:
        get: {status, title, company, salary, experience, location, skills, match_score, is_applied, can_apply, url, ...}
        similar: {status, job_id, source, total, count, page, has_more, jobs: [...]}
        compare: {status, count, jobs: [...], common_skills, all_skills, best_match_job_id, average_fit_score}
        report_fraud: {status, message}
        detail_v1: {status, job_id, title, company, is_walk_in, contact_name, jd_views, jd_applies, closing_date, ...}
        bulk: {status, count, jobs: [...]}
    """
    # ── ISP: warn about params irrelevant to chosen action ─────────────
    _provided = {
        "job_id": job_id, "reason": reason,
        "limit": limit if limit != 10 else None,
        "page": page if page != 1 else None,
        "job_ids": job_ids,
        "timeout_seconds": timeout_seconds if timeout_seconds != 120 else None,
    }
    _unused = validate_action_params(action, _provided, _VALID_PARAMS_PER_ACTION)

    def _attach_unused(result: dict) -> dict:
        if _unused and isinstance(result, dict):
            result["unused_params"] = _unused
        return result

    if action == "get":
        if not job_id:
            return {"status": "error", "message": "job_id required", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await _get_job(job_id_or_url=job_id))
    elif action == "similar":
        if not job_id:
            return {"status": "error", "message": "similar requires job_id.", "error_code": "VALIDATION_ERROR"}

        async def _similar():
            # REST-first: lightweight v2 API, no browser needed
            try:
                return await _get_similar_jobs_rest(job_id, limit)
            except Exception:
                pass
            # Fallback: full parser via search module helper
            from naukri_server.tools.search import _get_similar_jobs
            return await _get_similar_jobs(job_id=job_id, limit=limit, page=page)

        return _attach_unused(await handle_tool_action(_similar, "jobs.similar"))
    elif action == "compare":
        if not job_ids:
            return {"status": "error", "message": "compare requires job_ids (list of 2-5 IDs).", "error_code": "VALIDATION_ERROR"}
        from naukri_server.tools.compare import _compare_jobs
        return _attach_unused(await handle_tool_action(
            lambda: _compare_jobs(job_ids=job_ids, timeout_seconds=timeout_seconds), "jobs.compare"))
    elif action == "bulk":
        if not job_ids:
            return {"status": "error", "message": "bulk requires job_ids (list of 1-20 IDs).", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await handle_tool_action(
            lambda: _bulk_fetch_jobs(job_ids=job_ids), "jobs.bulk"))
    elif action == "detail_v1":
        if not job_id:
            return {"status": "error", "message": "job_id required for detail_v1", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await handle_tool_action(
            lambda: _get_job_v1(job_id=job_id), "jobs.detail_v1"))
    elif action == "report_fraud":
        if not job_id:
            return {"status": "error", "message": "job_id required for report_fraud", "error_code": "VALIDATION_ERROR"}
        if not reason:
            return {"status": "error", "message": "reason required for report_fraud", "error_code": "VALIDATION_ERROR"}
        return _attach_unused(await handle_tool_action(
            lambda: _report_fraud(job_id=job_id, reason=reason), "jobs.report_fraud"))
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: get, similar, compare, bulk, detail_v1, report_fraud", "error_code": "VALIDATION_ERROR"}
