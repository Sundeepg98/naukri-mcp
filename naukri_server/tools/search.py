import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_post, NaukriAPIError
from naukri_server.browser import browser
from naukri_server.config import NAUKRI_BASE, RECOMMENDED_JOBS_API


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
    async with browser._lock:
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
            if page > 1:
                params.append(f"pageNo={page}")
            if params:
                page_url += "?" + "&".join(params)

            # Intercept the search API response that Naukri's frontend makes
            captured = {}
            response_event = asyncio.Event()

            async def on_response(response):
                if "/jobapi/v3/search" in response.url and response.status == 200:
                    try:
                        captured["data"] = await response.json()
                    except Exception:
                        pass
                    response_event.set()

            browser.page.on("response", on_response)
            try:
                await browser.goto(page_url)
                try:
                    await asyncio.wait_for(response_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass  # Fall through to check captured data
            finally:
                browser.page.remove_listener("response", on_response)

            data = captured.get("data")
            if not data:
                return {"status": "error", "message": "Search API response not captured. Page may not have loaded correctly."}

            # Parse structured JSON from the intercepted response
            jobs = []
            for job in data.get("jobDetails", [])[:limit]:
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

            return {
                "status": "success",
                "keywords": keywords,
                "location": location,
                "page": page,
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
