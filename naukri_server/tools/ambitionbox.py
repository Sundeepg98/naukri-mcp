"""AmbitionBox company intel and slug-bridge tools — scrapes __NEXT_DATA__ from SSR pages.

naukri_company_intel automatically enriches browser-scraped results with
supplementary REST data (work_culture, benefits, competitors) when a
numeric company ID can be extracted from the __NEXT_DATA__ pageProps.

REST bridge functions live in ambitionbox_rest.py and are re-exported
here for backward compatibility.
"""

import asyncio
import logging
import re

from naukri_server import mcp
from naukri_server.browser import browser, page_goto, page_evaluate_safe
from naukri_server.config import (
    AMBITIONBOX_BASE, AMBITIONBOX_FALLBACK_SLEEP, AMBITIONBOX_WAIT_TIMEOUT,
)
from naukri_server.domain import safe_get
from naukri_server.utils import derive_slug
from naukri_server.validation import validate_salary_data, validate_review_data, validate_page

# Backward-compat re-exports — REST bridge moved to ambitionbox_rest.py
from naukri_server.tools.ambitionbox_rest import (  # noqa: F401
    ab_get_benefits, ab_get_work_culture, ab_get_interview_questions,
    ab_get_competitors, ab_get_locations, ab_get_applied_jobs_insights,
    ab_get_salary_rest,
)

logger = logging.getLogger(__name__)

# Valid intel_type values for naukri_company_intel
_VALID_INTEL_TYPES = ("salary", "reviews", "interviews")


def _extract_company_id(page_props: dict) -> str | None:
    """Extract the AmbitionBox numeric company ID from __NEXT_DATA__ pageProps.

    The ID lives at pageProps.metaData.companyId (string like "41").
    Also checks companyData.CompanyId and companyHeaderData.CompanyId as fallbacks.
    """
    if not page_props:
        return None
    # Primary: metaData.companyId
    meta = safe_get(page_props, "metaData", field_name="metaData", warn=True, context="company_id") or {}
    cid = safe_get(meta, "companyId", field_name="companyId", warn=True, context="company_id")
    if cid:
        return str(cid)
    # Fallback: companyData / companyHeaderData / companyInfo
    for key in ("companyData", "companyHeaderData", "companyInfo", "company"):
        container = safe_get(page_props, key)
        if isinstance(container, dict):
            val = safe_get(container, "CompanyId", "companyId", "id",
                           field_name="company_id", warn=True, context=f"fallback_{key}")
            if val is not None:
                return str(val)
    return None


def _ensure_slug(value: str) -> str:
    """Accept either a slug (lowercase-dashed) or a company name; return a slug."""
    if re.match(r'^[a-z0-9][a-z0-9-]*$', value):
        return value  # Already a slug
    return derive_slug(value)


