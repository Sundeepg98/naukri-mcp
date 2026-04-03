"""AmbitionBox company intel and slug-bridge tools — scrapes __NEXT_DATA__ from SSR pages.

Includes REST bridge for direct API calls to AmbitionBox service gateways
using cookies extracted from the browser context.

naukri_company_intel automatically enriches browser-scraped results with
supplementary REST data (work_culture, benefits, competitors) when a
numeric company ID can be extracted from the __NEXT_DATA__ pageProps.
"""

import asyncio
import logging
import re
import time

import aiohttp

from naukri_server import mcp
from naukri_server.browser import browser, page_goto
from naukri_server.config import (
    AMBITIONBOX_BASE, AMBITIONBOX_FALLBACK_SLEEP, AMBITIONBOX_WAIT_TIMEOUT,
    AB_SALARY_API, AB_BENEFITS_API, AB_REVIEW_DIST_API, AB_INTERVIEW_QS_API,
    AB_COMPANY_COMPARE_API, AB_COMPANY_LOCATIONS_API, AB_INSIGHTS_APPLIED_API,
    AB_COOKIE_TTL,
)
from naukri_server.utils import derive_slug
from naukri_server.validation import validate_salary_data, validate_review_data, validate_page

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
    meta = page_props.get("metaData") or {}
    cid = meta.get("companyId")
    if cid:
        return str(cid)
    # Fallback: companyData / companyHeaderData / companyInfo
    for key in ("companyData", "companyHeaderData", "companyInfo", "company"):
        container = page_props.get(key)
        if isinstance(container, dict):
            for id_key in ("CompanyId", "companyId", "id"):
                val = container.get(id_key)
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

            page_props = next_data.get("props", {}).get("pageProps", {})
            if not page_props:
                return {
                    "status": "error",
                    "message": "pageProps is empty — the company slug may be invalid or the page didn't load correctly.",
                    "error_code": "BROWSER_ERROR",
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

            page_props = next_data.get("props", {}).get("pageProps", {})
            if not page_props:
                return {
                    "status": "error",
                    "message": "pageProps is empty — the company slug may be invalid or the page didn't load correctly.",
                    "error_code": "BROWSER_ERROR",
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

            page_props = next_data.get("props", {}).get("pageProps", {})

            # Company info
            company_data = (
                page_props.get("companyData", {})
                or page_props.get("companyInfo", {})
                or {}
            )
            company_name = (
                company_data.get("CompanyName")
                or company_data.get("companyName")
                or company_slug
            )

            # Interview overview
            overview = page_props.get("interviewOverview", {}) or page_props.get("overview", {}) or {}
            total_interviews = (
                overview.get("totalInterviews")
                or overview.get("interviewCount")
                or page_props.get("totalInterviews")
            )
            overall_difficulty = (
                overview.get("difficultyPercentage")
                or overview.get("difficulty")
            )

            # Individual experiences
            raw_interviews = (
                page_props.get("interviewReviews")
                or page_props.get("interviews")
                or page_props.get("interviewExperiences")
                or []
            )

            experiences = []
            for iv in raw_interviews:
                if not isinstance(iv, dict):
                    continue
                questions = iv.get("interviewQuestions") or iv.get("questions") or []
                if isinstance(questions, str):
                    questions = [q.strip() for q in questions.split("\n") if q.strip()]

                experiences.append({
                    "designation": (
                        iv.get("jobProfile", {}).get("name")
                        if isinstance(iv.get("jobProfile"), dict)
                        else iv.get("jobProfile") or iv.get("designation")
                    ),
                    "difficulty": iv.get("difficultyLevel") or iv.get("difficulty"),
                    "outcome": iv.get("offerStatus") or iv.get("outcome") or iv.get("result"),
                    "experience_type": iv.get("experienceType") or iv.get("interviewType"),
                    "duration": iv.get("duration") or iv.get("interviewDuration"),
                    "rounds": iv.get("rounds") or iv.get("interviewRounds"),
                    "questions": questions[:10],
                    "date": iv.get("created") or iv.get("date"),
                    "likes": iv.get("likesText") or iv.get("positives"),
                    "dislikes": iv.get("disLikesText") or iv.get("negatives"),
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

            difficulty_data = overview.get("difficultyBreakdown") or overview.get("difficultyDistribution")
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

    Failures are silently swallowed — enrichment is best-effort.
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
                logger.debug("REST enrichment %s failed: %s", label, res)
            elif isinstance(res, dict) and res.get("status") == "success":
                rest_data[label] = {k: v for k, v in res.items() if k != "status"}
    except Exception as e:
        logger.debug("REST enrichment failed entirely: %s", e)

    if rest_data:
        result["ab_rest"] = rest_data
    if rest_errors:
        result.setdefault("_enrichment_errors", []).extend(rest_errors)

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
    REST enrichment is best-effort — failures are silent.

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


# ═══════════════════════════════════════════════════════════════════════
# AmbitionBox REST Bridge — direct API calls using browser cookies
# ═══════════════════════════════════════════════════════════════════════

_ab_cookies: dict = {}
_ab_cookies_ts: float = 0.0
_ab_cookies_lock = asyncio.Lock()


async def _get_ab_cookies() -> dict:
    """Extract AmbitionBox cookies from the browser context.

    Navigates to an AB page if cookies are stale (older than AB_COOKIE_TTL).
    Returns a dict of cookie name→value pairs.
    """
    global _ab_cookies, _ab_cookies_ts
    now = time.time()
    if _ab_cookies and (now - _ab_cookies_ts) < AB_COOKIE_TTL:
        return _ab_cookies
    async with _ab_cookies_lock:
        # Double-check after acquiring lock
        now = time.time()
        if _ab_cookies and (now - _ab_cookies_ts) < AB_COOKIE_TTL:
            return _ab_cookies
        async with browser.page_pool.acquire() as page:
            await page_goto(page, f"{AMBITIONBOX_BASE}/list-of-companies")
            cookies_list = await page.context.cookies(AMBITIONBOX_BASE)
            _ab_cookies = {c["name"]: c["value"] for c in cookies_list}
            _ab_cookies_ts = time.time()
    return _ab_cookies


async def _ab_rest_get(url: str, params: dict = None) -> dict:
    """GET request to an AmbitionBox REST endpoint using cached cookies.

    Retries once with fresh cookies on 401/403.
    """
    global _ab_cookies, _ab_cookies_ts
    cookies = await _get_ab_cookies()

    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(cookies=cookies) as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status in (401, 403) and attempt == 0:
                        # Invalidate cookies and retry
                        _ab_cookies = {}
                        _ab_cookies_ts = 0.0
                        cookies = await _get_ab_cookies()
                        continue
                    resp.raise_for_status()
                    return await resp.json()
        except aiohttp.ClientError as e:
            if attempt == 0:
                _ab_cookies = {}
                _ab_cookies_ts = 0.0
                cookies = await _get_ab_cookies()
                continue
            raise
    return {}


async def ab_get_benefits(company_id: str) -> dict:
    """Fetch 30 benefit categories for a company via REST."""
    url = f"{AB_BENEFITS_API}/{company_id}/benefits-stats"
    data = await _ab_rest_get(url)
    benefits_data = data.get("data", {})
    return {
        "status": "success",
        "total_benefits": benefits_data.get("totalBenefits", 0),
        "benefits": benefits_data.get("benefits", []),
    }


async def ab_get_work_culture(company_id: str) -> dict:
    """Fetch work culture distribution (timing, travel, work days, shifts)."""
    url = f"{AB_REVIEW_DIST_API}/{company_id}"
    data = await _ab_rest_get(url)
    dist = data.get("data", {})
    return {
        "status": "success",
        "reviews_count": dist.get("reviewsCount", 0),
        "work_timing": {
            "labels": dist.get("workMonitorLabels", []),
            "values": dist.get("workMonitorSeries", []),
            "insight": dist.get("workMonitorInsight", ""),
        },
        "travel": {
            "labels": dist.get("travelTagsLabels", []),
            "values": dist.get("travelTagsSeries", []),
        },
        "work_days": {
            "labels": dist.get("workDaysLabels", []),
            "values": dist.get("workDaysSeries", []),
        },
        "shifts": {
            "labels": dist.get("shiftsLabels", []),
            "values": dist.get("shiftsSeries", []),
        },
    }


async def ab_get_interview_questions(company_id: str, designation_id: str = None, limit: int = 7) -> dict:
    """Fetch actual interview questions by company and role."""
    params = {"companyId": company_id, "limit": str(limit)}
    if designation_id:
        params["designation"] = designation_id
    data = await _ab_rest_get(AB_INTERVIEW_QS_API, params=params)
    meta = data.get("meta", {})
    return {
        "status": "success",
        "total_interviews": data.get("totalInterviewExperiencesLive", 0),
        "count": meta.get("count", 0),
        "questions": data.get("data", []),
    }


async def ab_get_competitors(company_id: str) -> dict:
    """Fetch top 41 competitors with ratings and review counts."""
    url = f"{AB_COMPANY_COMPARE_API}/{company_id}"
    data = await _ab_rest_get(url)
    competitors = data if isinstance(data, list) else data.get("data", [])
    return {
        "status": "success",
        "count": len(competitors),
        "competitors": competitors,
    }


async def ab_get_locations(company_id: str) -> dict:
    """Fetch all office locations for a company."""
    url = f"{AB_COMPANY_LOCATIONS_API}/{company_id}/sectionalDetails"
    data = await _ab_rest_get(url, params={"sections": "companyLocations"})
    locations = data.get("companyLocations", [])
    return {
        "status": "success",
        "count": len(locations),
        "locations": locations,
    }


async def ab_get_applied_jobs_insights() -> dict:
    """Fetch salary insights for jobs you've applied to on Naukri.

    Cross-references Naukri applications with AmbitionBox salary data.
    No parameters needed — returns insights for your recent applications.
    """
    data = await _ab_rest_get(AB_INSIGHTS_APPLIED_API)
    insights = data if isinstance(data, list) else data.get("data", [])
    return {
        "status": "success",
        "count": len(insights),
        "insights": insights,
    }


async def ab_get_salary_rest(company_id: str, job_profile_id: str) -> dict:
    """Fetch detailed salary data (17 fields) for a specific role at a company."""
    url = f"{AB_SALARY_API}/{company_id}/jobProfile/{job_profile_id}/salaryData"
    data = await _ab_rest_get(url)
    salary_data = data.get("data", {})
    summary = salary_data.get("summaryData", {})
    return {
        "status": "success",
        "profile_info": salary_data.get("profileInfo", {}),
        "summary": summary,
        "confidence": salary_data.get("confidence", ""),
        "experience_levels": salary_data.get("experienceLevels", []),
        "ctc_comparison": salary_data.get("ctcComparison", {}),
        "take_home": salary_data.get("takeHomeSalary", {}),
    }


