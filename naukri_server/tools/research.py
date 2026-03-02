"""Unified company research — combines job search, salary, and reviews in one call."""
import asyncio

from naukri_server import mcp
from naukri_server.config import logger
from naukri_server.utils import derive_slug


@mcp.tool()
async def naukri_research_company(
    keyword: str,
    include_jobs: bool = True,
    include_reviews: bool = True,
    include_interviews: bool = True,
    jobs_limit: int = 5,
) -> dict:
    """Research a company comprehensively — jobs, salary data, reviews, and interview experiences.

    Searches for jobs at the company on Naukri, then fetches AmbitionBox
    salary and review data. Handles partial failures gracefully.

    Args:
        keyword: Company name (e.g., "Google", "Infosys", "TCS")
        include_jobs: Include open job listings (default True)
        include_reviews: Include AmbitionBox salary/review data (default True)
        include_interviews: Include AmbitionBox interview experiences (default True)
        jobs_limit: Max jobs to include (default 5)

    Returns:
        - {status: "success", company_name, slug,
           jobs: {total, sample: [...]},
           salary: {avg_salary, min_salary, max_salary, ...},
           reviews: {overall_rating, category_ratings, reviews: [...]},
           errors: [any partial failures]}
        - {status: "error", message}
    """
    from naukri_server.tools.ambitionbox import (
        _fetch_salary, _fetch_reviews, _fetch_interviews,
    )

    errors = []
    slug = derive_slug(keyword)

    result = {
        "status": "success",
        "company_name": keyword,
        "slug": slug,
    }

    # Step 1: Search for jobs at this company
    if include_jobs:
        try:
            from naukri_server.tools.companies import _search_companies, _get_company_jobs

            # Step 1: Find the company's group_id
            company_result = await _search_companies(keyword=keyword)
            group_id = None
            if company_result.get("status") == "success":
                companies = company_result.get("companies", [])
                if companies:
                    group_id = companies[0].get("group_id")
                    result["company_name"] = companies[0].get("name") or keyword

            if group_id:
                # Step 2: Get company jobs by group_id (exact match)
                jobs_result = await _get_company_jobs(group_id=str(group_id), limit=jobs_limit)
                if jobs_result.get("status") == "success":
                    result["jobs"] = {
                        "total": jobs_result.get("total", 0),
                        "matching": jobs_result.get("count", 0),
                        "sample": jobs_result.get("jobs", [])[:jobs_limit],
                    }
                else:
                    errors.append(f"Jobs: {jobs_result.get('message', 'unknown error')}")
            else:
                # Fallback: keyword search if company not found
                from naukri_server.tools.search import naukri_search_jobs
                jobs_result = await naukri_search_jobs(keywords=keyword, limit=jobs_limit)
                if jobs_result.get("status") == "success":
                    jobs = jobs_result.get("jobs", [])
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

    # Step 2: Get AmbitionBox salary + reviews + interviews (if requested)
    if include_reviews or include_interviews:
        gather_coros = []
        gather_labels = []
        if include_reviews:
            gather_coros.append(_fetch_salary(company_slug=slug))
            gather_labels.append("salary")
            gather_coros.append(_fetch_reviews(company_slug=slug))
            gather_labels.append("reviews")
        if include_interviews:
            gather_coros.append(_fetch_interviews(company_slug=slug))
            gather_labels.append("interviews")

        gather_results = await asyncio.gather(*gather_coros, return_exceptions=True)
        results_map = dict(zip(gather_labels, gather_results))

        salary_result = results_map.get("salary")
        reviews_result = results_map.get("reviews")
        interviews_result = results_map.get("interviews")

        if salary_result is not None:
            if isinstance(salary_result, Exception):
                errors.append(f"Salary: {type(salary_result).__name__}: {salary_result}")
                logger.warning("Research company salary failed: %s", salary_result)
            elif salary_result.get("status") == "success":
                result["salary"] = {
                    k: v for k, v in salary_result.items()
                    if k not in ("status", "url", "_debug_page_props_keys")
                }
            else:
                errors.append(f"Salary: {salary_result.get('message', 'unknown error')}")

        if reviews_result is not None:
            if isinstance(reviews_result, Exception):
                errors.append(f"Reviews: {type(reviews_result).__name__}: {reviews_result}")
                logger.warning("Research company reviews failed: %s", reviews_result)
            elif reviews_result.get("status") == "success":
                result["reviews"] = {
                    k: v for k, v in reviews_result.items()
                    if k not in ("status", "url")
                }
            else:
                errors.append(f"Reviews: {reviews_result.get('message', 'unknown error')}")

        if interviews_result is not None:
            if isinstance(interviews_result, Exception):
                errors.append(f"Interviews: {type(interviews_result).__name__}: {interviews_result}")
                logger.warning("Research company interviews failed: %s", interviews_result)
            elif interviews_result.get("status") == "success":
                result["interviews"] = {
                    k: v for k, v in interviews_result.items()
                    if k not in ("status",)
                }
            else:
                errors.append(f"Interviews: {interviews_result.get('message', 'unknown error')}")

    if errors:
        result["errors"] = errors

    return result
