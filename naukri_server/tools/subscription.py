"""Subscription tools — Naukri 360 Pro status, promo codes, and login verification."""

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.config import N360_CONFIG_API, LOGIN_STATUS_API


@mcp.tool()
async def naukri_get_subscription_status() -> dict:
    """Get your Naukri 360 Pro subscription status — paid/free, active subscriptions,
    available promo codes, and feature eligibility.

    Returns:
        - {status: "success", is_paid, has_active_subscription, is_jobseeker_agent_eligible, promo_code, promo_discount, services: {...}}
        - {status: "error", message}
    """
    try:
        data = await api_get(N360_CONFIG_API)

        # Extract user state
        user_state = data.get("user", data.get("userState", {}))
        if not isinstance(user_state, dict):
            user_state = {}

        # Extract promo info
        promo = data.get("promo", {})
        if not isinstance(promo, dict):
            promo = {}

        # Extract service details
        serv_details = data.get("servDetails", data.get("services", {}))

        return {
            "status": "success",
            "is_paid": user_state.get("isPaid", False),
            "has_active_subscription": user_state.get("hasActiveSubscription", False),
            "is_jobseeker_agent_eligible": user_state.get("isJobseekerAgentEligible", False),
            "promo_code": promo.get("code") or promo.get("promoCode", ""),
            "promo_discount": promo.get("discount") or promo.get("percentOff", ""),
            "services": serv_details if isinstance(serv_details, dict) else {},
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get subscription status: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_login_status() -> dict:
    """Quick check if your Naukri session is still active and authenticated.

    Uses the activity level endpoint which reliably returns login state.

    Returns:
        - {status: "success", logged_in: true/false}
        - {status: "error", message}
    """
    try:
        from naukri_server.config import ACTIVITY_LEVEL_API
        data = await api_get(ACTIVITY_LEVEL_API)
        return {
            "status": "success",
            "logged_in": data.get("loggedInStatus", False),
        }
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to check login status: {type(e).__name__}: {e}"}
