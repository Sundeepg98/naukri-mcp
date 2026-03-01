"""AmbitionBox salary, review, and slug-bridge tools — scrapes __NEXT_DATA__ from SSR pages."""

import asyncio
import logging
import re

from naukri_server import mcp
from naukri_server.browser import browser, page_goto
from naukri_server.config import NAUKRI_BASE, AMBITIONBOX_BASE
from naukri_server.utils import derive_slug
from naukri_server.validation import validate_salary_data, validate_review_data

logger = logging.getLogger(__name__)


def _ensure_slug(value: str) -> str:
    """Accept either a slug (lowercase-dashed) or a company name; return a slug."""
    if re.match(r'^[a-z0-9][a-z0-9-]*$', value):
        return value  # Already a slug
    # Derive from company name
    name = value.strip()
    for suffix in ("Pvt. Ltd.", "Pvt Ltd", "Private Limited", "Ltd.", "Ltd",
                   "Limited", "Inc.", "Inc", "Corp.", "Corp", "Corporation",
                   "LLP", "LLC", "Technologies", "Technology", "Solutions",
                   "Services", "India"):
        if name.lower().endswith(suffix.lower()):
            name = name[:len(name) - len(suffix)].strip()
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return re.sub(r'-+', '-', slug)


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


# ============================================================================
# Tool: Get Company Salary Data from AmbitionBox
# ============================================================================


