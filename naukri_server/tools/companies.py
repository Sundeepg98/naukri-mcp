"""Company tools — search companies, browse company jobs, follow/unfollow."""

from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import SEARCH_API, COMPANY_SEARCH_API, COMPANY_FOLLOW_STATUS_API
from naukri_server.tools.search import _parse_job_list

_COMPANY_HEADERS = {"appid": "103"}


def _parse_company(group: dict) -> dict:
    """Parse a company from the search response."""
    ab = group.get("ambitionBoxData", {})
    if not isinstance(ab, dict):
        ab = {}
    return {
        "group_id": group.get("groupId"),
        "name": group.get("title") or group.get("name"),
        "type": group.get("type"),
        "industry": group.get("industry"),
        "size": group.get("employeeCount") or group.get("size"),
        "rating": ab.get("AggregateRating") or group.get("rating"),
        "review_count": ab.get("ReviewsCount") or group.get("reviewCount"),
        "logo_url": group.get("logoPath") or group.get("logo"),
        "tagline": group.get("tagline"),
    }


@mcp.tool()
async def naukri_search_companies(
    keyword: str,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """Search for companies on Naukri.com.

    Args:
        keyword: Company name or industry keyword (e.g., "Google", "fintech")
        page: Page number for pagination (default 1)
        limit: Max results per page (default 20)

    Returns:
        - {status: "success", keyword, page, total_found, count, companies: [{group_id, name, type, industry, size, rating, review_count, logo_url, tagline}]}
        - {status: "error", message}
    """
    try:
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

        return {
            "status": "success",
            "keyword": keyword,
            "page": page,
            "total_found": data.get("noOfGroups"),
            "count": len(companies),
            "companies": companies,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Company search failed: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_company_jobs(
    group_id: str,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """Get job listings from a specific company on Naukri.com.

    Use naukri_search_companies first to get the group_id.

    Args:
        group_id: Company group ID (from naukri_search_companies results)
        page: Page number for pagination (default 1)
        limit: Max jobs to return (default 20, max 50)

    Returns:
        - {status: "success", group_id, page, total_found, count, jobs: [{job_id, title, company, salary, location, experience, is_applied, posted_date, tags, url}]}
        - {status: "error", message}
    """
    try:
        data = await api_get(
            SEARCH_API,
            params={
                "groupId": group_id,
                "searchType": "groupidsearch",
                "pageNo": str(page),
                "noOfResults": str(limit),
            },
        )

        job_details = data.get("jobDetails", [])
        jobs = _parse_job_list(job_details, limit)

        return {
            "status": "success",
            "group_id": group_id,
            "page": page,
            "total_found": data.get("noOfJobs"),
            "count": len(jobs),
            "jobs": jobs,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get company jobs: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_follow_company(
    group_id: str,
    action: str = "status",
) -> dict:
    """Check follow status, follow, or unfollow a company on Naukri.com.

    Use naukri_search_companies first to get the group_id.

    Args:
        group_id: Company group ID (from naukri_search_companies results)
        action: One of "status" (check if followed), "follow", or "unfollow"

    Returns:
        - action="status": {status: "success", group_id, is_followed}
        - action="follow"/"unfollow": {status: "success", group_id, action: "followed"/"unfollowed"}
        - {status: "error", message}
    """
    try:
        if action == "status":
            data = await api_get(
                COMPANY_FOLLOW_STATUS_API,
                params={"query": group_id},
            )
            followed = data.get("followedGroups", [])
            is_followed = any(
                str(g.get("id", g) if isinstance(g, dict) else g) == str(group_id)
                for g in followed
            )
            return {
                "status": "success",
                "group_id": group_id,
                "is_followed": is_followed,
            }

        elif action in ("follow", "unfollow"):
            await api_post(
                COMPANY_FOLLOW_STATUS_API,
                body={"groupId": group_id, "action": action},
            )
            return {
                "status": "success",
                "group_id": group_id,
                "action": f"{action}ed",
            }

        else:
            return {
                "status": "error",
                "message": f"Invalid action '{action}'. Use 'status', 'follow', or 'unfollow'.",
            }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Company follow failed: {type(e).__name__}: {e}"}
