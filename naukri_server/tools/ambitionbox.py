"""AmbitionBox salary and review tools — scrapes __NEXT_DATA__ from SSR pages."""

import asyncio
import logging

from naukri_server import mcp
from naukri_server.browser import browser

logger = logging.getLogger(__name__)

AMBITIONBOX_BASE = "https://www.ambitionbox.com"


async def _extract_next_data(timeout: int = 15000) -> dict | None:
    """Extract __NEXT_DATA__ JSON from the current page."""
    # Wait a moment for SSR content to be in the DOM
    await asyncio.sleep(2)
    return await browser.page.evaluate("""
        () => {
            const el = document.getElementById('__NEXT_DATA__');
            return el ? JSON.parse(el.textContent) : null;
        }
    """)


# ============================================================================
# Tool: Get Company Salary Data from AmbitionBox
# ============================================================================


@mcp.tool()
async def naukri_get_company_salary(company_slug: str, designation: str = "") -> dict:
    """Get salary data for a company from AmbitionBox — avg salary, ranges, and experience breakdowns.

    Args:
        company_slug: Company URL slug (e.g., "wissen-technology", "google", "tcs")
        designation: Optional job title slug (e.g., "software-engineer", "data-scientist")
                     If provided, returns salary for that specific role.

    Returns:
        - {status: "success", company, salaries: [...], ...}
        - {status: "error", message}
    """
    async with browser._lock:
        try:
            # Build URL
            if designation:
                url = f"{AMBITIONBOX_BASE}/salaries/{company_slug}-salaries/{designation}"
            else:
                url = f"{AMBITIONBOX_BASE}/salaries/{company_slug}-salaries"

            logger.info("Navigating to AmbitionBox salary page: %s", url)
            await browser.page.goto(url, wait_until="networkidle", timeout=30000)

            # Check for 404 / invalid page
            if "404" in (await browser.page.title() or ""):
                return {"status": "error", "message": f"Page not found for company slug '{company_slug}'."}

            # Extract __NEXT_DATA__
            next_data = await _extract_next_data()
            if not next_data:
                return {
                    "status": "error",
                    "message": "Could not find __NEXT_DATA__ on the page. "
                               "The page structure may have changed or the company slug may be invalid.",
                }

            page_props = next_data.get("props", {}).get("pageProps", {})
            if not page_props:
                return {
                    "status": "error",
                    "message": "pageProps is empty — the company slug may be invalid or the page didn't load correctly.",
                }

            # Extract company info
            company_data = page_props.get("companyData", {}) or page_props.get("company", {}) or {}
            company_name = (
                company_data.get("CompanyName")
                or company_data.get("companyName")
                or company_data.get("name")
                or company_slug
            )

            # Extract salary overview / aggregate data
            salary_overview = page_props.get("salaryOverview", {}) or page_props.get("overview", {}) or {}
            avg_salary = (
                salary_overview.get("avgSalary")
                or salary_overview.get("averageSalary")
                or salary_overview.get("medianSalary")
            )
            min_salary = salary_overview.get("minSalary") or salary_overview.get("lowSalary")
            max_salary = salary_overview.get("maxSalary") or salary_overview.get("highSalary")
            total_salaries = salary_overview.get("totalSalaries") or salary_overview.get("salaryCount")

            # Extract individual salary entries (list of designations with salary ranges)
            raw_salaries = (
                page_props.get("salaries")
                or page_props.get("salaryList")
                or page_props.get("designationSalaries")
                or []
            )
            salaries = []
            if isinstance(raw_salaries, list):
                for entry in raw_salaries:
                    if not isinstance(entry, dict):
                        continue
                    salaries.append({
                        "designation": (
                            entry.get("Designation")
                            or entry.get("designation")
                            or entry.get("title")
                            or entry.get("name")
                        ),
                        "avg_salary": entry.get("avgSalary") or entry.get("averageSalary"),
                        "min_salary": entry.get("minSalary") or entry.get("lowSalary"),
                        "max_salary": entry.get("maxSalary") or entry.get("highSalary"),
                        "salary_count": entry.get("salaryCount") or entry.get("count"),
                        "experience": entry.get("experience") or entry.get("avgExperience"),
                    })

            # Extract experience-wise breakdown if available
            exp_breakdown_raw = (
                page_props.get("experienceWiseSalary")
                or page_props.get("experienceBreakdown")
                or page_props.get("salaryByExperience")
                or []
            )
            experience_breakdown = []
            if isinstance(exp_breakdown_raw, list):
                for entry in exp_breakdown_raw:
                    if not isinstance(entry, dict):
                        continue
                    experience_breakdown.append({
                        "experience_range": (
                            entry.get("experienceRange")
                            or entry.get("experience")
                            or entry.get("label")
                        ),
                        "avg_salary": entry.get("avgSalary") or entry.get("averageSalary"),
                        "min_salary": entry.get("minSalary") or entry.get("lowSalary"),
                        "max_salary": entry.get("maxSalary") or entry.get("highSalary"),
                        "count": entry.get("count") or entry.get("salaryCount"),
                    })

            result = {
                "status": "success",
                "company": company_name,
                "url": url,
                "avg_salary": avg_salary,
                "min_salary": min_salary,
                "max_salary": max_salary,
                "total_salaries_reported": total_salaries,
                "salaries": salaries,
            }

            if designation:
                result["designation"] = designation

            if experience_breakdown:
                result["experience_breakdown"] = experience_breakdown

            # Include raw pageProps keys for debugging if salary data seems sparse
            if not salaries and not avg_salary:
                result["_debug_page_props_keys"] = list(page_props.keys())

            return result

        except Exception as e:
            logger.error("Failed to get AmbitionBox salary data: %s: %s", type(e).__name__, e)
            return {
                "status": "error",
                "message": f"Failed to get salary data: {type(e).__name__}: {e}",
            }


