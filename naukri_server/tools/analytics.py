"""Match analytics for job applications."""

import logging

from naukri_server.interfaces import api_client
from naukri_server.config import MATCH_ANALYTICS_API

logger = logging.getLogger(__name__)


async def _get_match_analytics(days: int = 7) -> dict:
    """Get match-score analytics for recent job applications — overall match distribution and per-field breakdowns.

    Args:
        days: Number of days of application history to analyze (default 7)

    Returns:
        - {status: "success", days, total_applies, complete_match, high_match, medium_match,
           low_match, field_breakdown, user_details}
        - {status: "error", message}
    """
    logger.info("Fetching match analytics for last %d days", days)
    if days < 1:
        return {"status": "error", "message": "days must be >= 1", "error_code": "VALIDATION_ERROR"}
    data = await api_client.get(MATCH_ANALYTICS_API, params={"days": str(days)})

    return {
        "status": "success",
        "days": days,
        "total_applies": data.get("totalApplies"),
        "complete_match": data.get("completeMatch"),
        "high_match": data.get("highMatch"),
        "medium_match": data.get("mediumMatch"),
        "low_match": data.get("lowMatch"),
        "field_breakdown": data.get("relevantFieldMatch"),
        "user_details": data.get("userDetails"),
    }
