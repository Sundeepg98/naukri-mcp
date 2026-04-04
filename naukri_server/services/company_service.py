"""Company service — core business logic for company search, follow, and research."""

import asyncio
import re
import statistics
from typing import Optional

from naukri_server.api import NaukriAPIError
from naukri_server.interfaces import api_client
from naukri_server.browser import browser, page_goto, page_intercept_json
from naukri_server.config import (
    NAUKRI_BASE, COMPANY_SEARCH_API, COMPANY_FOLLOW_STATUS_API,
    BATCH_FOLLOW_STATUS_API, INTERCEPT_WAIT_TIMEOUT, COMPANY_API_HEADERS,
    BROWSER_MODAL_APPEAR, logger,
)
from naukri_server.domain import safe_get
from naukri_server.tools.job_parsing import _parse_job_list
from naukri_server.utils import derive_slug
from naukri_server.validation import validate_company_list, validate_job_list, validate_limit, validate_page

__all__ = [
    # Company search & browse
    "parse_company",
    "first",
    "extract_slug_from_url",
    "search_companies",
    "get_company_jobs",
    "get_company_slug",
    "batch_get_slugs",
    # Follow management
    "batch_follow_status",
    "get_follow_status",
    "follow_or_unfollow",
    # Research
    "research_company",
    "salary_benchmark",
]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_company(group: dict) -> dict:
    """Parse a company from the search response."""
    tags = safe_get(group, "groupTags", default={}, field_name="groupTags")
    if not isinstance(tags, dict):
        tags = {}
    logo = safe_get(group, "groupLogo", default={}, field_name="groupLogo")
    return {
        "group_id": safe_get(group, "groupId", field_name="group_id", warn=True),
        "name": safe_get(group, "groupName", field_name="company_name", warn=True),
        "type": first(tags.get("businessSize")),
        "industry": first(tags.get("primaryIndustry")),
        "size": first(tags.get("employeesCount")),
        "ownership": first(tags.get("ownershipType")),
        "rating": safe_get(group, "rating", field_name="rating", warn=True),
        "review_count": safe_get(group, "reviewsCount", field_name="review_count", warn=True),
        "logo_url": logo.get("desktop") if isinstance(logo, dict) else None,
        "jobs_url": safe_get(group, "groupJobsURL", field_name="jobs_url"),
    }


def first(lst):
    """Return first element of list or None."""
    return lst[0] if isinstance(lst, list) and lst else None


def extract_slug_from_url(url: str) -> str | None:
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


# ---------------------------------------------------------------------------
# Company search & browse
# ---------------------------------------------------------------------------

async def search_companies(keyword: str, page: int = 1, limit: int = 20) -> dict:
    """Search for companies on Naukri.com by name or industry."""
    limit = validate_limit(limit)
    page = validate_page(page)
    seo_key = keyword.lower().replace(" ", "-")
    data = await api_client.get(
        COMPANY_SEARCH_API,
        params={
            "seoKey": seo_key,
            "urltype": "search_by_company_general",
            "pageNo": str(page),
            "qcount": str(limit),
            "searchType": "companySearch",
        },
        extra_headers=COMPANY_API_HEADERS,
    )

    groups = safe_get(data, "groupDetails", default=[], field_name="groupDetails", warn=True, context=f"keyword={keyword}")
    companies = [parse_company(g) for g in groups]

    total = safe_get(data, "noOfGroups", field_name="noOfGroups", warn=True, context=f"keyword={keyword}")
    result = {
        "status": "success",
        "keyword": keyword,
        "page": page,
        "total": total,
        "count": len(companies),
        "companies": companies,
    }
    warnings = validate_company_list(companies, total)
    if warnings:
        result["warnings"] = warnings
    return result


