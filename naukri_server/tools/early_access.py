"""Early access tools — pre-posted roles from top companies."""

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError, api_tool
from naukri_server.config import EARLY_ACCESS_API


@mcp.tool()
@api_tool("Get early access roles")
async def naukri_get_early_access_roles(page: int = 1, limit: int = 20) -> dict:
    """Get early access roles — pre-posted jobs from top companies before they go live.

    These are jobs where you can "share interest" before formal applications open.

    Args:
        page: Page number (default 1)
        limit: Max roles to return (default 20)

    Returns:
        - {status: "success", page, total, count, roles: [{job_id, title, company_hint, location, experience, tags, url}]}
        - {status: "error", message}
    """
    data = await api_get(
        EARLY_ACCESS_API,
        params={"pageNo": str(page), "noOfResults": str(limit)},
    )

    jobs_raw = data.get("jobDetails", data.get("jobs", []))
    roles = []
    for j in jobs_raw:
        if not isinstance(j, dict):
            continue
        roles.append({
            "job_id": j.get("jobId", ""),
            "title": j.get("title", ""),
            "company_hint": j.get("companyName", j.get("companyHint", "")),
            "location": j.get("placeholders", [{}])[0].get("label", "") if j.get("placeholders") else j.get("location", ""),
            "experience": j.get("experienceText", j.get("experience", "")),
            "salary": j.get("salaryText", j.get("salary", "")),
            "job_type": j.get("jobType", ""),
            "tags": [t.get("value", t) if isinstance(t, dict) else str(t) for t in j.get("tags", j.get("tagsAndSkills", "").split(","))[:5]] if j.get("tags") or j.get("tagsAndSkills") else [],
            "url": j.get("jdURL", j.get("url", "")),
        })

    return {
        "status": "success",
        "page": page,
        "total": data.get("noOfJobs", data.get("totalJobs", len(roles))),
        "count": len(roles),
        "roles": roles,
    }
