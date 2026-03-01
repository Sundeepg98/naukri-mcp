"""Early access tools — pre-posted roles from top companies."""

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError, api_tool
from naukri_server.config import EARLY_ACCESS_API, APPLY_WORKFLOW_API


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


@mcp.tool()
@api_tool("Share interest")
async def naukri_share_interest(job_id: str) -> dict:
    """Express interest in an early access (pre-posted) role.

    Early access roles are jobs from top companies before they go live.
    Sharing interest is instant — no screening questions.
    Get job_ids from naukri_get_early_access_roles.

    Args:
        job_id: Early access role job ID

    Returns:
        - {status: "success", job_id, message, quota}
        - {status: "error", message}
    """
    data = await api_post(
        APPLY_WORKFLOW_API,
        body={
            "strJobsarr": [str(job_id)],
            "applySrc": "----F-0-1---",
            "applytype": "single",
        },
    )

    jobs = data.get("jobs", [])
    if not jobs:
        return {"status": "error", "message": "No response from apply endpoint."}

    job_result = jobs[0]
    if job_result.get("status") == 200:
        quota = data.get("quotaDetails", {})
        return {
            "status": "success",
            "job_id": job_id,
            "message": job_result.get("message", "Interest shared successfully."),
            "quota": {
                "daily_applied": quota.get("dailyApplied", 0),
                "daily_quota": quota.get("dailyQuota", 50),
            },
        }
    else:
        return {"status": "error", "message": job_result.get("message", f"Failed with status {job_result.get('status')}")}
