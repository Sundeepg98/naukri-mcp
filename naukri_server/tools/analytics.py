"""Match analytics for job applications.

Business logic lives in naukri_server.services.insights_service.
This module re-exports the function so existing imports and test patch-paths remain valid.
"""

# Re-export from service layer — preserves import path and patch target
from naukri_server.services.insights_service import (  # noqa: F401
    get_match_analytics as _get_match_analytics,
)

# Also re-export api_client so existing patch targets
# like "naukri_server.tools.analytics.api_client" continue to work.
from naukri_server.interfaces import api_client  # noqa: F401
