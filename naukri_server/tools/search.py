from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.browser import browser, page_goto, page_intercept_json
from naukri_server.config import NAUKRI_BASE, RECOMMENDED_JOBS_API, SIMILAR_JOBS_API, logger
from naukri_server.tools.job_parsing import _parse_job_list
from naukri_server.validation import validate_job_list


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
    limit: int = 20,
    page: int = 1,
) -> dict:
    """Search for jobs on Naukri.com by keywords, location, and experience.

    Search by keywords/location. For personalized suggestions based on your profile,
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
        limit: Max jobs to return (default 20, max 50)
        page: Page number for pagination (default 1)

    Returns:
        - {status: "success", keywords, location, page, total, count, filters, jobs: [{job_id, title, company, salary, location, experience, is_applied, posted_date, tags, url}]}
        - {status: "error", message}
    """
    page_no = page  # save before shadowing by page_pool
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
            if page_no > 1:
                params.append(f"pageNo={page_no}")
            if params:
                page_url += "?" + "&".join(params)

            data = await page_intercept_json(page, page_url, url_pattern="/jobapi/v3/search")
            if not data:
                return {"status": "error", "message": "Search API response not captured. Try again."}

            jobs = _parse_job_list(data.get("jobDetails", []), limit)

            result = {
                "status": "success",
                "keywords": keywords,
                "location": location,
                "page": page_no,
                "total": data.get("noOfJobs"),
                "count": len(jobs),
                "filters": {k: v for k, v in {
                    "experience": experience,
                    "salary_min": salary_min,
                    "salary_max": salary_max,
                    "sort_by": sort_by,
                    "freshness": freshness,
                    "work_mode": work_mode,
                }.items() if v is not None},
                "jobs": jobs,
            }
            warnings = validate_job_list(jobs, data.get("noOfJobs"), "search")
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as e:
            return {"status": "error", "message": f"Search failed: {type(e).__name__}: {e!r}"}


@mcp.tool()
async def naukri_get_recommendations(limit: int = 20) -> dict:
    """Get personalized job recommendations from Naukri's algorithm based on your profile.

    AI-recommended jobs based on your profile, skills, and activity.
    For keyword-based search, use naukri_search_jobs instead.

    Args:
        limit: Max jobs to return (default 20, max 50)

    Returns:
        - {status: "success", source: "recommendations", total, count, jobs: [{job_id, title, company, ...}]}
        - {status: "error", message}
    """
    try:
        data = await api_post(RECOMMENDED_JOBS_API, body={})
        job_details = data.get("jobDetails", [])
        jobs = _parse_job_list(job_details, limit)
        result = {
            "status": "success",
            "source": "recommendations",
            "total": data.get("noOfJobs"),
            "count": len(jobs),
            "jobs": jobs,
        }
        warnings = validate_job_list(jobs, data.get("noOfJobs"), "recommendations")
        if warnings:
            result["warnings"] = warnings
        return result
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get recommendations: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_similar_jobs(job_id: str, limit: int = 10) -> dict:
    """Get jobs similar to a given job posting on Naukri.com.

    Requires: a job_id from naukri_search_jobs or naukri_get_job results.

    Args:
        job_id: The Naukri job ID to find similar jobs for
        limit: Max jobs to return (default 10)

    Returns:
        - {status: "success", job_id, source: "similar", total, count, jobs: [{job_id, title, company, ...}]}
        - {status: "error", message}
    """
    try:
        data = await api_get(SIMILAR_JOBS_API + job_id, params={
            "noOfResults": str(limit),
            "searchType": "sim",
        })
        # Similar jobs uses simJobDetails with content + collaborative arrays
        sim = data.get("simJobDetails", {})
        job_details = sim.get("content", []) + sim.get("collaborative", [])
        jobs = _parse_job_list(job_details, limit)
        result = {
            "status": "success",
            "job_id": job_id,
            "source": "similar",
            "total": data.get("noOfJobs"),
            "count": len(jobs),
            "jobs": jobs,
        }
        warnings = validate_job_list(jobs, data.get("noOfJobs"), "similar_jobs")
        if warnings:
            result["warnings"] = warnings
        return result
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get similar jobs: {type(e).__name__}: {e}"}