@mcp.tool()
async def naukri_get_company_salary(company_slug: str, designation: str = "") -> dict:
    """Get salary data for a company from AmbitionBox — avg salary, ranges, and experience breakdowns.

    Args:
        company_slug: AmbitionBox URL slug OR company name (e.g., "google" or "Google India Pvt. Ltd.")
        designation: Optional job title slug (e.g., "software-engineer", "data-scientist")
                     If provided, returns salary for that specific role.

    Returns:
        - {status: "success", company, salaries: [...], ...}
        - {status: "error", message}
    """
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
        company_slug: AmbitionBox URL slug OR company name (e.g., "google" or "Google India Pvt. Ltd.")
        page: Page number for review pagination (default: 1)

    Returns:
        - {status: "success", company, overall_rating, review_count, reviews: [...], ...}
        - {status: "error", message}
    """
    async with browser.page_pool.acquire() as tab:
        try:
            company_slug = _ensure_slug(company_slug)
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


# ============================================================================
# Tool: Get Interview Experiences from AmbitionBox
# ============================================================================


@mcp.tool()
async def naukri_get_interview_experiences(
    company_slug: str,
    page: int = 1,
) -> dict:
    """Get interview experiences for a company from AmbitionBox.

    Returns interview questions, difficulty ratings, process details, and offer outcomes.

    Args:
        company_slug: AmbitionBox URL slug OR company name (e.g., "google" or "Google India Pvt. Ltd.")
        page: Page number for pagination (default 1)

    Returns:
        - {status: "success", company_name, total_interviews, overall_difficulty,
           interview_experiences: [{designation, difficulty, outcome, experience_type,
           duration, questions, date}]}
        - {status: "error", message}
    """
    company_slug = _ensure_slug(company_slug)
    url = f"{AMBITIONBOX_BASE}/interviews/{company_slug}-interview-questions"
    if page > 1:
        url += f"?page={page}"

    async with browser.page_pool.acquire() as pg:
        try:
            await page_goto(pg, url, wait="networkidle")

            if "404" in (await pg.title() or ""):
                return {"status": "error", "message": f"No interview data for '{company_slug}'."}

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

                return {"status": "error", "message": "Could not extract interview data (no __NEXT_DATA__ and DOM scraping yielded no results)."}

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

            difficulty_data = overview.get("difficultyBreakdown") or overview.get("difficultyDistribution")
            if difficulty_data:
                result["difficulty_breakdown"] = difficulty_data

            return result

        except Exception as e:
            return {"status": "error", "message": f"Interview experiences failed: {type(e).__name__}: {e}"}


# ============================================================================
# Tool: Convert Naukri group_id to AmbitionBox company slug
# ============================================================================


@mcp.tool()
async def naukri_get_company_slug(group_id: str) -> dict:
    """Convert a Naukri company group_id to an AmbitionBox company slug.

    Use this to bridge between Naukri company tools (which use group_id from
    naukri_search_companies) and AmbitionBox tools (naukri_get_company_salary,
    naukri_get_company_reviews) which require company_slug.

    Requires: group_id from naukri_search_companies results.

    Note: naukri_get_company_salary and naukri_get_company_reviews now auto-derive
    slugs from company names. Use this tool only when you specifically need the
    canonical AmbitionBox slug (e.g., for URL construction).

    Args:
        group_id: Naukri company group ID (from naukri_search_companies)

    Returns:
        - {status: "success", group_id, company_slug, company_name}
        - {status: "error", message}
    """
    async with browser.page_pool.acquire() as page:
        try:
            # Navigate to the Naukri company overview page — it embeds AmbitionBox links
            page_url = f"{NAUKRI_BASE}/company/{group_id}"
            logger.info("Navigating to Naukri company page for slug extraction: %s", page_url)

            # Intercept the company API response which contains ambitionBoxData
            captured = {}
            event = asyncio.Event()

            async def on_response(response):
                try:
                    url = response.url
                    # Company info API returns ambitionBoxData with Url field
                    if response.status == 200 and (
                        f"/companyapi/v1/company/{group_id}" in url
                        or f"/companyapi/v2/company/{group_id}" in url
                        or (f"/company/{group_id}" in url and "api" in url)
                    ):
                        data = await response.json()
                        captured["company"] = data
                        event.set()
                except Exception:
                    pass

            page.on("response", on_response)
            try:
                await page_goto(page, page_url)
                try:
                    await asyncio.wait_for(event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    logger.warning("Company API response not captured after 10s for group_id: %s", group_id)
            finally:
                page.remove_listener("response", on_response)

            # Strategy 1: Extract from intercepted API response
            company_data = captured.get("company", {})
            ambition_data = (
                company_data.get("ambitionBoxData")
                or company_data.get("ambitionBox")
                or {}
            )
            company_name = (
                company_data.get("companyName")
                or company_data.get("groupName")
                or company_data.get("name")
            )

            # Look for AmbitionBox URL in the data — e.g., Url: "/reviews/google-reviews"
            ab_url = ambition_data.get("Url") or ambition_data.get("url") or ""
            slug = _extract_slug_from_url(ab_url)

            if slug:
                return {
                    "status": "success",
                    "group_id": group_id,
                    "company_slug": slug,
                    "company_name": company_name or slug.replace("-", " ").title(),
                }

            # Strategy 2: Scrape AmbitionBox links from the rendered page
            logger.info("API interception didn't yield slug, trying DOM scrape")
            await asyncio.sleep(2)  # Let page render fully
            ab_link = await page.evaluate("""
                () => {
                    // Look for links to ambitionbox.com
                    for (const a of document.querySelectorAll('a[href*="ambitionbox.com"]')) {
                        return a.href;
                    }
                    // Fallback: look for data attributes or embedded JSON
                    const scripts = document.querySelectorAll('script');
                    for (const s of scripts) {
                        const text = s.textContent || '';
                        const match = text.match(/ambitionbox\\.com\\/(?:reviews|salaries)\\/([a-z0-9-]+)/i);
                        if (match) return match[0];
                    }
                    return null;
                }
            """)

            if ab_link:
                slug = _extract_slug_from_url(ab_link)
                if slug:
                    # Get company name from page if not already found
                    if not company_name:
                        company_name = await page.evaluate("""
                            () => {
                                const h1 = document.querySelector('h1');
                                return h1 ? h1.textContent.trim() : null;
                            }
                        """)
                    return {
                        "status": "success",
                        "group_id": group_id,
                        "company_slug": slug,
                        "company_name": company_name or slug.replace("-", " ").title(),
                    }

            # Strategy 3: Derive slug from company name (fallback)
            if not company_name:
                company_name = await page.evaluate("""
                    () => {
                        const h1 = document.querySelector('h1');
                        return h1 ? h1.textContent.trim() : null;
                    }
                """)

            if company_name:
                slug = derive_slug(company_name)
                return {
                    "status": "success",
                    "group_id": group_id,
                    "company_slug": slug,
                    "company_name": company_name,
                    "derived": True,
                    "_note": "Slug derived from company name — may not match AmbitionBox exactly. Verify with naukri_get_company_salary.",
                }

            return {
                "status": "error",
                "message": f"Could not determine AmbitionBox slug for group_id '{group_id}'. The company page may not have AmbitionBox integration.",
            }

        except Exception as e:
            logger.error("Failed to get company slug: %s: %s", type(e).__name__, e)
            return {
                "status": "error",
                "message": f"Failed to get company slug: {type(e).__name__}: {e}",
            }


def _extract_slug_from_url(url: str) -> str | None:
    """Extract AmbitionBox company slug from a URL like /reviews/google-reviews or /salaries/tcs-salaries."""
    if not url:
        return None
    # Match patterns: /reviews/{slug}-reviews, /salaries/{slug}-salaries, /overview/{slug}-overview
    match = re.search(r'/(?:reviews|salaries|overview)/([a-z0-9-]+?)(?:-(?:reviews|salaries|overview))?(?:\?|$|/)', url, re.IGNORECASE)
    if match:
        return match.group(1)
    # Simpler pattern: ambitionbox.com/{section}/{slug}
    match = re.search(r'ambitionbox\.com/\w+/([a-z0-9-]+)', url, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