# ============================================================================
# Tool: Get Company Reviews from AmbitionBox
# ============================================================================


@mcp.tool()
async def naukri_get_company_reviews(company_slug: str, page: int = 1) -> dict:
    """Get employee reviews for a company from AmbitionBox — ratings, pros, cons.

    Args:
        company_slug: Company URL slug (e.g., "wissen-technology", "google", "tcs")
        page: Page number for review pagination (default: 1)

    Returns:
        - {status: "success", company, overall_rating, review_count, reviews: [...], ...}
        - {status: "error", message}
    """
    async with browser._lock:
        try:
            url = f"{AMBITIONBOX_BASE}/reviews/{company_slug}-reviews?page={page}"

            logger.info("Navigating to AmbitionBox reviews page: %s", url)
            await browser.page.goto(url, wait_until="networkidle", timeout=30000)

            # Check for 404 / invalid page
            if "404" in (await browser.page.title() or ""):
                return {"status": "error", "message": f"Page not found for company slug '{company_slug}'."}

            # Extract __NEXT_DATA__
            next_data = await _extract_next_data()
            if not next_data:
                return {
                    "status": "error",
                    "message": "Could not find __NEXT_DATA__ on the page. "
                               "The page structure may have changed or the company slug may be invalid.",
                }

            page_props = next_data.get("props", {}).get("pageProps", {})
            if not page_props:
                return {
                    "status": "error",
                    "message": "pageProps is empty — the company slug may be invalid or the page didn't load correctly.",
                }

            # Extract company info
            company_data = page_props.get("companyData", {}) or page_props.get("company", {}) or {}
            company_name = (
                company_data.get("CompanyName")
                or company_data.get("companyName")
                or company_data.get("name")
                or company_slug
            )

            # Extract overall rating info
            rating_data = (
                page_props.get("companyRating", {})
                or page_props.get("ratingData", {})
                or page_props.get("overallRating", {})
                or {}
            )
            overall_rating = (
                rating_data.get("overallRating")
                or rating_data.get("rating")
                or rating_data.get("avgRating")
                or company_data.get("overallRating")
                or company_data.get("rating")
            )
            review_count = (
                rating_data.get("totalReviewCount")
                or rating_data.get("reviewCount")
                or rating_data.get("count")
                or company_data.get("reviewCount")
            )

            # Extract rating distribution (1-star through 5-star counts)
            rating_dist_raw = (
                rating_data.get("ratingDistribution")
                or page_props.get("ratingDistribution")
                or {}
            )
            rating_distribution = None
            if isinstance(rating_dist_raw, dict) and rating_dist_raw:
                rating_distribution = rating_dist_raw
            elif isinstance(rating_dist_raw, list):
                rating_distribution = {
                    str(entry.get("rating", entry.get("star", i + 1))): entry.get("count", entry.get("value"))
                    for i, entry in enumerate(rating_dist_raw)
                    if isinstance(entry, dict)
                }

            # Extract category-wise ratings (work-life balance, salary, etc.)
            category_ratings_raw = (
                rating_data.get("categoryRatings")
                or page_props.get("categoryRatings")
                or rating_data.get("subRatings")
                or []
            )
            category_ratings = {}
            if isinstance(category_ratings_raw, list):
                for cat in category_ratings_raw:
                    if not isinstance(cat, dict):
                        continue
                    cat_name = cat.get("name") or cat.get("category") or cat.get("label")
                    cat_rating = cat.get("rating") or cat.get("value") or cat.get("avgRating")
                    if cat_name:
                        category_ratings[cat_name] = cat_rating
            elif isinstance(category_ratings_raw, dict):
                category_ratings = category_ratings_raw

            # Extract individual reviews
            raw_reviews = (
                page_props.get("reviews")
                or page_props.get("reviewList")
                or page_props.get("companyReviews")
                or []
            )
            reviews = []
            if isinstance(raw_reviews, list):
                for rev in raw_reviews:
                    if not isinstance(rev, dict):
                        continue
                    reviews.append({
                        "title": rev.get("title") or rev.get("reviewTitle") or rev.get("heading"),
                        "rating": rev.get("overallRating") or rev.get("rating"),
                        "designation": (
                            rev.get("designation")
                            or rev.get("jobTitle")
                            or rev.get("jobRole")
                        ),
                        "department": rev.get("department") or rev.get("dept"),
                        "employment_type": (
                            rev.get("employmentType")
                            or rev.get("empType")
                            or rev.get("type")
                        ),
                        "experience": rev.get("experience") or rev.get("tenure"),
                        "date": (
                            rev.get("reviewDate")
                            or rev.get("date")
                            or rev.get("createdAt")
                            or rev.get("postedOn")
                        ),
                        "likes": rev.get("likes") or rev.get("pros") or rev.get("prosText"),
                        "dislikes": rev.get("dislikes") or rev.get("cons") or rev.get("consText"),
                        "is_current_employee": rev.get("isCurrentEmployee") or rev.get("isCurrent"),
                        "helpful_count": rev.get("helpfulCount") or rev.get("upvoteCount"),
                    })

            # Pagination info
            pagination = page_props.get("pagination", {}) or {}
            total_pages = (
                pagination.get("totalPages")
                or pagination.get("lastPage")
                or page_props.get("totalPages")
            )

            result = {
                "status": "success",
                "company": company_name,
                "url": url,
                "overall_rating": overall_rating,
                "review_count": review_count,
                "page": page,
                "total_pages": total_pages,
                "reviews": reviews,
            }

            if category_ratings:
                result["category_ratings"] = category_ratings

            if rating_distribution:
                result["rating_distribution"] = rating_distribution

            # Include raw pageProps keys for debugging if review data seems sparse
            if not reviews and not overall_rating:
                result["_debug_page_props_keys"] = list(page_props.keys())

            return result

        except Exception as e:
            logger.error("Failed to get AmbitionBox reviews: %s: %s", type(e).__name__, e)
            return {
                "status": "error",
                "message": f"Failed to get reviews: {type(e).__name__}: {e}",
            }
