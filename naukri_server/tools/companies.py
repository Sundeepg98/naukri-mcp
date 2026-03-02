"""Company tools — search, browse jobs, get slug, follow/unfollow."""

import asyncio
import re
from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.browser import browser, page_goto, page_intercept_json
from naukri_server.config import NAUKRI_BASE, COMPANY_SEARCH_API, COMPANY_FOLLOW_STATUS_API, logger
from naukri_server.tools.job_parsing import _parse_job_list
from naukri_server.utils import derive_slug
from naukri_server.validation import validate_company_list, validate_job_list, validate_limit, validate_page

_COMPANY_HEADERS = {"appid": "103"}


def _parse_company(group: dict) -> dict:
    """Parse a company from the search response."""
    tags = group.get("groupTags", {})
    if not isinstance(tags, dict):
        tags = {}
    logo = group.get("groupLogo", {})
    return {
        "group_id": group.get("groupId"),
        "name": group.get("groupName"),
        "type": _first(tags.get("businessSize")),
        "industry": _first(tags.get("primaryIndustry")),
        "size": _first(tags.get("employeesCount")),
        "ownership": _first(tags.get("ownershipType")),
        "rating": group.get("rating"),
        "review_count": group.get("reviewsCount"),
        "logo_url": logo.get("desktop") if isinstance(logo, dict) else None,
        "jobs_url": group.get("groupJobsURL"),
    }


def _first(lst):
    """Return first element of list or None."""
    return lst[0] if isinstance(lst, list) and lst else None


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


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified naukri_company tool)
# ---------------------------------------------------------------------------

async def _search_companies(keyword: str, page: int = 1, limit: int = 20) -> dict:
    """Search for companies on Naukri.com by name or industry."""
    limit = validate_limit(limit)
    page = validate_page(page)
    seo_key = keyword.lower().replace(" ", "-")
    data = await api_get(
        COMPANY_SEARCH_API,
        params={
            "seoKey": seo_key,
            "urltype": "search_by_company_general",
            "pageNo": str(page),
            "qcount": str(limit),
            "searchType": "companySearch",
        },
        extra_headers=_COMPANY_HEADERS,
    )

    groups = data.get("groupDetails", [])
    companies = [_parse_company(g) for g in groups]

    result = {
        "status": "success",
        "keyword": keyword,
        "page": page,
        "total": data.get("noOfGroups"),
        "count": len(companies),
        "companies": companies,
    }
    warnings = validate_company_list(companies, data.get("noOfGroups"))
    if warnings:
        result["warnings"] = warnings
    return result


async def _get_company_jobs(group_id: str, page: int = 1, limit: int = 20) -> dict:
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

            job_details = data.get("jobDetails", [])
            jobs = _parse_job_list(job_details, limit)

            result = {
                "status": "success",
                "group_id": group_id,
                "page": page_no,
                "total": data.get("noOfJobs"),
                "count": len(jobs),
                "jobs": jobs,
            }
            warnings = validate_job_list(jobs, data.get("noOfJobs"), "company_jobs")
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as e:
            return {"status": "error", "message": f"Failed to get company jobs: {type(e).__name__}: {e}", "error_code": "API_ERROR"}


async def _get_company_slug(group_id: str) -> dict:
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


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified follow tool)
# ---------------------------------------------------------------------------

async def _get_follow_status(group_ids: list[str]) -> dict:
    """Check follow status for one or more companies via the API."""
    query = ",".join(group_ids)
    data = await api_get(
        COMPANY_FOLLOW_STATUS_API,
        params={"query": query},
    )
    followed_raw = data.get("followedGroups", [])
    followed_ids = set()
    for g in followed_raw:
        gid = str(g.get("id", g) if isinstance(g, dict) else g)
        followed_ids.add(gid)

    followed = [gid for gid in group_ids if gid in followed_ids]
    not_followed = [gid for gid in group_ids if gid not in followed_ids]

    return {
        "status": "success",
        "followed": followed,
        "not_followed": not_followed,
    }


