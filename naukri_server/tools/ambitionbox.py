"""AmbitionBox salary and review tools — scrapes __NEXT_DATA__ from SSR pages."""

import asyncio
import logging

from naukri_server import mcp
from naukri_server.browser import browser

logger = logging.getLogger(__name__)

AMBITIONBOX_BASE = "https://www.ambitionbox.com"


async def _extract_next_data() -> dict | None:
    """Extract __NEXT_DATA__ JSON from the current page."""
    await asyncio.sleep(2)
    return await browser.page.evaluate("""
        () => {
            const el = document.getElementById('__NEXT_DATA__');
            if (el) return JSON.parse(el.textContent);
            // Fallback: check all script tags with type application/json
            for (const s of document.querySelectorAll('script[type="application/json"]')) {
                try {
                    const d = JSON.parse(s.textContent);
                    if (d && d.props && d.props.pageProps) return d;
                } catch(e) {}
            }
            return null;
        }
    """)


async def _scrape_salary_table() -> list:
    """Fallback: scrape salary data from rendered DOM body text via regex matching."""
    raw = await browser.page.evaluate("""
        () => {
            const body = document.body.innerText;
            const lines = body.split('\\n').filter(l => l.trim());
            const entries = [];
            for (let i = 0; i < lines.length - 2; i++) {
                const line = lines[i].trim();
                const next = lines[i+1] ? lines[i+1].trim() : '';
                if (next.includes('exp.') && next.includes('salaries')) {
                    let avgSalary = '';
                    let salaryRange = '';
                    for (let j = i + 2; j < Math.min(i + 6, lines.length); j++) {
                        const l = lines[j].trim();
                        if (l.startsWith('\\u20B9') && !avgSalary) {
                            avgSalary = l;
                        } else if (l.startsWith('\\u20B9') && avgSalary && l.includes('-')) {
                            salaryRange = l;
                            break;
                        }
                    }
                    if (avgSalary) {
                        entries.push({
                            designation: line,
                            experience_info: next,
                            avg_salary: avgSalary,
                            salary_range: salaryRange
                        });
                    }
                }
            }
            return entries;
        }
    """)
    salaries = []
    if not isinstance(raw, list):
        return salaries
    for text in raw:
        if not isinstance(text, str):
            continue
        # Parse entries like "Software Engineer\n\n2 - 5 years exp. (1.1k salaries)\n₹14.8 Lakhs\n\n₹14.1 L/yr - ₹15.5 L/yr"
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        # Skip non-salary cards (ads, promos)
        if any(skip in text for skip in ["Claim Now", "fresher salaries", "Share your salary", "Salary Hike", "also viewed"]):
            continue
        designation = lines[0] if lines else ""
        experience = ""
        avg_salary = ""
        salary_range = ""
        salary_count = ""
        for line in lines[1:]:
            if "exp." in line or "year" in line.lower():
                experience = line.split("(")[0].strip()
                if "(" in line and "salaries" in line:
                    salary_count = line.split("(")[1].split("salaries")[0].strip()
            elif line.startswith("₹") and "L/yr" in line and "-" in line:
                salary_range = line
            elif line.startswith("₹") and ("Lakhs" in line or "L" in line):
                avg_salary = line
        if designation and (avg_salary or salary_range):
            salaries.append({
                "designation": designation,
                "avg_salary": avg_salary,
                "salary_range": salary_range,
                "experience": experience,
                "salary_count": salary_count,
            })
    return salaries


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
            await browser.goto(url, wait="networkidle")

            # Check for 404 / invalid page
            if "404" in (await browser.page.title() or ""):
                return {"status": "error", "message": f"Page not found for company slug '{company_slug}'."}

            # Extract __NEXT_DATA__
            next_data = await _extract_next_data()
            if not next_data:
                # Fallback: scrape salary data from the rendered DOM
                dom_salaries = await _scrape_salary_table()
                if dom_salaries:
                    title = await browser.page.title() or ""
                    return {
                        "status": "success",
                        "company": company_slug.replace("-", " ").title(),
                        "url": url,
                        "source": "dom_scrape",
                        "salaries": dom_salaries,
                        "page_title": title,
                    }
                return {
                    "status": "error",
                    "message": "Could not extract salary data from the page.",
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
            await browser.goto(url, wait="networkidle")

            # Check for 404 / invalid page
            if "404" in (await browser.page.title() or ""):
                return {"status": "error", "message": f"Page not found for company slug '{company_slug}'."}

            # Extract __NEXT_DATA__
            next_data = await _extract_next_data()
            if not next_data:
                return {
                    "status": "error",
                    "message": "Could not extract review data from the page.",
                }

            page_props = next_data.get("props", {}).get("pageProps", {})
            if not page_props:
                return {
                    "status": "error",
                    "message": "pageProps is empty — the company slug may be invalid or the page didn't load correctly.",
                }

            # Extract company info — actual keys: companyName, companyHeaderData
            company_name = (
                page_props.get("companyName")
                or (page_props.get("companyHeaderData") or {}).get("CompanyName")
                or company_slug
            )

            # Extract overall rating — actual key: ratingsData
            rating_data = page_props.get("ratingsData", {}) or {}
            overall_rating = (
                rating_data.get("overallRating")
                or rating_data.get("rating")
            )
            review_count = (
                page_props.get("reviewCount")
                or page_props.get("fixedReviewCount")
                or rating_data.get("totalReviewCount")
            )

            # Rating distribution — actual key: ratingDistribution (dict of star:count)
            rating_dist_raw = page_props.get("ratingDistribution", {})
            rating_distribution = None
            if isinstance(rating_dist_raw, dict) and rating_dist_raw:
                rating_distribution = rating_dist_raw
            elif isinstance(rating_dist_raw, list):
                rating_distribution = {
                    str(entry.get("rating", entry.get("star", i + 1))): entry.get("count", entry.get("value"))
                    for i, entry in enumerate(rating_dist_raw)
                    if isinstance(entry, dict)
                }

            # Category ratings — from ratingsData or ratingCounts
            category_ratings_raw = page_props.get("ratingCounts", rating_data.get("categoryRatings", []))
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

            # Individual reviews — actual keys: reviewsData, detailedReviews
            raw_reviews = (
                page_props.get("reviewsData")
                or page_props.get("detailedReviews")
                or page_props.get("reviews")
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
