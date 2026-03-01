"""Early access tools — pre-posted roles from top companies."""

from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import EARLY_ACCESS_API, APPLY_WORKFLOW_API


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _list_early_access_roles(page: int = 1, limit: int = 20) -> dict:
    """Fetch early access roles from the API and return structured result."""
    limit = min(limit, 50)
    if page < 1:
        return {"status": "error", "message": "page must be >= 1", "error_code": "VALIDATION_ERROR"}
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

    total = data.get("noOfJobs", data.get("totalJobs", len(roles)))
    return {
        "status": "success",
        "total": total,
        "count": len(roles),
        "page": page,
        "has_more": (page * limit) < total,
        "roles": roles,
    }


async def _share_interest(job_id: str) -> dict:
    """Express interest in an early access role via the apply workflow API."""
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
        return {"status": "error", "message": "No response from apply endpoint.", "error_code": "API_ERROR"}

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
        return {"status": "error", "message": job_result.get("message", f"Failed with status {job_result.get('status')}"), "error_code": "API_ERROR"}


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_early_access(
    action: str = "list",
    job_id: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> dict:
    """Unified early access management — browse pre-posted roles and share interest.

    Early access roles are jobs from top companies before they go live.
    Sharing interest is instant — no screening questions.

    Actions:
      - "list": Get early access roles (use page/limit for pagination)
      - "share": Express interest in a role (requires job_id)

    Args:
        action: "list" | "share"
        job_id: Required for share — the early access role job ID
        page: Page number for list action (default 1)
        limit: Max roles for list action (default 20)

    Returns:
        - list: {status, total, count, page, has_more, roles: [{job_id, title, company_hint, location, experience, salary, job_type, tags, url}]}
        - share: {status, job_id, message, quota}
        - {status: "error", message} on failure
    """
    # ── list ───────────────────────────────────────────────────────────
    if action == "list":
        try:
            return await _list_early_access_roles(page=page, limit=limit)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Get early access roles failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── share ──────────────────────────────────────────────────────────
    elif action == "share":
        if not job_id:
            return {"status": "error", "message": "share requires job_id.", "error_code": "VALIDATION_ERROR"}
        try:
            return await _share_interest(job_id)
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Share interest failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # ── unknown action ─────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: list, share", "error_code": "VALIDATION_ERROR"}
