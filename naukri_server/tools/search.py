import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.browser import browser, page_goto
from naukri_server.config import NAUKRI_BASE, RECOMMENDED_JOBS_API, SIMILAR_JOBS_API, logger


@mcp.tool()
async def naukri_search_jobs(
    keywords: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    limit: int = 20,
    page: int = 1,
) -> dict:
    """Search for jobs on Naukri.com.

    Navigates to the search page and intercepts the structured JSON response
    that Naukri's frontend receives from its search API.

    Args:
        keywords: Job title or skills (e.g., "python developer", "react")
        location: City name (e.g., "Bangalore", "Mumbai", "Remote")
        experience: Years of experience filter (e.g., 5)
        limit: Max jobs to return (default 20, max 50)
        page: Page number for pagination (default 1)

    Returns:
        - {status: "success", keywords, location, page, total_found, count, jobs: [{job_id, title, company, salary, location, experience, is_applied, posted_date, tags, url}]}
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
            if page_no > 1:
                params.append(f"pageNo={page_no}")
            if params:
                page_url += "?" + "&".join(params)

            # Intercept the search API response that Naukri's frontend makes
            captured = {}
            response_event = asyncio.Event()

            async def on_response(response):
                if "/jobapi/v3/search" in response.url and response.status == 200:
                    try:
                        captured["data"] = await response.json()
                    except Exception as e:
                        logger.debug("search response parse failed: %s", e)
                    response_event.set()

            page.on("response", on_response)
            try:
                await page_goto(page, page_url)
                try:
                    await asyncio.wait_for(response_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass  # Fall through to check captured data
            finally:
                page.remove_listener("response", on_response)

            data = captured.get("data")
            if not data:
                return {"status": "error", "message": "Search API response not captured. Page may not have loaded correctly."}

            jobs = _parse_job_list(data.get("jobDetails", []), limit)

            return {
                "status": "success",
                "keywords": keywords,
                "location": location,
                "page": page_no,
                "total_found": data.get("noOfJobs"),
                "count": len(jobs),
                "jobs": jobs,
            }
        except Exception as e:
            return {"status": "error", "message": f"Search failed: {type(e).__name__}: {e!r}"}


def _parse_job_list(job_details: list, limit: int) -> list:
    """Parse Naukri's jobDetails array into a clean list of job dicts."""
    jobs = []
    for job in job_details[:limit]:
        salary = job.get("salaryDetail", {})
        sal_min = salary.get("minimumSalary", 0)
        sal_max = salary.get("maximumSalary", 0)
        sal_label = salary.get("label", "")
        salary_str = sal_label if sal_label else (
            f"{sal_min/100000:.1f}-{sal_max/100000:.1f} LPA" if sal_max else "Not Disclosed"
        )

        placeholders = job.get("placeholders", [])
        loc_label = None
        for ph in placeholders:
            if ph.get("type") == "location":
                loc_label = ph.get("label")
                break
        if not loc_label and placeholders:
            loc_label = placeholders[0].get("label")

        jobs.append({
            "job_id": job.get("jobId"),
            "title": job.get("title"),
            "company": job.get("companyName"),
            "salary": salary_str,
            "location": loc_label,
            "experience": f"{job.get('minimumExperience', '?')}-{job.get('maximumExperience', '?')} Yrs",
            "is_applied": job.get("isApplied", False),
            "posted_date": job.get("createdDate") or job.get("footerPlaceholderLabel"),
            "job_age": job.get("jobAge"),
            "tags": [t.strip() for t in job.get("tagsAndSkills", "").split(",") if t.strip()] if isinstance(job.get("tagsAndSkills"), str) else job.get("tagsAndSkills", []),
            "url": f"{NAUKRI_BASE}/job-listings-{job.get('jobId', '')}",
        })
    return jobs


@mcp.tool()
async def naukri_get_recommendations(limit: int = 20) -> dict:
    """Get personalized job recommendations from Naukri's algorithm based on your profile.

    Args:
        limit: Max jobs to return (default 20, max 50)

    Returns:
        - {status: "success", source: "recommendations", total_found, count, jobs: [{job_id, title, company, ...}]}
        - {status: "error", message}
    """
    try:
        data = await api_post(RECOMMENDED_JOBS_API, body={})
        job_details = data.get("jobDetails", [])
        jobs = _parse_job_list(job_details, limit)
        return {
            "status": "success",
            "source": "recommendations",
            "total_found": data.get("noOfJobs"),
            "count": len(jobs),
            "jobs": jobs,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get recommendations: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_similar_jobs(job_id: str, limit: int = 10) -> dict:
    """Get jobs similar to a given job posting on Naukri.com.

    Args:
        job_id: The Naukri job ID to find similar jobs for
        limit: Max jobs to return (default 10)

    Returns:
        - {status: "success", job_id, source: "similar", total_found, count, jobs: [{job_id, title, company, ...}]}
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
        return {
            "status": "success",
            "job_id": job_id,
            "source": "similar",
            "total_found": data.get("noOfJobs"),
            "count": len(jobs),
            "jobs": jobs,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get similar jobs: {type(e).__name__}: {e}"}
