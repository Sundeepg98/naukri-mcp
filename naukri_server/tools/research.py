"""Unified company research — combines job search, salary, and reviews in one call."""
import asyncio
from typing import Optional

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
    timeout_seconds: int = 120,
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
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        - {status: "success", company_name, slug,
           jobs: {total, sample: [...]},
           salary: {avg_salary, min_salary, max_salary, ...},
           reviews: {overall_rating, category_ratings, reviews: [...]},
           errors: [any partial failures]}
        - {status: "error", message}
    """
    async def _do_work() -> dict:
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

    try:
        return await asyncio.wait_for(_do_work(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return {"status": "partial_success", "message": f"Timed out after {timeout_seconds}s", "error_code": "TIMEOUT"}


@mcp.tool()
async def naukri_salary_benchmark(
    keywords: str,
    location: Optional[str] = None,
    sample_size: int = 15,
    freshness: Optional[int] = 7,
    timeout_seconds: int = 120,
) -> dict:
    """Benchmark your salary against the market for a given role.

    Searches for jobs in your niche, aggregates salary data, and compares
    against your current/expected CTC from your profile.

    Args:
        keywords: Job title or skills to benchmark (e.g., "python developer", "data engineer")
        location: City to filter (e.g., "Bangalore", "Mumbai"). None = all India.
        sample_size: Number of jobs to sample (default 15, max 50)
        freshness: Posted within N days (default 7). None = no filter.
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        - {status: "success", jobs_sampled, jobs_with_salary, salary_aggregate: {min, max, avg, median, p25, p75},
           your_positioning: {current_vs_market, expected_vs_market}, salary_by_company: [...]}
        - {status: "error", message, error_code}
    """
    import re
    import statistics

    sample_size = min(sample_size, 50)
    if sample_size < 1:
        return {"status": "error", "message": "sample_size must be >= 1", "error_code": "VALIDATION_ERROR"}

    async def _do_work():
        from naukri_server.tools.search import naukri_search_jobs
        from naukri_server.tools.profile import get_cached_profile
        from naukri_server.config import LAKHS_MULTIPLIER

        errors = []

        # Parallel: search jobs + get profile
        search_result, profile_result = await asyncio.gather(
            naukri_search_jobs(keywords=keywords, location=location, limit=sample_size, freshness=freshness),
            get_cached_profile(),
            return_exceptions=True,
        )

        if isinstance(search_result, Exception) or (isinstance(search_result, dict) and search_result.get("status") == "error"):
            msg = str(search_result) if isinstance(search_result, Exception) else search_result.get("message")
            return {"status": "error", "message": f"Search failed: {msg}", "error_code": "API_ERROR"}

        jobs = search_result.get("jobs", [])
        if not jobs:
            return {"status": "error", "message": f"No jobs found for '{keywords}'", "error_code": "NOT_FOUND"}

        # Extract profile CTC
        current_ctc = None
        expected_ctc = None
        if isinstance(profile_result, dict) and profile_result.get("status") == "success":
            current_ctc = profile_result.get("current_ctc")
            expected_ctc = profile_result.get("expected_ctc")
        else:
            errors.append("Profile fetch failed — CTC comparison unavailable")

        # Parse salaries from jobs
        salary_values = []
        salary_by_company = []
        for job in jobs:
            salary_str = job.get("salary", "")
            if not salary_str or salary_str.lower() == "not disclosed":
                continue

            nums = re.findall(r'(\d+(?:\.\d+)?)', str(salary_str).replace(",", ""))
            if len(nums) >= 2:
                low, high = float(nums[0]), float(nums[1])
                if low > 200:
                    low /= LAKHS_MULTIPLIER
                    high /= LAKHS_MULTIPLIER
                avg_val = (low + high) / 2
                salary_values.append(avg_val)
                salary_by_company.append({
                    "company": job.get("company"),
                    "title": job.get("title"),
                    "salary_range": salary_str,
                    "avg_lpa": round(avg_val, 1),
                })
            elif len(nums) == 1:
                val = float(nums[0])
                if val > 200:
                    val /= LAKHS_MULTIPLIER
                salary_values.append(val)
                salary_by_company.append({
                    "company": job.get("company"),
                    "title": job.get("title"),
                    "salary_range": salary_str,
                    "avg_lpa": round(val, 1),
                })

        if not salary_values:
            return {
                "status": "partial_success",
                "message": "Jobs found but no salary data available",
                "jobs_sampled": len(jobs),
                "jobs_with_salary": 0,
                "errors": errors,
            }

        salary_values.sort()

        aggregate = {
            "min": round(min(salary_values), 1),
            "max": round(max(salary_values), 1),
            "avg": round(statistics.mean(salary_values), 1),
            "median": round(statistics.median(salary_values), 1),
        }
        if len(salary_values) >= 4:
            q1_idx = len(salary_values) // 4
            q3_idx = 3 * len(salary_values) // 4
            aggregate["p25"] = round(salary_values[q1_idx], 1)
            aggregate["p75"] = round(salary_values[q3_idx], 1)

        positioning = {}
        avg_market = aggregate["avg"]
        if current_ctc is not None:
            ctc = float(current_ctc) if not isinstance(current_ctc, (int, float)) else current_ctc
            if ctc > 200:
                ctc /= LAKHS_MULTIPLIER
            if ctc < avg_market * 0.85:
                positioning["current_vs_market"] = "below"
            elif ctc > avg_market * 1.15:
                positioning["current_vs_market"] = "above"
            else:
                positioning["current_vs_market"] = "at_market"
            below = sum(1 for s in salary_values if s < ctc)
            positioning["current_percentile"] = round(below / len(salary_values) * 100)

        if expected_ctc is not None:
            ectc = float(expected_ctc) if not isinstance(expected_ctc, (int, float)) else expected_ctc
            if ectc > 200:
                ectc /= LAKHS_MULTIPLIER
            if ectc < avg_market * 0.85:
                positioning["expected_vs_market"] = "below"
            elif ectc > avg_market * 1.15:
                positioning["expected_vs_market"] = "above"
            else:
                positioning["expected_vs_market"] = "at_market"
            below = sum(1 for s in salary_values if s < ectc)
            positioning["expected_percentile"] = round(below / len(salary_values) * 100)

        salary_by_company.sort(key=lambda x: x["avg_lpa"], reverse=True)

        result = {
            "status": "success",
            "keywords": keywords,
            "location": location,
            "jobs_sampled": len(jobs),
            "jobs_with_salary": len(salary_values),
            "salary_aggregate": aggregate,
            "your_positioning": positioning if positioning else None,
            "salary_by_company": salary_by_company[:10],
        }
        if errors:
            result["status"] = "partial_success"
            result["errors"] = errors
        return result

    try:
        return await asyncio.wait_for(_do_work(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return {"status": "error", "message": f"Salary benchmark timed out after {timeout_seconds}s", "error_code": "API_ERROR"}
