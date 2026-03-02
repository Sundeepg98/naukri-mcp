from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.browser import browser, page_intercept_json
from naukri_server.config import NAUKRI_BASE, RECOMMENDED_JOBS_API, SEARCH_API, SIMILAR_JOBS_API, logger
from naukri_server.tools.job_parsing import _parse_job_list
from naukri_server.validation import validate_job_list, validate_limit, validate_page


@mcp.tool()
async def naukri_search_jobs(
    keywords: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    salary_min: Optional[int] = None,
    salary_max: Optional[int] = None,
    sort_by: Optional[str] = None,
    freshness: Optional[int] = None,
    work_mode: Optional[str] = None,
    job_type: Optional[str] = None,
    company_type: Optional[str] = None,
    industry: Optional[str] = None,
    education: Optional[str] = None,
    role_category: Optional[str] = None,
    posted_within: Optional[int] = None,
    limit: int = 20,
    page: int = 1,
) -> dict:
    """Search for jobs on Naukri.com by keywords, location, and filters.

    This tool searches by keywords and filters — it does NOT use your Naukri profile.
    For AI-recommended jobs based on your profile (no keywords needed),
    use naukri_get_recommendations instead.

    Args:
        keywords: Job title or skills (e.g., "python developer", "react")
        location: City name (e.g., "Bangalore", "Mumbai", "Remote")
        experience: Years of experience filter (e.g., 5)
        salary_min: Minimum CTC in LPA (e.g., 15 for 15 LPA)
        salary_max: Maximum CTC in LPA (e.g., 50 for 50 LPA)
        sort_by: Sort order — "relevance" (default), "date", or "salary"
        freshness: Posted within N days — 1, 3, 7, 15, or 30
        work_mode: Work arrangement — "wfh", "hybrid", or "office"
        job_type: Filter by job type — "fulltime", "parttime", "contract", "internship", "temporary"
        company_type: Filter by company type — "startup", "mnc", "indian_mnc", "corporate"
        industry: Industry sector (e.g., "IT-Software", "Banking", "Healthcare")
        education: Minimum education — "any_graduate", "b_tech", "mba", "m_tech", "diploma", "phd"
        role_category: Role category (e.g., "Software Development", "Data Science")
        posted_within: Jobs posted within N days (overrides freshness if both set)
        limit: Max jobs to return (default 20, max 50)
        page: Page number for pagination (default 1)

    Returns:
        - {status: "success", keywords, location, page, total, count, has_more, filters, jobs: [{job_id, title, company, salary, location, experience, is_applied, posted_date, tags, url}]}
        - {status: "error", message}
    """
    limit = validate_limit(limit)
    page = validate_page(page)
    page_no = page  # save before shadowing by page_pool

    # REST-first search — faster than browser intercept; falls back on failure
    try:
        rest_params = {"keyword": keywords, "noOfResults": str(limit), "pageNo": str(page_no)}
        if location:
            rest_params["location"] = location
        if experience is not None:
            rest_params["experience"] = str(experience)
        if salary_min is not None:
            rest_params["salary"] = f"{salary_min}-{salary_max or 999}"
        if sort_by:
            rest_params["sortBy"] = sort_by
        if freshness is not None:
            rest_params["jobAge"] = str(freshness)
        if industry:
            rest_params["industry"] = industry
        if education:
            rest_params["education"] = education
        if role_category:
            rest_params["roleCategory"] = role_category
        if job_type:
            _JOB_TYPE_MAP = {"fulltime": "1", "parttime": "2", "contract": "3", "internship": "4", "temporary": "5"}
            jt = _JOB_TYPE_MAP.get(job_type.lower())
            if jt:
                rest_params["jobType"] = jt
        if company_type:
            rest_params["companyType"] = company_type
        if work_mode:
            rest_params["wfhType"] = work_mode
        if posted_within is not None:
            rest_params["jobAge"] = str(posted_within)

        data = await api_get(SEARCH_API, params=rest_params)
        if data and isinstance(data, dict) and data.get("jobDetails"):
            jobs = _parse_job_list(data.get("jobDetails", []), limit)
            total = data.get("noOfJobs") or data.get("totalCount") or len(jobs)
            result = {
                "status": "success",
                "keywords": keywords,
                "location": location,
                "page": page_no,
                "total": total,
                "count": len(jobs),
                "has_more": (page_no * limit) < total if isinstance(total, int) else len(jobs) == limit,
                "filters": {k: v for k, v in {
                    "experience": experience,
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                    "sort_by": sort_by,
                    "freshness": freshness,
                    "work_mode": work_mode,
                    "job_type": job_type,
                    "company_type": company_type,
                    "industry": industry,
                    "education": education,
                    "role_category": role_category,
                    "posted_within": posted_within,
                }.items() if v is not None},
                "jobs": jobs,
                "search_path": "rest",
            }
            warnings = validate_job_list(jobs, data.get("noOfJobs"), "search")
            if warnings:
                result["warnings"] = warnings
            return result
    except Exception as e:
        logger.info("REST search failed (%s), falling back to browser", e)

    async with browser.page_pool.acquire() as page:
        try:
            # Build Naukri search URL (SEO-friendly format)
            slug = keywords.lower().replace(" ", "-").replace(".", "-")
            if location:
                loc_slug = location.lower().replace(" ", "-")
                page_url = f"{NAUKRI_BASE}/{slug}-jobs-in-{loc_slug}"
            else:
                page_url = f"{NAUKRI_BASE}/{slug}-jobs"
            params = []
            if experience is not None:
                params.append(f"experience={experience}")
            if salary_min is not None:
                params.append(f"nignbekg={salary_min}")
            if salary_max is not None:
                params.append(f"naagnbekg={salary_max}")
            if sort_by:
                params.append(f"sortBy={sort_by}")
            if freshness is not None:
                params.append(f"jobAge={freshness}")
            if work_mode:
                wm_map = {"wfh": "1", "hybrid": "3", "office": "2"}
                wm_val = wm_map.get(work_mode.lower())
                if wm_val:
                    params.append(f"wfhType={wm_val}")
            if job_type:
                _JOB_TYPE_MAP = {"fulltime": "1", "parttime": "2", "contract": "3", "internship": "4", "temporary": "5"}
                jt = _JOB_TYPE_MAP.get(job_type.lower())
                if jt:
                    params.append(f"jobType={jt}")
            if company_type:
                _COMPANY_TYPE_MAP = {"startup": "startup", "mnc": "mnc", "indian_mnc": "indianMNC", "corporate": "corporate"}
                ct = _COMPANY_TYPE_MAP.get(company_type.lower())
                if ct:
                    params.append(f"companyType={ct}")
            if industry:
                params.append(f"industry={industry}")
            if education:
                _EDU_MAP = {"any_graduate": "0", "b_tech": "35", "mba": "41", "m_tech": "36", "diploma": "23", "phd": "46"}
                edu = _EDU_MAP.get(education.lower(), education)
                params.append(f"education={edu}")
            if role_category:
                params.append(f"roleCategory={role_category}")
            if posted_within is not None:
                params.append(f"jobAge={posted_within}")
            if page_no > 1:
                params.append(f"pageNo={page_no}")
            if params:
                page_url += "?" + "&".join(params)

            data = await page_intercept_json(page, page_url, url_pattern="/jobapi/v3/search")
            if not data:
                return {"status": "error", "message": "Search API response not captured. Try again.", "error_code": "API_ERROR"}

            jobs = _parse_job_list(data.get("jobDetails", []), limit)

            total_jobs = data.get("noOfJobs") or 0
            result = {
                "status": "success",
                "keywords": keywords,
                "location": location,
                "page": page_no,
                "total": total_jobs,
                "count": len(jobs),
                "has_more": (page_no * limit) < total_jobs,
                "clusters": data.get("clusters", {}),
                "filters": {k: v for k, v in {
                    "experience": experience,
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                    "sort_by": sort_by,
                    "freshness": freshness,
                    "work_mode": work_mode,
                    "job_type": job_type,
                    "company_type": company_type,
                    "industry": industry,
                    "education": education,
                    "role_category": role_category,
                    "posted_within": posted_within,
                }.items() if v is not None},
                "jobs": jobs,
                "search_path": "browser",
            }
            warnings = validate_job_list(jobs, data.get("noOfJobs"), "search")
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as e:
            return {"status": "error", "message": f"Search failed: {type(e).__name__}: {e!r}", "error_code": "API_ERROR"}