async def _extract_next_data(page) -> dict | None:
    """Extract __NEXT_DATA__ JSON from the given page."""
    try:
        await page.wait_for_selector('script#__NEXT_DATA__', timeout=AMBITIONBOX_WAIT_TIMEOUT)
    except Exception as e:
        logger.debug("__NEXT_DATA__ selector not found, using fallback: %s", e)
        await asyncio.sleep(AMBITIONBOX_FALLBACK_SLEEP)  # Fallback if selector not found
    return await page_evaluate_safe(page, """
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


async def _scrape_interview_data(page) -> dict | None:
    """Fallback: scrape interview data from rendered DOM when __NEXT_DATA__ is absent."""
    return await page.evaluate("""
        () => {
            const result = {
                company_name: null,
                total_interviews: null,
                difficulty: null,
                duration: null,
                rating: null,
                questions: [],
                experiences: [],
            };

            // Company name — try page title parsing
            const title = document.title || '';
            const titleMatch = title.match(/^[\\d,+.\\s]*(.*?)\\s*Interview/i);
            if (titleMatch) result.company_name = titleMatch[1].trim();

            // Stats — "Interviews" tab count
            const tabs = document.querySelectorAll('#midNavTabs a, .ab_header_tabs a');
            tabs.forEach(tab => {
                const text = tab.textContent || '';
                if (text.includes('Interview')) {
                    const numMatch = text.match(/([\\d,.]+[kKmM]?)/);
                    if (numMatch) result.total_interviews = numMatch[1];
                }
            });

            // Insight card — extract rating, difficulty, duration
            const insightCards = document.querySelectorAll('[class*="interviewInsight"]');
            insightCards.forEach(card => {
                const text = card.textContent || '';
                const ratingMatch = text.match(/(\\d+\\.\\d+)\\/5/);
                if (ratingMatch) result.rating = ratingMatch[1];

                const diffMatch = text.match(/Easy\\s*(\\d+)%.*?Moderate\\s*(\\d+)%.*?Hard\\s*(\\d+)%/s);
                if (diffMatch) {
                    result.difficulty = {easy: diffMatch[1]+'%', moderate: diffMatch[2]+'%', hard: diffMatch[3]+'%'};
                }

                const durMatch = text.match(/Less than 2 weeks\\s*(\\d+)%.*?2-4 weeks\\s*(\\d+)%/s);
                if (durMatch) {
                    result.duration = {under_2_weeks: durMatch[1]+'%', two_to_four_weeks: durMatch[2]+'%'};
                }
            });

            // Interview experience cards — only top-level cards (have the __cta link)
            const ctaLinks = document.querySelectorAll('a.interview-exp-card__cta');
            const seen = new Set();
            ctaLinks.forEach(link => {
                // Walk up to the top-level card container
                let card = link.closest('[class*="interview-exp-card"]');
                // Go up until we find one whose parent is NOT also an exp-card
                while (card && card.parentElement && Array.from(card.parentElement.classList || []).some(c => c.includes('interview-exp-card'))) {
                    card = card.parentElement;
                }
                if (!card || seen.has(card)) return;
                seen.add(card);

                const text = card.textContent.trim();
                // Parse "A <Role> shared <date> <preview>"
                const roleMatch = text.match(/^(?:An?\\s+)(.+?)\\s+shared/i);
                const dateMatch = text.match(/shared\\s*\\n?\\s*([\\dwmdy]+\\s*ago)/i);
                const previewMatch = text.match(/ago\\s*\\n?\\s*(.+?)(?:\\s*\\.{3}|\\s*Read full)/s);

                result.experiences.push({
                    designation: roleMatch ? roleMatch[1].trim() : null,
                    date: dateMatch ? dateMatch[1].trim() : null,
                    text_preview: previewMatch ? previewMatch[1].trim().substring(0, 300) : text.substring(0, 200),
                });
            });

            // Interview questions — Q&A format cards
            const qHeaders = document.querySelectorAll('[class*="questionCard"], [class*="ques_cnt"]');
            qHeaders.forEach((card, i) => {
                if (i >= 15) return;
                const text = card.textContent.trim();
                if (text.length > 30) {
                    // Parse "A <Role> was asked <date> Q. <question>"
                    const qMatch = text.match(/Q\\.\\s*(.+?)(?:\\nAns|$)/s);
                    const roleMatch = text.match(/^(?:An?\\s+)(.+?)\\s+was asked/i);
                    result.questions.push({
                        designation: roleMatch ? roleMatch[1].trim() : null,
                        question: qMatch ? qMatch[1].trim().substring(0, 300) : text.substring(0, 300),
                    });
                }
            });

            // Fallback: broader question search
            if (result.questions.length === 0) {
                document.querySelectorAll('div').forEach((el, i) => {
                    if (result.questions.length >= 10) return;
                    const text = el.textContent.trim();
                    if (text.includes('was asked') && text.includes('Q.') && text.length < 1000) {
                        const qMatch = text.match(/Q\\.\\s*(.+?)(?:\\nAns|$)/s);
                        const roleMatch = text.match(/^(?:An?\\s+)(.+?)\\s+was asked/i);
                        if (qMatch) {
                            result.questions.push({
                                designation: roleMatch ? roleMatch[1].trim() : null,
                                question: qMatch[1].trim().substring(0, 300),
                            });
                        }
                    }
                });
            }

            return result;
        }
    """)


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — called by the unified tool + research.py)
# ---------------------------------------------------------------------------


async def _fetch_salary(company_slug: str, designation: str = "") -> dict:
    """Fetch salary data for a company from AmbitionBox."""
    async with browser.page_pool.acquire() as page:
        try:
            company_slug = _ensure_slug(company_slug)
            # Build URL
            if designation:
                url = f"{AMBITIONBOX_BASE}/salaries/{company_slug}-salaries/{designation}"
            else:
                url = f"{AMBITIONBOX_BASE}/salaries/{company_slug}-salaries"

            logger.info("Navigating to AmbitionBox salary page: %s", url)
            await page_goto(page, url, wait="networkidle")

            # Check for 404 / invalid page
            if "404" in (await page.title() or ""):
                return {"status": "error", "message": f"Page not found for company slug '{company_slug}'.", "error_code": "NOT_FOUND"}

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
                    "error_code": "BROWSER_ERROR",
                }

            props = safe_get(next_data, "props", field_name="props", warn=True, context=company_slug) or {}
            page_props = safe_get(props, "pageProps", field_name="pageProps", warn=True, context=company_slug) or {}
            if not page_props:
                return {
                    "status": "error",
                    "message": "pageProps is empty — the company slug may be invalid or the page didn't load correctly.",
                    "error_code": "BROWSER_ERROR",
                }

            # Extract company info — check multiple possible keys
            company_data = (
                safe_get(page_props, "companyData", "companyMetaData", "companyInfo", "company",
                         field_name="company_data", warn=True, context=company_slug)
                or {}
            )
            company_name = (
                safe_get(company_data, "CompanyName", "companyName", "name",
                         field_name="company_name", warn=True, context=company_slug)
                or safe_get(page_props, "companyName")
                or company_slug
            )

            # New structure (2026): salaryData.data.summaryData + latestSalaries.latestSalaries
            # Old structure: flat keys like salaryOverview, salaries, experienceWiseSalary
            salary_container = safe_get(page_props, "salaryData",
                                        field_name="salaryData", warn=True, context=company_slug) or {}
            salary_data = safe_get(salary_container, "data") or {} if isinstance(salary_container, dict) else {}
            summary = safe_get(salary_data, "summaryData",
                               field_name="summaryData", warn=True, context=company_slug) or {}
            insights = safe_get(salary_data, "profileInsights") or {}

            # Extract salary overview — try new nested structure first, then old flat keys
            salary_overview = safe_get(page_props, "salaryOverview") or {}
            avg_salary = (
                safe_get(summary, "totalSalaryAverage",
                         field_name="avg_salary", warn=True, context=company_slug)
                or safe_get(insights, "averageCtc")
                or safe_get(salary_overview, "avgSalary", "averageSalary")
            )
            # Convert string values to float if needed
            if isinstance(avg_salary, str):
                try:
                    avg_salary = round(float(avg_salary))
                except (ValueError, TypeError):
                    pass
            min_salary = (
                safe_get(summary, "minCtc",
                         field_name="min_salary", warn=True, context=company_slug)
                or safe_get(insights, "minCtc")
                or safe_get(salary_overview, "minSalary")
            )
            if isinstance(min_salary, str):
                try:
                    min_salary = round(float(min_salary))
                except (ValueError, TypeError):
                    pass
            max_salary = (
                safe_get(summary, "maxCtc",
                         field_name="max_salary", warn=True, context=company_slug)
                or safe_get(insights, "maxCtc")
                or safe_get(salary_overview, "maxSalary")
            )
            if isinstance(max_salary, str):
                try:
                    max_salary = round(float(max_salary))
                except (ValueError, TypeError):
                    pass
            total_salaries = (
                safe_get(summary, "totalSalaryDataPoints",
                         field_name="total_salaries", warn=True, context=company_slug)
                or safe_get(salary_overview, "totalSalaries")
            )
            if isinstance(total_salaries, str):
                try:
                    total_salaries = int(total_salaries)
                except (ValueError, TypeError):
                    pass

            # Extract percentiles if available (new structure)
            percentiles = safe_get(summary, "percentiles")

            # Extract individual salary entries — latestSalaries is {latestSalaries: [...]}
            latest_container = safe_get(page_props, "latestSalaries",
                                        field_name="latestSalaries", warn=True, context=company_slug) or {}
            raw_salaries = (
                (safe_get(latest_container, "latestSalaries") if isinstance(latest_container, dict) else None)
                or (latest_container if isinstance(latest_container, list) else None)
                or safe_get(page_props, "salaries", "salaryList", "designationSalaries")
                or []
            )
            salaries = []
            if isinstance(raw_salaries, list):
                for entry in raw_salaries:
                    if not isinstance(entry, dict):
                        continue
                    salaries.append({
                        "designation": safe_get(
                            entry, "jobProfileName", "Designation", "designation", "title",
                            field_name="salary_designation", warn=True, context=company_slug),
                        "ctc": entry.get("ctc"),
                        "avg_salary": safe_get(entry, "avgSalary", "averageSalary", "ctc"),
                        "experience": safe_get(entry, "experience", "avgExperience"),
                        "experience_unit": entry.get("experienceUnit"),
                        "reported": safe_get(entry, "timeElapse", "timeStamp"),
                    })

            # Extract experience-wise breakdown — new: salaryData.data.experienceLevels
            exp_breakdown_raw = (
                safe_get(salary_data, "experienceLevels", "bucketedExperienceLevels")
                or safe_get(page_props, "experienceWiseSalary", "experienceBreakdown")
                or []
            )
            experience_breakdown = []
            if isinstance(exp_breakdown_raw, list):
                for entry in exp_breakdown_raw:
                    if not isinstance(entry, dict):
                        continue
                    # Build experience range from minExp/maxExp if no label
                    exp_range = safe_get(entry, "experienceRange", "bucketLabel", "label")
                    if not exp_range and entry.get("minExp") is not None and entry.get("maxExp") is not None:
                        exp_range = f"{entry['minExp']}-{entry['maxExp']} years"
                    experience_breakdown.append({
                        "experience_range": exp_range,
                        "avg_salary": safe_get(entry, "avgSalary", "averageSalary", "avgCtc"),
                        "min_salary": safe_get(entry, "minSalary", "lowSalary", "minCtc"),
                        "max_salary": safe_get(entry, "maxSalary", "highSalary", "maxCtc"),
                        "count": safe_get(entry, "count", "salaryCount", "dataPoints"),
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

            # Include AB company ID for REST enrichment
            ab_cid = _extract_company_id(page_props)
            if ab_cid:
                result["_ab_company_id"] = ab_cid

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
                "error_code": "BROWSER_ERROR",
            }


async def _fetch_reviews(company_slug: str, page: int = 1) -> dict:
    """Fetch employee reviews for a company from AmbitionBox."""
    page = validate_page(page)
    async with browser.page_pool.acquire() as tab:
        try:
            company_slug = _ensure_slug(company_slug)
            url = f"{AMBITIONBOX_BASE}/reviews/{company_slug}-reviews?page={page}"

            logger.info("Navigating to AmbitionBox reviews page: %s", url)
            await page_goto(tab, url, wait="networkidle")

            # Check for 404 / invalid page
            if "404" in (await tab.title() or ""):
                return {"status": "error", "message": f"Page not found for company slug '{company_slug}'.", "error_code": "NOT_FOUND"}

            # Extract __NEXT_DATA__
            next_data = await _extract_next_data(tab)
            if not next_data:
                return {
                    "status": "error",
                    "message": "Could not extract review data from the page.",
                    "error_code": "BROWSER_ERROR",
                }

            props = safe_get(next_data, "props", field_name="props", warn=True, context=company_slug) or {}
            page_props = safe_get(props, "pageProps", field_name="pageProps", warn=True, context=company_slug) or {}
            if not page_props:
                return {
                    "status": "error",
                    "message": "pageProps is empty — the company slug may be invalid or the page didn't load correctly.",
                    "error_code": "BROWSER_ERROR",
                }

            # Extract company info — actual keys: companyName, companyHeaderData
            header_data = safe_get(page_props, "companyHeaderData") or {}
            company_name = (
                safe_get(page_props, "companyName")
                or safe_get(header_data, "CompanyName",
                            field_name="company_name", warn=True, context=company_slug)
                or company_slug
            )

            # Overall rating — ratingsData.overallCompanyRating (float like 3.8)
            rating_data = safe_get(page_props, "ratingsData",
                                   field_name="ratingsData", warn=True, context=company_slug) or {}
            overall_rating = safe_get(rating_data, "overallCompanyRating",
                                      field_name="overall_rating", warn=True, context=company_slug)
            review_count = safe_get(page_props, "fixedReviewCount", "reviewCount",
                                    field_name="review_count", warn=True, context=company_slug)

            # Rating distribution — ratingDistribution is dict {"5": 281, "4": 130, ...}
            rating_dist_raw = safe_get(page_props, "ratingDistribution") or {}
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
            raw_reviews = safe_get(page_props, "reviewsData", "detailedReviews",
                                   field_name="reviewsData", warn=True, context=company_slug) or []
            reviews = []
            if isinstance(raw_reviews, list):
                for rev in raw_reviews:
                    if not isinstance(rev, dict):
                        continue
                    # Extract designation from nested jobProfile object
                    job_profile = safe_get(rev, "jobProfile") or {}
                    designation = safe_get(job_profile, "name") if isinstance(job_profile, dict) else None
                    # Extract location from nested jobLocation object
                    job_location = safe_get(rev, "jobLocation") or {}
                    location = safe_get(job_location, "name") if isinstance(job_location, dict) else None

                    reviews.append({
                        "title": safe_get(rev, "reviewTitle",
                                          field_name="review_title", warn=True, context=company_slug),
                        "rating": safe_get(rev, "overallCompanyRating",
                                           field_name="review_rating", warn=True, context=company_slug),
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
            pagination = safe_get(page_props, "detailedReviewPagination", "pagination") or {}
            total_pages = safe_get(pagination, "totalPages", "lastPage")

            # Review summary (AI-generated)
            review_summary = safe_get(page_props, "reviewSummary")

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

            # Include AB company ID for REST enrichment
            ab_cid = _extract_company_id(page_props)
            if ab_cid:
                result["_ab_company_id"] = ab_cid

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
                "error_code": "BROWSER_ERROR",
            }


async def _fetch_interviews(company_slug: str, page: int = 1) -> dict:
    """Fetch interview experiences for a company from AmbitionBox."""
    page = validate_page(page)
    company_slug = _ensure_slug(company_slug)
    url = f"{AMBITIONBOX_BASE}/interviews/{company_slug}-interview-questions"
    if page > 1:
        url += f"?page={page}"

    async with browser.page_pool.acquire() as pg:
        try:
            await page_goto(pg, url, wait="networkidle")

            if "404" in (await pg.title() or ""):
                return {"status": "error", "message": f"No interview data for '{company_slug}'.", "error_code": "BROWSER_ERROR"}

            next_data = await _extract_next_data(pg)
            if not next_data:
                # Fallback: scrape interview data from rendered DOM
                dom_data = await _scrape_interview_data(pg)
                if dom_data and (dom_data.get("questions") or dom_data.get("experiences")):
                    company_name = dom_data.get("company_name") or company_slug

                    result = {
                        "status": "success",
                        "company_name": company_name,
                        "total_interviews": dom_data.get("total_interviews"),
                        "overall_rating": dom_data.get("rating"),
                        "overall_difficulty": dom_data.get("difficulty"),
                        "typical_duration": dom_data.get("duration"),
                        "source": "dom_scrape",
                        "count": len(dom_data.get("experiences", [])),
                        "interview_experiences": dom_data.get("experiences", []),
                        "sample_questions": dom_data.get("questions", [])[:10],
                    }
                    return result

                return {"status": "error", "message": "Could not extract interview data (no __NEXT_DATA__ and DOM scraping yielded no results).", "error_code": "NOT_FOUND"}

            props = safe_get(next_data, "props", field_name="props", warn=True, context=company_slug) or {}
            page_props = safe_get(props, "pageProps", field_name="pageProps", warn=True, context=company_slug) or {}

            # Company info
            company_data = (
                safe_get(page_props, "companyData", "companyInfo",
                         field_name="company_data", warn=True, context=company_slug)
                or {}
            )
            company_name = (
                safe_get(company_data, "CompanyName", "companyName",
                         field_name="company_name", warn=True, context=company_slug)
                or company_slug
            )

            # Interview overview
            overview = safe_get(page_props, "interviewOverview", "overview",
                                field_name="interview_overview", warn=True, context=company_slug) or {}
            total_interviews = (
                safe_get(overview, "totalInterviews", "interviewCount",
                         field_name="total_interviews", warn=True, context=company_slug)
                or safe_get(page_props, "totalInterviews")
            )
            overall_difficulty = safe_get(overview, "difficultyPercentage", "difficulty",
                                          field_name="overall_difficulty", warn=True, context=company_slug)

            # Individual experiences
            raw_interviews = (
                safe_get(page_props, "interviewReviews", "interviews", "interviewExperiences",
                         field_name="interview_experiences", warn=True, context=company_slug)
                or []
            )

            experiences = []
            for iv in raw_interviews:
                if not isinstance(iv, dict):
                    continue
                questions = safe_get(iv, "interviewQuestions", "questions") or []
                if isinstance(questions, str):
                    questions = [q.strip() for q in questions.split("\n") if q.strip()]

                job_profile = safe_get(iv, "jobProfile")
                experiences.append({
                    "designation": (
                        safe_get(job_profile, "name")
                        if isinstance(job_profile, dict)
                        else job_profile or safe_get(iv, "designation")
                    ),
                    "difficulty": safe_get(iv, "difficultyLevel", "difficulty"),
                    "outcome": safe_get(iv, "offerStatus", "outcome", "result"),
                    "experience_type": safe_get(iv, "experienceType", "interviewType"),
                    "duration": safe_get(iv, "duration", "interviewDuration"),
                    "rounds": safe_get(iv, "rounds", "interviewRounds"),
                    "questions": questions[:10],
                    "date": safe_get(iv, "created", "date"),
                    "likes": safe_get(iv, "likesText", "positives"),
                    "dislikes": safe_get(iv, "disLikesText", "negatives"),
                })

            result = {
                "status": "success",
                "company_name": company_name,
                "total_interviews": total_interviews,
                "overall_difficulty": overall_difficulty,
                "count": len(experiences),
                "interview_experiences": experiences,
            }

            # Include AB company ID for REST enrichment
            ab_cid = _extract_company_id(page_props)
            if ab_cid:
                result["_ab_company_id"] = ab_cid

            difficulty_data = safe_get(overview, "difficultyBreakdown", "difficultyDistribution")
            if difficulty_data:
                result["difficulty_breakdown"] = difficulty_data

            return result

        except Exception as e:
            return {"status": "error", "message": f"Interview experiences failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


# ---------------------------------------------------------------------------
# REST enrichment — supplements browser-scraped data with AB REST bridge
# ---------------------------------------------------------------------------


async def _enrich_with_rest(result: dict) -> dict:
    """Try to enrich a browser-scraped result with supplementary AB REST data.

    If the result contains an _ab_company_id (extracted from __NEXT_DATA__),
    fetches work_culture, benefits, and competitors in parallel via the REST
    bridge and merges them into the result under an 'ab_rest' key.

    A FAILED BRIDGE SAYS SO. Enrichment is best-effort, but "best-effort" was
    being spent as permission to report nothing at all: the reason lived only
    under the underscore-prefixed ``_enrichment_errors`` (which reads as an
    internal detail) and in a ``logger.debug`` line nobody reads, while
    ``naukri_company_intel`` still answered ``status: "success"`` with
    ``ab_rest`` simply absent. Measured 2026-08-24 on a live read-only call:
    all three helpers 403'd and the tool called itself a success.

    So every enriched result now carries ``ab_rest_status``:

        "ok"       -- every helper returned
        "degraded" -- some returned; ``ab_rest`` holds those
        "failed"   -- the bridge returned nothing; ``ab_rest`` is absent

    plus ``ab_rest_errors`` (a readable channel, not an underscore key) and a
    WARNING log line. ``status`` itself is deliberately NOT demoted: this data
    is documented as supplementary, and demoting it would make
    ``interview_prep._unpack`` discard the browser-scraped company rating and
    sample questions over a failed supplement -- one silently-wrong answer
    traded for another. ``ab_rest_status`` cannot be misread as a measurement,
    which is the same shape used by ``counted`` in the notification count and
    ``listed``/``not_listed`` in the daily brief.

    A result with no ``_ab_company_id`` is left untouched: never having tried
    is a different claim from having failed.
    """
    company_id = result.get("_ab_company_id")
    if not company_id:
        return result

    rest_data = {}
    rest_errors = []

    try:
        coros = [
            ab_get_work_culture(company_id),
            ab_get_benefits(company_id),
            ab_get_competitors(company_id),
        ]
        labels = ["work_culture", "benefits", "competitors"]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                rest_errors.append(f"{label}: {type(res).__name__}: {res}")
            elif isinstance(res, dict) and res.get("status") == "success":
                rest_data[label] = {k: v for k, v in res.items() if k != "status"}
            else:
                # A non-success dict used to fall through this loop without a
                # trace, which is a third way for the section to vanish.
                rest_errors.append("%s: %s" % (
                    label,
                    (res or {}).get("message", "no data") if isinstance(res, dict)
                    else "unexpected %s" % type(res).__name__))
    except Exception as e:
        rest_errors.append("enrichment: %s: %s" % (type(e).__name__, e))

    if rest_data:
        result["ab_rest"] = rest_data
    if rest_errors:
        result.setdefault("_enrichment_errors", []).extend(rest_errors)
        result["ab_rest_errors"] = list(rest_errors)

    if not rest_errors:
        result["ab_rest_status"] = "ok"
    elif rest_data:
        result["ab_rest_status"] = "degraded"
        logger.warning(
            "AmbitionBox ab_rest enrichment DEGRADED for company %s: %s",
            company_id, "; ".join(rest_errors)[:400])
    else:
        result["ab_rest_status"] = "failed"
        logger.warning(
            "AmbitionBox ab_rest bridge returned NOTHING for company %s: %s",
            company_id, "; ".join(rest_errors)[:400])

    return result


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def naukri_company_intel(
    company: str,
    intel_type: str = "salary",
    designation: str = "",
    page: int = 1,
) -> dict:
    """Unified company intelligence — salary data, employee reviews, or interview experiences from AmbitionBox.

    Note: Uses 'intel_type' instead of 'action' because each value selects a category of
    company intelligence to fetch (salary, reviews, interviews), not a CRUD operation.
    All dispatches are read-only scrapes from AmbitionBox.

    Tries AB REST bridge for supplementary data (work culture, benefits,
    competitors) when a numeric company ID can be extracted from the page.
    REST enrichment is best-effort but NOT quiet: when a company id was
    found, the result carries `ab_rest_status` ("ok" | "degraded" | "failed")
    and, on anything but "ok", `ab_rest_errors` naming what did not arrive.
    A missing `ab_rest` key with no `ab_rest_status` beside it means no
    company id could be extracted, so the bridge was never called.

    Note: AmbitionBox data is scraped, may break if site structure changes.

    Replaces separate salary/reviews/interviews tools. Accepts a company slug OR name.

    Args:
        company: AmbitionBox slug OR company name (e.g., "google" or "Google India Pvt. Ltd.")
        intel_type: "salary" | "reviews" | "interviews"
        designation: (salary only) Job title slug to filter (e.g., "software-engineer")
        page: Page number for reviews/interviews pagination (default 1)

    Returns:
        - salary:     {status, company, avg_salary, salaries: [...], ab_rest?: {...}, ...}
        - reviews:    {status, company, overall_rating, reviews: [...], ab_rest?: {...}, ...}
        - interviews: {status, company_name, interview_experiences: [...], ab_rest?: {...}, ...}
        - {status: "error", message} on failure
    """
    if intel_type not in _VALID_INTEL_TYPES:
        return {
            "status": "error",
            "message": f"Unknown intel_type '{intel_type}'. Use: {', '.join(_VALID_INTEL_TYPES)}",
            "error_code": "VALIDATION_ERROR",
        }

    slug = _ensure_slug(company)
    result = None

    # ── salary ─────────────────────────────────────────────────────────
    if intel_type == "salary":
        result = await _fetch_salary(company_slug=slug, designation=designation)

    # ── reviews ────────────────────────────────────────────────────────
    elif intel_type == "reviews":
        result = await _fetch_reviews(company_slug=slug, page=page)

    # ── interviews ─────────────────────────────────────────────────────
    elif intel_type == "interviews":
        result = await _fetch_interviews(company_slug=slug, page=page)

    # ── enrich with REST data if the scrape succeeded ─────────────────
    if isinstance(result, dict) and result.get("status") == "success":
        result = await _enrich_with_rest(result)

    return result

