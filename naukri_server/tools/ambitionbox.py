"""AmbitionBox salary and review tools — scrapes __NEXT_DATA__ from SSR pages."""

import asyncio
import logging

from naukri_server import mcp
from naukri_server.browser import browser, page_goto

logger = logging.getLogger(__name__)

AMBITIONBOX_BASE = "https://www.ambitionbox.com"


async def _extract_next_data(page) -> dict | None:
    """Extract __NEXT_DATA__ JSON from the given page."""
    await asyncio.sleep(2)
    return await page.evaluate("""
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


async def _scrape_salary_table(page) -> list:
    """Fallback: scrape salary data from rendered DOM body text via regex matching."""
    raw = await page.evaluate("""
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
                        if ((l.startsWith('\u20B9') || l.includes('Lakhs') || l.includes('L/yr')) && !avgSalary) {
                            avgSalary = l;
                        } else if ((l.startsWith('\u20B9') || l.includes('L/yr')) && avgSalary && l.includes('-')) {
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
    # JS returns list of dicts with designation, experience_info, avg_salary, salary_range
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict) and entry.get("designation")]


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
    async with browser.page_pool.acquire() as page:
        try:
            # Build URL
            if designation:
                url = f"{AMBITIONBOX_BASE}/salaries/{company_slug}-salaries/{designation}"
            else:
                url = f"{AMBITIONBOX_BASE}/salaries/{company_slug}-salaries"

            logger.info("Navigating to AmbitionBox salary page: %s", url)
            await page_goto(page, url, wait="networkidle")

            # Check for 404 / invalid page
            if "404" in (await page.title() or ""):
                return {"status": "error", "message": f"Page not found for company slug '{company_slug}'."}

            # Extract __NEXT_DATA__
            next_data = await _extract_next_data(page)
            if not next_data:
                # Fallback: scrape salary data from the rendered DOM
                dom_salaries = await _scrape_salary_table(page)
                if dom_salaries:
                    title = await page.title() or ""
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
    async with browser.page_pool.acquire() as tab:
        try:
            url = f"{AMBITIONBOX_BASE}/reviews/{company_slug}-reviews?page={page}"

            logger.info("Navigating to AmbitionBox reviews page: %s", url)
            await page_goto(tab, url, wait="networkidle")

            # Check for 404 / invalid page
            if "404" in (await tab.title() or ""):
                return {"status": "error", "message": f"Page not found for company slug '{company_slug}'."}

            # Extract __NEXT_DATA__
            next_data = await _extract_next_data(tab)
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

            # Overall rating — ratingsData.overallCompanyRating (float like 3.8)
            rating_data = page_props.get("ratingsData", {}) or {}
            overall_rating = rating_data.get("overallCompanyRating")
            review_count = (
                page_props.get("fixedReviewCount")
                or page_props.get("reviewCount")
            )

            # Rating distribution — ratingDistribution is dict {"5": 281, "4": 130, ...}
            rating_dist_raw = page_props.get("ratingDistribution", {})
            rating_distribution = rating_dist_raw if isinstance(rating_dist_raw, dict) and rating_dist_raw else None

            # Category ratings — ratingsData has float ratings, ratingCounts has counts
            # Use ratingsData for actual ratings (exclude overallCompanyRating)
            category_ratings = {}
            _rating_key_labels = {
                "workSatisfactionRating": "Work Satisfaction",
                "careerGrowthRating": "Career Growth",
                "skillDevelopmentRating": "Skill Development",
                "workLifeRating": "Work-Life Balance",
                "compensationBenefitsRating": "Compensation & Benefits",
                "jobSecurityRating": "Job Security",
                "companyCultureRating": "Company Culture",
            }
            for key, label in _rating_key_labels.items():
                val = rating_data.get(key)
                if val is not None:
                    category_ratings[label] = val

            # Individual reviews — reviewsData is list of review dicts
            raw_reviews = page_props.get("reviewsData") or page_props.get("detailedReviews") or []
            reviews = []
            if isinstance(raw_reviews, list):
                for rev in raw_reviews:
                    if not isinstance(rev, dict):
                        continue
                    # Extract designation from nested jobProfile object
                    job_profile = rev.get("jobProfile") or {}
                    designation = job_profile.get("name") if isinstance(job_profile, dict) else None
                    # Extract location from nested jobLocation object
                    job_location = rev.get("jobLocation") or {}
                    location = job_location.get("name") if isinstance(job_location, dict) else None

                    reviews.append({
                        "title": rev.get("reviewTitle"),
                        "rating": rev.get("overallCompanyRating"),
                        "designation": designation,
                        "location": location,
                        "department": rev.get("division"),
                        "employment_type": rev.get("employmentType"),
                        "work_policy": rev.get("workPolicy"),
                        "date": rev.get("created"),
                        "likes": rev.get("likesText"),
                        "dislikes": rev.get("disLikesText"),
                        "is_current_employee": rev.get("currentJob"),
                        "verified": rev.get("verified"),
                        "helpful_count": rev.get("helpfulCount"),
                    })

            # Pagination — detailedReviewPagination or pagination
            pagination = page_props.get("detailedReviewPagination") or page_props.get("pagination") or {}
            total_pages = pagination.get("totalPages") or pagination.get("lastPage")

            # Review summary (AI-generated)
            review_summary = page_props.get("reviewSummary")

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

            if review_summary:
                result["review_summary"] = review_summary

            return result

        except Exception as e:
            logger.error("Failed to get AmbitionBox reviews: %s: %s", type(e).__name__, e)
            return {
                "status": "error",
                "message": f"Failed to get reviews: {type(e).__name__}: {e}",
            }
