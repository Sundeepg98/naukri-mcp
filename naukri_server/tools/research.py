"""Unified company research — combines search, jobs, salary, and reviews in one call."""

from typing import Optional

from naukri_server import mcp
from naukri_server.config import logger


@mcp.tool()
async def naukri_research_company(
    keyword: str,
    include_jobs: bool = True,
    include_reviews: bool = True,
    jobs_limit: int = 5,
) -> dict:
    """Research a company comprehensively — search, jobs, salary, and reviews in one call.

    Combines: naukri_search_companies → naukri_get_company_jobs → naukri_get_company_slug
    → naukri_get_company_salary → naukri_get_company_reviews

    For individual company operations, use the specific tools instead.

    Args:
        keyword: Company name to search for (e.g., "Google", "Infosys")
        include_jobs: Include open job listings (default True)
        include_reviews: Include AmbitionBox salary/review data (default True)
        jobs_limit: Max jobs to include (default 5)

    Returns:
        - {status: "success", company: {name, group_id, type, industry, size, rating, review_count},
           jobs: {total, sample: [...]},
           salary: {avg_salary, min_salary, max_salary, ...},
           reviews: {overall_rating, category_ratings, reviews: [...]},
           errors: [any partial failures]}
        - {status: "error", message}
    """
    # Import tool functions from their modules (avoiding circular imports)
    from naukri_server.tools.companies import naukri_search_companies, naukri_get_company_jobs
    from naukri_server.tools.ambitionbox import naukri_get_company_slug, naukri_get_company_salary, naukri_get_company_reviews

    errors = []

    # Step 1: Search for the company
    search_result = await naukri_search_companies(keyword=keyword, limit=1)
    if search_result.get("status") != "success" or not search_result.get("companies"):
        return {
            "status": "error",
            "message": f"Company '{keyword}' not found on Naukri. Try a different name.",
        }

    company = search_result["companies"][0]
    group_id = company.get("group_id")
    result = {
        "status": "success",
        "company": company,
    }

    # Step 2: Get company jobs (if requested)
    if include_jobs and group_id:
        try:
            jobs_result = await naukri_get_company_jobs(group_id=str(group_id), limit=jobs_limit)
            if jobs_result.get("status") == "success":
                result["jobs"] = {
                    "total": jobs_result.get("total"),
                    "sample": jobs_result.get("jobs", []),
                }
            else:
                errors.append(f"Jobs: {jobs_result.get('message', 'unknown error')}")
        except Exception as e:
            errors.append(f"Jobs: {type(e).__name__}: {e}")
            logger.warning("Research company jobs failed: %s", e)

    # Step 3: Get AmbitionBox data (if requested)
    if include_reviews and group_id:
        # First, get the company slug
        slug = None
        try:
            slug_result = await naukri_get_company_slug(group_id=str(group_id))
            if slug_result.get("status") == "success":
                slug = slug_result.get("company_slug")
            else:
                errors.append(f"Slug: {slug_result.get('message', 'unknown error')}")
        except Exception as e:
            errors.append(f"Slug: {type(e).__name__}: {e}")
            logger.warning("Research company slug failed: %s", e)

        if slug:
            # Get salary data
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

            # Get reviews
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