async def get_company_jobs(group_id: str, page: int = 1, limit: int = 20) -> dict:
    """Get job listings from a specific company on Naukri.com."""
    limit = validate_limit(limit)
    page = validate_page(page)
    page_no = page  # save before shadowing by page_pool
    async with browser.page_pool.acquire() as page:
        try:
            # Navigate to company jobs page and intercept the search API response
            page_url = f"{NAUKRI_BASE}/jobs-in-{group_id}?groupId={group_id}&searchType=groupidsearch"
            if page_no > 1:
                page_url += f"&pageNo={page_no}"

            data = await page_intercept_json(page, page_url, url_pattern="/jobapi/v3/search")
            if not data:
                return {"status": "error", "message": "Search API response not captured for company jobs.", "error_code": "BROWSER_ERROR"}

            job_details = safe_get(data, "jobDetails", default=[], field_name="jobDetails", warn=True, context=f"group_id={group_id}")
            jobs = _parse_job_list(job_details, limit)

            total = safe_get(data, "noOfJobs", field_name="noOfJobs", warn=True, context=f"group_id={group_id}")
            result = {
                "status": "success",
                "group_id": group_id,
                "page": page_no,
                "total": total,
                "count": len(jobs),
                "jobs": jobs,
            }
            warnings = validate_job_list(jobs, total, "company_jobs")
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as e:
            return {"status": "error", "message": f"Failed to get company jobs: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


async def get_company_slug(group_id: str) -> dict:
    """Convert a Naukri company group_id to an AmbitionBox company slug."""
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
                except Exception as e:
                    logger.debug("Company response intercept: %s", e)

            page.on("response", on_response)
            try:
                await page_goto(page, page_url)
                try:
                    await asyncio.wait_for(event.wait(), timeout=INTERCEPT_WAIT_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("Company API response not captured after %ss for group_id: %s", INTERCEPT_WAIT_TIMEOUT, group_id)
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
            slug = extract_slug_from_url(ab_url)

            if slug:
                return {
                    "status": "success",
                    "group_id": group_id,
                    "company_slug": slug,
                    "company_name": company_name or slug.replace("-", " ").title(),
                }

            # Strategy 2: Scrape AmbitionBox links from the rendered page
            logger.info("API interception didn't yield slug, trying DOM scrape")
            await asyncio.sleep(BROWSER_MODAL_APPEAR)  # Let page render fully
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
                slug = extract_slug_from_url(ab_link)
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
                    "_note": "Slug derived from company name — may not match AmbitionBox exactly. Verify with naukri_company_intel.",
                }

            return {
                "status": "error",
                "message": f"Could not determine AmbitionBox slug for group_id '{group_id}'. The company page may not have AmbitionBox integration.",
                "error_code": "NOT_FOUND",
            }

        except Exception as e:
            logger.error("Failed to get company slug: %s: %s", type(e).__name__, e)
            return {
                "status": "error",
                "message": f"Failed to get company slug: {type(e).__name__}: {e}",
                "error_code": "BROWSER_ERROR",
            }


async def batch_get_slugs(group_ids: list[str]) -> dict:
    """Convert multiple Naukri group_ids to AmbitionBox slugs."""
    results = {}
    for gid in group_ids:
        try:
            result = await get_company_slug(group_id=gid)
            if result.get("status") == "success":
                results[gid] = result.get("slug") or result.get("company_slug") or derive_slug(gid)
            else:
                results[gid] = None
        except Exception:
            results[gid] = None
    return {"status": "success", "slugs": results, "count": len(results)}


# ---------------------------------------------------------------------------
# Follow management
# ---------------------------------------------------------------------------

async def batch_follow_status(group_ids: list) -> dict:
    """Check follow status for multiple companies in one POST call."""
    data = await api_client.post(
        BATCH_FOLLOW_STATUS_API,
        body={"groups": [int(gid) for gid in group_ids]},
    )

    followed = safe_get(data, "followedGroups", default=[], field_name="followedGroups", warn=True)
    unfollowed = safe_get(data, "unfollowedGroups", default=[], field_name="unfollowedGroups", warn=True)

    companies = []
    for g in followed:
        companies.append({
            "group_id": safe_get(g, "id", field_name="group_id", warn=True),
            "name": safe_get(g, "name", field_name="company_name", warn=True),
            "follower_count": safe_get(g, "followerCount", field_name="follower_count"),
            "is_followed": True,
            "logo": safe_get(g, "groupLogo", field_name="logo"),
        })
    for g in unfollowed:
        companies.append({
            "group_id": safe_get(g, "id", field_name="group_id", warn=True),
            "name": safe_get(g, "name", field_name="company_name", warn=True),
            "follower_count": safe_get(g, "followerCount", field_name="follower_count"),
            "is_followed": False,
            "logo": safe_get(g, "groupLogo", field_name="logo"),
        })

    return {
        "status": "success",
        "total": len(companies),
        "followed_count": len(followed),
        "unfollowed_count": len(unfollowed),
        "companies": companies,
    }


async def get_follow_status(group_ids: list[str]) -> dict:
    """Check follow status for one or more companies via the API."""
    query = ",".join(group_ids)
    data = await api_client.get(
        COMPANY_FOLLOW_STATUS_API,
        params={"query": query},
    )
    followed_raw = safe_get(data, "followedGroups", default=[], field_name="followedGroups", warn=True)
    followed_ids = set()
    for g in followed_raw:
        gid = str(safe_get(g, "id", default=g, field_name="group_id") if isinstance(g, dict) else g)
        followed_ids.add(gid)

    followed = [gid for gid in group_ids if gid in followed_ids]
    not_followed = [gid for gid in group_ids if gid not in followed_ids]

    return {
        "status": "success",
        "followed": followed,
        "not_followed": not_followed,
    }


async def follow_or_unfollow(group_ids: list[str], action: str) -> dict:
    """Follow or unfollow one or more companies via the API."""
    results = []
    errors = []
    for group_id in group_ids:
        try:
            await api_client.post(
                COMPANY_FOLLOW_STATUS_API,
                body={"groupId": group_id, "action": action},
            )
            results.append(group_id)
        except NaukriAPIError as e:
            errors.append({"group_id": group_id, "message": str(e), "http_status": e.status})
        except Exception as e:
            errors.append({"group_id": group_id, "message": f"{type(e).__name__}: {e}"})

    result = {
        "status": "success" if not errors else ("partial_success" if results else "error"),
        "action": f"{action}ed",
        f"{action}ed": results,
    }
    if errors:
        result["errors"] = errors
    return result


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

async def research_company(
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
    logger.info("Researching company: %s", keyword)

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
                    companies = safe_get(company_result, "companies", default=[], field_name="companies", warn=True, context=f"keyword={keyword}")
                    if companies:
                        group_id = safe_get(companies[0], "group_id", field_name="group_id", warn=True, context=f"keyword={keyword}")
                        result["company_name"] = safe_get(companies[0], "name", default=keyword, field_name="company_name", warn=True, context=f"keyword={keyword}")

                if group_id:
                    # Step 2: Get company jobs by group_id (exact match)
                    jobs_result = await _get_company_jobs(group_id=str(group_id), limit=jobs_limit)
                    if jobs_result.get("status") == "success":
                        result["jobs"] = {
                            "total": safe_get(jobs_result, "total", default=0, field_name="total", context=f"company_jobs:{keyword}"),
                            "matching": safe_get(jobs_result, "count", default=0, field_name="count", context=f"company_jobs:{keyword}"),
                            "sample": safe_get(jobs_result, "jobs", default=[], field_name="jobs", context=f"company_jobs:{keyword}")[:jobs_limit],
                        }
                    else:
                        errors.append(f"Jobs: {jobs_result.get('message', 'unknown error')}")
                else:
                    # Fallback: keyword search if company not found
                    from naukri_server.tools.search import naukri_search_jobs
                    jobs_result = await naukri_search_jobs(keywords=keyword, limit=jobs_limit)
                    if jobs_result.get("status") == "success":
                        jobs = safe_get(jobs_result, "jobs", default=[], field_name="jobs", context=f"search_fallback:{keyword}")
                        keyword_lower = keyword.lower()
                        matching = [j for j in jobs if keyword_lower in (j.get("company") or "").lower()]
                        result["jobs"] = {
                            "total": safe_get(jobs_result, "total", field_name="total", context=f"search_fallback:{keyword}"),
                            "matching": len(matching),
                            "sample": matching if matching else jobs[:jobs_limit],
                        }
                    else:
                        errors.append(f"Jobs: {jobs_result.get('message', 'unknown error')}")
            except Exception as e:
                errors.append(f"Jobs: {type(e).__name__}: {e}")
                logger.warning("Research company jobs failed: %s", e)

        # Step 2: Get AmbitionBox data (REST bridge first, browser fallback)
        if include_reviews or include_interviews:
            gather_coros = []
            gather_labels = []

            # Try AB REST bridge for work culture + benefits (fast, no browser)
            try:
                from naukri_server.tools.ambitionbox import ab_get_work_culture, ab_get_benefits
                # company_id lookup — use group_id if available
                if result.get("jobs", {}).get("sample"):
                    sample_job = result["jobs"]["sample"][0]
                    ab_company_id = str(sample_job.get("company_id", ""))
                    if ab_company_id:
                        gather_coros.append(ab_get_work_culture(ab_company_id))
                        gather_labels.append("work_culture")
                        gather_coros.append(ab_get_benefits(ab_company_id))
                        gather_labels.append("benefits")
            except Exception:
                pass  # REST bridge unavailable, fall back to browser

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

            # AB REST results (work culture + benefits)
            for ab_key in ("work_culture", "benefits"):
                ab_result = results_map.get(ab_key)
                if ab_result is not None:
                    if isinstance(ab_result, Exception):
                        errors.append(f"{ab_key}: {type(ab_result).__name__}: {ab_result}")
                    elif isinstance(ab_result, dict) and ab_result.get("status") == "success":
                        result[ab_key] = {k: v for k, v in ab_result.items() if k != "status"}
                    # Silently skip failed AB REST calls (not critical)

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


async def salary_benchmark(
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
    logger.info("Salary benchmark: keywords=%s, location=%s, sample_size=%d", keywords, location, sample_size)
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
        return {"status": "error", "message": f"Salary benchmark timed out after {timeout_seconds}s", "error_code": "TIMEOUT"}
