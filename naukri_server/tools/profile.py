"""Profile tools — get, update, audit, boost, dashboard, and targeting.

Browser mutations live in profile_update.py; DFP targeting in profile_targeting.py.
This module owns the MCP dispatcher, ISP validation, and re-exports from profile_service.
"""

import asyncio
from typing import Optional

from naukri_server import mcp
from naukri_server.browser import browser_retry
from naukri_server.interfaces import api_client  # noqa: F401 — tests patch this path
from naukri_server.error_handler import handle_tool_action
from naukri_server.config import BROWSER_OPERATION_TIMEOUT
from naukri_server.models import validate_action_params
from naukri_server.utils import TtlCache

# ---------------------------------------------------------------------------
# Re-export business logic from profile_service (backward compat)
# ---------------------------------------------------------------------------
from naukri_server.services.profile_service import (  # noqa: F401
    _get_profile,
    _fetch_raw_profile,
    get_cached_profile,
    _get_dashboard,
    _fetch_raw_dashboard,
    get_cached_dashboard,
    _audit_profile,
    _profile_ttl_cache,
    _dashboard_ttl_cache,
)

# Backward-compat alias — tests import _TtlCache from here
_TtlCache = TtlCache


# ---------------------------------------------------------------------------
# ISP param validation
# ---------------------------------------------------------------------------

_VALID_PARAMS_PER_ACTION = {
    "get": set(),
    "update": {"fields", "notice_period", "expected_ctc", "current_ctc"},
    "audit": set(),
    "boost": {"randomize"},
    "dashboard": set(),
    "targeting": set(),
}


# ---------------------------------------------------------------------------
# Profile registry — maps action to handler(kwargs) -> awaitable[dict]
# ---------------------------------------------------------------------------

async def _do_update(**kw) -> dict:
    """Wrap browser_retry + wait_for for profile update."""
    return await asyncio.wait_for(
        browser_retry(
            lambda: _update_profile(
                fields=kw.get("fields") or {},
                notice_period=kw.get("notice_period"),
                expected_ctc=kw.get("expected_ctc"),
                current_ctc=kw.get("current_ctc"),
            ),
            description="profile update",
        ),
        timeout=BROWSER_OPERATION_TIMEOUT,
    )


async def _do_boost(**kw) -> dict:
    """Wrap browser_retry + wait_for for profile boost."""
    return await asyncio.wait_for(
        browser_retry(
            lambda: _boost_visibility(randomize=kw.get("randomize", False)),
            description="profile boost",
        ),
        timeout=BROWSER_OPERATION_TIMEOUT,
    )


_PROFILE_REGISTRY: dict[str, callable] = {
    "get": lambda **kw: _get_profile(),
    "update": _do_update,
    "audit": lambda **kw: _audit_profile(),
    "boost": _do_boost,
    "dashboard": lambda **kw: _get_dashboard(),
    "targeting": lambda **kw: _do_targeting(),
}


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_profile(
    action: str = "get",
    fields: Optional[dict] = None,
    notice_period: Optional[str] = None,
    expected_ctc: Optional[float] = None,
    current_ctc: Optional[float] = None,
    randomize: bool = False,
) -> dict:
    """Unified profile management — get, update, audit, boost, dashboard, or targeting.

    Actions:
      - "get": Fetch full profile (skills, employment, education, CTC, etc.)
      - "update": Update profile fields via browser UI (requires fields dict).
                 Some fields may be silently ignored by the API.
      - "audit": Audit profile completeness and get improvement suggestions
      - "boost": Re-save headline to appear as 'recently active' in recruiter searches
      - "dashboard": Get dashboard data (notifications, profile completeness, activity)
      - "targeting": How Naukri's ad system sees your profile — 35 targeting fields, completeness gaps

    Args:
        action: "get" | "update" | "audit" | "boost" | "dashboard" | "targeting"
        fields: Required for update — dict of fields to change. Supported keys:
            resumeHeadline, keySkills, noticePeriod, expectedCtc, currentCtc
        notice_period: Shorthand for update — "Serving Notice Period", "15 Days or less",
            "1 Month", "2 Months", "3 Months", "More than 3 Months"
        expected_ctc: Shorthand for update — expected CTC in lakhs (e.g., 15)
        current_ctc: Shorthand for update — current CTC in lakhs (e.g., 12)
        randomize: For boost only — if True, wait random 0-300s before refreshing

    Returns:
        - get: {status, name, current_ctc, expected_ctc, skills_with_experience, employment, education, ...}
        - update: {status: "updated", updated_fields, method, api_confirmed, message}
        - audit: {status, completeness_pct, grade, strengths, gaps, tips}
        - boost: {status: "refreshed", method, message}
        - dashboard: {status, profile_views, recruiter_activity_date, ctc_lpa, experience_years, ...}
        - targeting: {status, profile, completeness_gaps, gap_count}
        - {status: "error", message} on failure
    """
    # ── ISP: warn about params irrelevant to chosen action ─────────────
    _provided = {
        "fields": fields, "notice_period": notice_period,
        "expected_ctc": expected_ctc, "current_ctc": current_ctc,
        "randomize": randomize if randomize else None,
    }
    _unused = validate_action_params(action, _provided, _VALID_PARAMS_PER_ACTION)

    def _attach_unused(result: dict) -> dict:
        if _unused and isinstance(result, dict):
            result["unused_params"] = _unused
        return result

    # ── Registry lookup ─────────────────────────────────────────────────
    handler = _PROFILE_REGISTRY.get(action)
    if handler:
        kw = {
            "fields": fields, "notice_period": notice_period,
            "expected_ctc": expected_ctc, "current_ctc": current_ctc,
            "randomize": randomize,
        }
        return _attach_unused(await handle_tool_action(lambda: handler(**kw), f"profile.{action}"))

    return {
        "status": "error",
        "message": f"Unknown action '{action}'. Use: {', '.join(_PROFILE_REGISTRY)}",
        "error_code": "VALIDATION_ERROR",
    }


# ---------------------------------------------------------------------------
# Backward compatibility — re-export symbols that moved to new modules
# ---------------------------------------------------------------------------
from naukri_server.tools.profile_update import (  # noqa: E402, F401
    _update_profile, _boost_visibility,
    _SAVE_MODAL_JS, _fill_ctc_input, _click_save_modal,
    UPDATABLE_FIELDS, BROWSER_SUPPORTED_FIELDS,
    _update_headline, _update_key_skills, _update_notice_period,
    _update_current_ctc, _update_expected_ctc,
    _STANDALONE_HANDLERS, _CAREER_FIELD_HANDLERS, _FIELD_HANDLERS,
    _open_career_profile_modal, _ctc_find_js,
)
from naukri_server.tools.profile_targeting import _do_targeting  # noqa: E402, F401