@mcp.tool()
@api_tool("Get recommendations")
async def naukri_get_recommendations(limit: int = 20, page: int = 1) -> dict:
    """Get personalized job recommendations from Naukri's algorithm based on your profile.

    Returns jobs matched to YOUR profile, skills, and activity — no keywords or
    filters needed (the algorithm decides). For keyword/filter-based search,
    use naukri_search_jobs instead.

    Args:
        limit: Max jobs to return (default 20, max 50)
        page: Page number for pagination (default 1). Note: Naukri's recommendations API returns a single batch; pagination is applied client-side.

    Returns:
        - {status: "success", source: "recommendations", total, count, page, has_more, jobs: [{job_id, title, company, ...}]}
        - {status: "error", message}
    """
    limit = validate_limit(limit)
    page = validate_page(page)
    data = await api_post(RECOMMENDED_JOBS_API, body={})
    job_details = data.get("jobDetails", [])
    all_jobs = _parse_job_list(job_details, len(job_details))
    total = data.get("noOfJobs") or len(all_jobs)
    offset = (page - 1) * limit
    jobs = all_jobs[offset:offset + limit]
    result = {
        "status": "success",
        "source": "recommendations",
        "total": total,
        "count": len(jobs),
        "page": page,
        "has_more": (offset + limit) < total,
        "jobs": jobs,
    }
    warnings = validate_job_list(jobs, total, "recommendations")
    if warnings:
        result["warnings"] = warnings
    return result


