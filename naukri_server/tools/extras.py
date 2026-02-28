"""Extra tools — notification count, fraud reporting, and email verification."""

from naukri_server import mcp
from naukri_server.api import api_get, api_post, NaukriAPIError
from naukri_server.config import NOTIFICATION_COUNT_API, REPORT_FRAUD_API, MAIL_VERIFICATION_API


@mcp.tool()
async def naukri_get_notification_count() -> dict:
    """Get the count of unread notifications. Lightweight alternative to fetching the full notification feed.

    Returns:
        - {status: "success", count: N}
        - {status: "error", message}
    """
    try:
        data = await api_get(NOTIFICATION_COUNT_API)
        return {
            "status": "success",
            "count": data.get("count", 0),
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get notification count: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_report_fraud_job(job_id: str, reason: str) -> dict:
    """Report a job posting as fraudulent or suspicious on Naukri.

    Args:
        job_id: The job ID to report
        reason: Description of why this job seems fraudulent

    Returns:
        - {status: "success", job_id, message}
        - {status: "error", message}
    """
    try:
        data = await api_post(
            REPORT_FRAUD_API,
            body={"jobId": job_id, "reason": reason},
        )
        return {
            "status": "success",
            "job_id": job_id,
            "message": "Job reported as fraudulent.",
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to report job: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_check_email_verification() -> dict:
    """Check if your Naukri account email is verified.

    Returns:
        - {status: "success", is_verified, email, ...}
        - {status: "error", message}
    """
    try:
        data = await api_get(MAIL_VERIFICATION_API)
        return {
            "status": "success",
            "is_verified": data.get("isVerified", data.get("verified", None)),
            "email": data.get("email", data.get("emailId", "")),
            "data": {k: v for k, v in data.items() if k not in ("isVerified", "verified", "email", "emailId")},
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to check email verification: {type(e).__name__}: {e}"}