async def _follow_or_unfollow(group_ids: list[str], action: str) -> dict:
    """Follow or unfollow one or more companies via the API."""
    results = []
    errors = []
    for group_id in group_ids:
        try:
            await api_post(
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
# Unified MCP tool — company discovery (search, jobs, slug)
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_company(
    action: str = "search",
    keyword: Optional[str] = None,
    group_id: Optional[str] = None,
    group_ids: Optional[list[str]] = None,
    page: int = 1,
    limit: int = 20,
    # research params
    include_jobs: bool = True,
    include_reviews: bool = True,
    include_interviews: bool = True,
    jobs_limit: int = 5,
    timeout_seconds: int = 120,
) -> dict:
    """Unified company discovery — search companies, browse jobs, get AmbitionBox slug, follow/unfollow, or research a company.

    Actions:
      - "search": Search companies by name/industry (requires keyword)
      - "jobs": Get job listings for a specific company (requires group_id)
      - "slug": Convert group_id to AmbitionBox company slug (requires group_id)
      - "research": Comprehensive company research — jobs, salary, reviews, interviews (requires keyword)
      - "follow_status": Check follow status for companies (requires group_id or group_ids)
      - "follow": Follow companies (requires group_id or group_ids)
      - "unfollow": Unfollow companies (requires group_id or group_ids)

    Args:
        action: "search" | "jobs" | "slug" | "research" | "follow_status" | "follow" | "unfollow"
        keyword: Company name or industry keyword (required for search, research)
        group_id: Company group ID from search results (required for jobs, slug; also works for follow actions)
        group_ids: List of company group IDs for batch follow actions
        page: Page number for pagination (default 1, used by search/jobs)
        limit: Max results per page (default 20, used by search/jobs)
        include_jobs: Include open job listings (default True, research only)
        include_reviews: Include AmbitionBox salary/review data (default True, research only)
        include_interviews: Include AmbitionBox interview experiences (default True, research only)
        jobs_limit: Max jobs to include (default 5, research only)
        timeout_seconds: Max seconds before timeout (default 120, research only)

    Returns:
        - search: {status, keyword, page, total, count, companies: [...]}
        - jobs: {status, group_id, page, total, count, jobs: [...]}
        - slug: {status, group_id, company_slug, company_name}
        - research: {status, company_name, slug, jobs, salary, reviews, interviews, errors?}
        - follow_status: {status, followed: [...ids...], not_followed: [...ids...]}
        - follow/unfollow: {status, action, followed/unfollowed: [...ids...], errors?}
        - {status: "error", message} on failure
    """
    # ── search ─────────────────────────────────────────────────────────
    if action == "search":
        if not keyword:
            return {"status": "error", "message": "keyword is required for action='search'.", "error_code": "VALIDATION_ERROR"}
        try:
            return await _search_companies(keyword=keyword, page=page, limit=limit)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Company search failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── jobs ───────────────────────────────────────────────────────────
    elif action == "jobs":
        if not group_id:
            return {"status": "error", "message": "group_id is required for action='jobs'.", "error_code": "VALIDATION_ERROR"}
        try:
            return await _get_company_jobs(group_id=group_id, page=page, limit=limit)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Get company jobs failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── slug ───────────────────────────────────────────────────────────
    elif action == "slug":
        if not group_id:
            return {"status": "error", "message": "group_id is required for action='slug'.", "error_code": "VALIDATION_ERROR"}
        try:
            return await _get_company_slug(group_id=group_id)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Get company slug failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── research ────────────────────────────────────────────────────────
    elif action == "research":
        if not keyword:
            return {"status": "error", "message": "research requires keyword.", "error_code": "VALIDATION_ERROR"}
        from naukri_server.tools.research import _research_company
        try:
            return await _research_company(
                keyword=keyword, include_jobs=include_jobs,
                include_reviews=include_reviews, include_interviews=include_interviews,
                jobs_limit=jobs_limit, timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            return {"status": "error", "message": f"Research failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── follow_status / follow / unfollow ──────────────────────────────
    elif action in ("follow_status", "follow", "unfollow"):
        ids = group_ids or ([group_id] if group_id else [])
        if not ids:
            return {"status": "error", "message": "group_id or group_ids is required for follow actions.", "error_code": "VALIDATION_ERROR"}

        if action == "follow_status":
            try:
                return await _get_follow_status(ids)
            except NaukriAPIError as e:
                return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
            except Exception as e:
                return {"status": "error", "message": f"Check follow status failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}
        else:
            try:
                return await _follow_or_unfollow(ids, action)
            except NaukriAPIError as e:
                return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
            except Exception as e:
                return {"status": "error", "message": f"Company {action} failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── unknown action ─────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: search, jobs, slug, research, follow_status, follow, unfollow", "error_code": "VALIDATION_ERROR"}


# ---------------------------------------------------------------------------
# Legacy follow tool — kept as private helper, no longer registered as MCP tool
# ---------------------------------------------------------------------------

async def _company_follow(
    action: str,
    group_ids: list[str] = [],
) -> dict:
    """Unified company follow management — check status, follow, or unfollow.

    Requires: group_ids from naukri_company(action="search") results.

    Actions:
      - "status": Check follow status for given company IDs
      - "follow": Follow one or more companies
      - "unfollow": Unfollow one or more companies

    Args:
        action: "status" | "follow" | "unfollow"
        group_ids: List of company group IDs (from naukri_company search results)

    Returns:
        - status: {status, followed: [...ids...], not_followed: [...ids...]}
        - follow/unfollow: {status, action, followed/unfollowed: [...ids...], errors?}
        - {status: "error", message} on failure
    """
    # ── validate group_ids ────────────────────────────────────────────
    if not group_ids:
        return {"status": "error", "message": "group_ids is required and must not be empty.", "error_code": "VALIDATION_ERROR"}

    # ── status ────────────────────────────────────────────────────────
    if action == "status":
        try:
            return await _get_follow_status(group_ids)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Check follow status failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── follow / unfollow ─────────────────────────────────────────────
    elif action in ("follow", "unfollow"):
        try:
            return await _follow_or_unfollow(group_ids, action)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Company {action} failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── unknown action ────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: status, follow, unfollow", "error_code": "VALIDATION_ERROR"}


# Backward-compat alias (no longer an MCP tool)
naukri_company_follow = _company_follow
