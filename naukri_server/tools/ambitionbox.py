"""AmbitionBox salary and review tools — scrapes __NEXT_DATA__ from SSR pages."""

import asyncio
import logging

from naukri_server import mcp
from naukri_server.browser import browser, page_goto
from naukri_server.validation import validate_salary_data, validate_review_data

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

            # Extract company info — check multiple possible keys
            company_data = (
                page_props.get("companyData", {})
                or page_props.get("companyMetaData", {})
                or page_props.get("companyInfo", {})
                or page_props.get("company", {})
                or {}
            )
            company_name = (
                company_data.get("CompanyName")
                or company_data.get("companyName")
                or company_data.get("name")
                or page_props.get("companyName")
                or company_slug
            )

            # New structure (2026): salaryData.data.summaryData + latestSalaries.latestSalaries
            # Old structure: flat keys like salaryOverview, salaries, experienceWiseSalary
            salary_container = page_props.get("salaryData") or {}
            salary_data = salary_container.get("data", {}) if isinstance(salary_container, dict) else {}
            summary = salary_data.get("summaryData", {}) or {}
            insights = salary_data.get("profileInsights", {}) or {}

            # Extract salary overview — try new nested structure first, then old flat keys
            avg_salary = (
                summary.get("totalSalaryAverage")
                or insights.get("averageCtc")
                or page_props.get("salaryOverview", {}).get("avgSalary")
                or page_props.get("salaryOverview", {}).get("averageSalary")
            )
            # Convert string values to float if needed
            if isinstance(avg_salary, str):
                try:
                    avg_salary = round(float(avg_salary))
                except (ValueError, TypeError):
                    pass
            min_salary = (
                summary.get("minCtc")
                or insights.get("minCtc")
                or page_props.get("salaryOverview", {}).get("minSalary")
            )
            if isinstance(min_salary, str):
                try:
                    min_salary = round(float(min_salary))
                except (ValueError, TypeError):
                    pass
            max_salary = (
                summary.get("maxCtc")
                or insights.get("maxCtc")
                or page_props.get("salaryOverview", {}).get("maxSalary")
            )
            if isinstance(max_salary, str):
                try:
                    max_salary = round(float(max_salary))
                except (ValueError, TypeError):
                    pass
            total_salaries = (
                summary.get("totalSalaryDataPoints")
                or page_props.get("salaryOverview", {}).get("totalSalaries")
            )
            if isinstance(total_salaries, str):
                try:
                    total_salaries = int(total_salaries)
                except (ValueError, TypeError):
                    pass

            # Extract percentiles if available (new structure)
            percentiles = summary.get("percentiles")

            # Extract individual salary entries — latestSalaries is {latestSalaries: [...]}
            latest_container = page_props.get("latestSalaries") or {}
            raw_salaries = (
                (latest_container.get("latestSalaries") if isinstance(latest_container, dict) else None)
                or (latest_container if isinstance(latest_container, list) else None)
                or page_props.get("salaries")
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
                            entry.get("jobProfileName")
                            or entry.get("Designation")
                            or entry.get("designation")
                            or entry.get("title")
                        ),
                        "ctc": entry.get("ctc"),
                        "avg_salary": entry.get("avgSalary") or entry.get("averageSalary") or entry.get("ctc"),
                        "experience": entry.get("experience") or entry.get("avgExperience"),
                        "experience_unit": entry.get("experienceUnit"),
                        "reported": entry.get("timeElapse") or entry.get("timeStamp"),
                    })

            # Extract experience-wise breakdown — new: salaryData.data.experienceLevels
            exp_breakdown_raw = (
                salary_data.get("experienceLevels")
                or salary_data.get("bucketedExperienceLevels")
                or page_props.get("experienceWiseSalary")
                or page_props.get("experienceBreakdown")
                or []
            )
            experience_breakdown = []
            if isinstance(exp_breakdown_raw, list):
                for entry in exp_breakdown_raw:
                    if not isinstance(entry, dict):
                        continue
                    # Build experience range from minExp/maxExp if no label
                    exp_range = (
                        entry.get("experienceRange")
                        or entry.get("bucketLabel")
                        or entry.get("label")
                    )
                    if not exp_range and entry.get("minExp") is not None and entry.get("maxExp") is not None:
                        exp_range = f"{entry['minExp']}-{entry['maxExp']} years"
                    experience_breakdown.append({
                        "experience_range": exp_range,
                        "avg_salary": entry.get("avgSalary") or entry.get("averageSalary") or entry.get("avgCtc"),
                        "min_salary": entry.get("minSalary") or entry.get("lowSalary") or entry.get("minCtc"),
                        "max_salary": entry.get("maxSalary") or entry.get("highSalary") or entry.get("maxCtc"),
                        "count": entry.get("count") or entry.get("salaryCount") or entry.get("dataPoints"),
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

            if percentiles:
                result["percentiles"] = percentiles

            if summary.get("avgExp") is not None:
                result["avg_experience"] = summary["avgExp"]
                result["experience_range"] = f"{summary.get('minExp', '?')}-{summary.get('maxExp', '?')} years"

            if experience_breakdown:
                result["experience_breakdown"] = experience_breakdown

            # Include pageProps keys for debugging if salary data seems sparse
            if not salaries and not avg_salary:
                result["_debug_page_props_keys"] = list(page_props.keys())

            warnings = validate_salary_data(result)
            if warnings:
                result["warnings"] = warnings
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

            warnings = validate_review_data(result)
            if warnings:
                result["warnings"] = warnings
            return result

        except Exception as e:
            logger.error("Failed to get AmbitionBox reviews: %s: %s", type(e).__name__, e)
            return {
                "status": "error",
                "message": f"Failed to get reviews: {type(e).__name__}: {e}",
            }
