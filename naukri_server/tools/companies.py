"""Company tools — search companies, browse company jobs, follow/unfollow."""

from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.browser import browser, page_goto, page_intercept_json
from naukri_server.config import NAUKRI_BASE, COMPANY_SEARCH_API, COMPANY_FOLLOW_STATUS_API, logger
from naukri_server.tools.job_parsing import _parse_job_list
from naukri_server.validation import validate_company_list, validate_job_list

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


@mcp.tool()
@api_tool("Company search")
async def naukri_search_companies(
    keyword: str,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """Search for companies on Naukri.com by name or industry.

    Args:
        keyword: Company name or industry keyword (e.g., "Google", "fintech")
        page: Page number for pagination (default 1)
        limit: Max results per page (default 20)

    Returns:
        - {status: "success", keyword, page, total, count, companies: [{group_id, name, type, industry, size, ownership, rating, review_count, logo_url, jobs_url}]}
        - {status: "error", message}
    """
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


@mcp.tool()
async def naukri_get_company_jobs(
    group_id: str,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """Get job listings from a specific company on Naukri.com.

    Requires: group_id from naukri_search_companies results.

    Args:
        group_id: Company group ID (from naukri_search_companies results)
        page: Page number for pagination (default 1)
        limit: Max jobs to return (default 20, max 50)

    Returns:
        - {status: "success", group_id, page, total, count, jobs: [{job_id, title, company, salary, location, experience, is_applied, posted_date, tags, url}]}
        - {status: "error", message}
    """
    page_no = page  # save before shadowing by page_pool
    async with browser.page_pool.acquire() as page:
        try:
            # Navigate to company jobs page and intercept the search API response
            page_url = f"{NAUKRI_BASE}/jobs-in-{group_id}?groupId={group_id}&searchType=groupidsearch"
            if page_no > 1:
                page_url += f"&pageNo={page_no}"

            data = await page_intercept_json(page, page_url, url_pattern="/jobapi/v3/search")
            if not data:
                return {"status": "error", "message": "Search API response not captured for company jobs."}

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
            return {"status": "error", "message": f"Failed to get company jobs: {type(e).__name__}: {e}"}


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
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_company_follow(
    action: str,
    group_ids: list[str] = [],
) -> dict:
    """Unified company follow management — check status, follow, or unfollow.

    Requires: group_ids from naukri_search_companies results.

    Actions:
      - "status": Check follow status for given company IDs
      - "follow": Follow one or more companies
      - "unfollow": Unfollow one or more companies

    Args:
        action: "status" | "follow" | "unfollow"
        group_ids: List of company group IDs (from naukri_search_companies results)

    Returns:
        - status: {status, followed: [...ids...], not_followed: [...ids...]}
        - follow/unfollow: {status, action, followed/unfollowed: [...ids...], errors?}
        - {status: "error", message} on failure
    """
    # ── validate group_ids ────────────────────────────────────────────
    if not group_ids:
        return {"status": "error", "message": "group_ids is required and must not be empty."}

    # ── status ────────────────────────────────────────────────────────
    if action == "status":
        try:
            return await _get_follow_status(group_ids)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status}
        except Exception as e:
            return {"status": "error", "message": f"Check follow status failed: {type(e).__name__}: {e}"}

    # ── follow / unfollow ─────────────────────────────────────────────
    elif action in ("follow", "unfollow"):
        try:
            return await _follow_or_unfollow(group_ids, action)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status}
        except Exception as e:
            return {"status": "error", "message": f"Company {action} failed: {type(e).__name__}: {e}"}

    # ── unknown action ────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: status, follow, unfollow"}
