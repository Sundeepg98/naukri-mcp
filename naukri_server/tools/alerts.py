"""Job alert tools — list, create, and manage saved search alerts on Naukri."""

from typing import Optional

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import logger, JOB_ALERT_API, JOB_ALERTS_LIST_API


@mcp.tool()
async def naukri_get_job_alerts() -> dict:
    """List all job alerts (saved searches) on your Naukri account.

    Returns both SSA (Save Search Alert) and CJA (Custom Job Alert) types.

    Returns:
        - {status: "success", count, alerts: [{alert_id, name, keywords, location,
          experience, min_ctc, max_ctc, alert_type, function_area_id, role_id,
          industry_type_id}]}
        - {status: "error", message}
    """
    try:
        data = await api_get(JOB_ALERTS_LIST_API)
        raw_list = data.get("list", [])
        alerts = []
        for alert in raw_list:
            alerts.append({
                "alert_id": alert.get("alertId"),
                "name": alert.get("name"),
                "keywords": alert.get("keywords"),
                "location": alert.get("location"),
                "experience": alert.get("experience"),
                "min_ctc": alert.get("minCTC"),
                "max_ctc": alert.get("maxCTC"),
                "alert_type": alert.get("alertType"),  # "ssa" or "cja"
                "function_area_id": alert.get("functionAreaId"),
                "role_id": alert.get("roleId"),
                "industry_type_id": alert.get("industryTypeId"),
            })
        return {
            "status": "success",
            "count": len(alerts),
            "alerts": alerts,
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to list job alerts: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_create_job_alert(
    name: str,
    keyword: str,
    location: Optional[str] = None,
    experience: Optional[int] = None,
    min_ctc: Optional[int] = None,
    function_area_id: Optional[str] = None,
    role_id: Optional[str] = None,
    industry_type_id: Optional[str] = None,
) -> dict:
    """Create a job alert (saved search) on Naukri. You'll receive email
    notifications when new jobs match your criteria.

    Args:
        name: Alert name (e.g., "Python Remote Jobs")
        keyword: Search keywords (e.g., "python developer")
        location: City filter (e.g., "Bangalore", "Remote")
        experience: Years of experience filter (e.g., 5)
        min_ctc: Minimum CTC in lakhs (e.g., 15 for 15 LPA)
        function_area_id: Functional area ID (from Naukri taxonomy)
        role_id: Role ID (from Naukri taxonomy)
        industry_type_id: Industry type ID (from Naukri taxonomy)

    Returns:
        - {status: "success", alert_name, message}
        - {status: "duplicate", alert_name, message, replace_url}
        - {status: "error", message}
    """
    try:
        body = {"name": name, "keyword": keyword}
        if location is not None:
            body["location"] = location
        if experience is not None:
            body["experience"] = str(experience)
        if min_ctc is not None:
            body["minCTC"] = str(min_ctc * 100000)
        if function_area_id is not None:
            body["functionAreaId"] = function_area_id
        if role_id is not None:
            body["roleId"] = role_id
        if industry_type_id is not None:
            body["industryTypeId"] = industry_type_id

        await api_post(JOB_ALERT_API, body)
        return {
            "status": "success",
            "alert_name": name,
            "message": f"Job alert '{name}' created for '{keyword}'.",
        }
    except NaukriAPIError as e:
        if "replacealert" in str(e).lower() or "already" in str(e).lower():
            return {
                "status": "duplicate",
                "alert_name": name,
                "message": f"A similar alert may already exist: {e}",
            }
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to create job alert: {type(e).__name__}: {e}"}