async def _get_similar_jobs(job_id: str, limit: int = 10, page: int = 1) -> dict:
    """Get jobs similar to a given job posting on Naukri.com.

    Requires: a job_id from naukri_search_jobs or naukri_get_job results.

    Args:
        job_id: The Naukri job ID to find similar jobs for
        limit: Max jobs to return (default 10)
        page: Page number for pagination (default 1). Note: similar jobs API returns a single batch; pagination is applied client-side.

    Returns:
        - {status: "success", job_id, source: "similar", total, count, page, has_more, jobs: [{job_id, title, company, ...}]}
        - {status: "error", message}
    """
    try:
        limit = validate_limit(limit)
        page = validate_page(page)
        data = await api_get(SIMILAR_JOBS_API + job_id, params={
            "noOfResults": str(limit * page),
            "searchType": "sim",
        })
        # Similar jobs uses simJobDetails with content + collaborative arrays
        sim = data.get("simJobDetails", {})
        job_details = sim.get("content", []) + sim.get("collaborative", [])
        all_jobs = _parse_job_list(job_details, len(job_details))
        total = data.get("noOfJobs") or len(all_jobs)
        offset = (page - 1) * limit
        jobs = all_jobs[offset:offset + limit]
        result = {
            "status": "success",
            "job_id": job_id,
            "source": "similar",
            "total": total,
            "count": len(jobs),
            "page": page,
            "has_more": (offset + limit) < total,
            "jobs": jobs,
        }
        warnings = validate_job_list(jobs, total, "similar_jobs")
        if warnings:
            result["warnings"] = warnings
        return result
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "AUTH_ERROR" if e.status == 401 else "API_ERROR"}
    except Exception as e:
        return {"status": "error", "message": f"Get similar jobs failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


naukri_get_similar_jobs = _get_similar_jobs
