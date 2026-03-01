"""Subscription tools — Naukri 360 Pro status, promo codes, and login verification."""

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError, api_tool
from naukri_server.config import N360_CONFIG_API


@mcp.tool()
@api_tool("Get subscription status")
async def naukri_get_subscription_status() -> dict:
    """Get your Naukri 360 Pro subscription status — paid/free, active subscriptions,
    available promo codes, and feature eligibility.

    Returns:
        - {status: "success", is_paid, has_active_subscription, is_jobseeker_agent_eligible, promo_code, promo_discount, services: {...}}
        - {status: "error", message}
    """
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
