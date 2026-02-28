import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser
from naukri_server.config import NAUKRI_BASE


@mcp.tool()
async def naukri_search_jobs(
    keywords: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    limit: int = 20,
) -> dict:
    """Search for jobs on Naukri.com.

    Navigates to the search page and intercepts the structured JSON response
    that Naukri's frontend receives from its search API.

    Args:
        keywords: Job title or skills (e.g., "python developer", "react")
        location: City name (e.g., "Bangalore", "Mumbai", "Remote")
        experience: Years of experience filter (e.g., 5)
        limit: Max jobs to return (default 20, max 50)

    Returns list of jobs with id, title, company, salary, location, URL.
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
            if experience is not None:
                page_url += f"?experience={experience}"

            # Intercept the search API response that Naukri's frontend makes
            captured = {}

            async def on_response(response):
                if "/jobapi/v3/search" in response.url and response.status == 200:
                    try:
                        captured["data"] = await response.json()
                    except Exception:
                        pass

            browser.page.on("response", on_response)
            try:
                await browser.goto(page_url)
                await asyncio.sleep(4)
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
                    "url": f"{NAUKRI_BASE}/job-listings-{job.get('jobId', '')}",
                })

            return {
                "status": "success",
                "keywords": keywords,
                "location": location,
                "total_found": data.get("noOfJobs"),
                "count": len(jobs),
                "jobs": jobs,
            }
        except Exception as e:
            return {"status": "error", "message": f"Search failed: {type(e).__name__}: {e!r}"}
