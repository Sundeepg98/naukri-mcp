"""Unified company research — combines job search, salary, and reviews in one call."""

import re

from naukri_server import mcp
from naukri_server.config import logger


def _derive_slug(company_name: str) -> str:
    """Derive an AmbitionBox-style slug from a company name."""
    name = company_name.strip()
    for suffix in ("Pvt. Ltd.", "Pvt Ltd", "Private Limited", "Ltd.", "Ltd",
                   "Limited", "Inc.", "Inc", "Corp.", "Corp", "Corporation",
                   "LLP", "LLC", "Technologies", "Technology", "Solutions",
                   "Services", "India"):
        if name.lower().endswith(suffix.lower()):
            name = name[:len(name) - len(suffix)].strip()
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    slug = re.sub(r'-+', '-', slug)
    return slug


@mcp.tool()
async def naukri_research_company(
    keyword: str,
    include_jobs: bool = True,
    include_reviews: bool = True,
    jobs_limit: int = 5,
) -> dict:
    """Research a company comprehensively — jobs, salary data, and employee reviews.

    Searches for jobs at the company on Naukri, then fetches AmbitionBox
    salary and review data. Handles partial failures gracefully.

    Args:
        keyword: Company name (e.g., "Google", "Infosys", "TCS")
        include_jobs: Include open job listings (default True)
        include_reviews: Include AmbitionBox salary/review data (default True)
        jobs_limit: Max jobs to include (default 5)

    Returns:
        - {status: "success", company_name, slug,
           jobs: {total, sample: [...]},
           salary: {avg_salary, min_salary, max_salary, ...},
           reviews: {overall_rating, category_ratings, reviews: [...]},
           errors: [any partial failures]}
        - {status: "error", message}
    """
    from naukri_server.tools.search import naukri_search_jobs
    from naukri_server.tools.ambitionbox import (
        naukri_get_company_salary, naukri_get_company_reviews,
    )

    errors = []
    slug = _derive_slug(keyword)

    result = {
        "status": "success",
        "company_name": keyword,
        "slug": slug,
    }

    # Step 1: Search for jobs at this company via Naukri job search
    if include_jobs:
        try:
            jobs_result = await naukri_search_jobs(
                keywords=keyword, limit=jobs_limit,
            )
            if jobs_result.get("status") == "success":
                jobs = jobs_result.get("jobs", [])
                # Filter to jobs whose company name matches the keyword
                keyword_lower = keyword.lower()
                matching = [j for j in jobs if keyword_lower in (j.get("company") or "").lower()]
                result["jobs"] = {
                    "total": jobs_result.get("total"),
                    "matching": len(matching),
                    "sample": matching if matching else jobs[:jobs_limit],
                }
            else:
                errors.append(f"Jobs: {jobs_result.get('message', 'unknown error')}")
        except Exception as e:
            errors.append(f"Jobs: {type(e).__name__}: {e}")
            logger.warning("Research company jobs failed: %s", e)

    # Step 2: Get AmbitionBox salary + reviews (if requested)
    if include_reviews:
        # Try the derived slug first; common company names work directly
        try:
            salary_result = await naukri_get_company_salary(company_slug=slug)
            if salary_result.get("status") == "success":
                result["salary"] = {
                    k: v for k, v in salary_result.items()
                    if k not in ("status", "url", "_debug_page_props_keys")
                }
            else:
                errors.append(f"Salary: {salary_result.get('message', 'unknown error')}")
        except Exception as e:
            errors.append(f"Salary: {type(e).__name__}: {e}")
            logger.warning("Research company salary failed: %s", e)

        try:
            reviews_result = await naukri_get_company_reviews(company_slug=slug)
            if reviews_result.get("status") == "success":
                result["reviews"] = {
                    k: v for k, v in reviews_result.items()
                    if k not in ("status", "url")
                }
            else:
                errors.append(f"Reviews: {reviews_result.get('message', 'unknown error')}")
        except Exception as e:
            errors.append(f"Reviews: {type(e).__name__}: {e}")
            logger.warning("Research company reviews failed: %s", e)

    if errors:
        result["errors"] = errors

    return result
