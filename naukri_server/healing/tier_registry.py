"""Tier classification for the Pragmatic Auto-Fix healing system.

Every API endpoint constant in naukri_server.config is classified into one of
three tiers, which decides what the healer is allowed to do when drift is
detected:

  T1  Safe to auto-fix.  Read-only, low-impact endpoints (notifications, mock
      interview, AmbitionBox scrapes, taxonomy, ad-targeting). The healer
      patches the parser/config and commits to git without further checks.

  T2  Auto-fix WITH snapshot+revert protection.  Read endpoints that feed
      *agent decisions* (search, recommendations, dashboard, profile, inbox,
      applications-list, etc.). A bad fix on T2 could silently change which
      jobs the agent applies to, so every T2 fix is reverted unless the
      api_validator confirms the endpoint is healthy ~10 minutes later.

  T3  NEVER auto-applied.  Anything irreversible: the apply workflow,
      mark-interested, save/unsave, alert CRUD, settings, blocked-companies,
      photo upload, fraud reports, follow-status POSTs. Drift on a T3
      endpoint produces a HealingProposal notification (high priority) with
      a unified diff for the user to review.

This module is pure data — no I/O, no events, no git. It is consumed by
healing/router.py to dispatch drift events to the right tier handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Tier labels — exported so callers can do `if tier == TIER_T3: ...`
# ---------------------------------------------------------------------------

TIER_T1 = "T1"
TIER_T2 = "T2"
TIER_T3 = "T3"

ALL_TIERS = (TIER_T1, TIER_T2, TIER_T3)


@dataclass(frozen=True)
class TierEntry:
    """Routing record for a single endpoint constant.

    Attributes:
        tier: TIER_T1 | TIER_T2 | TIER_T3
        parser_module: dotted path to the module that owns the parser whose
            FIELD_ALIASES dict (or similar) the T1/T2 healer would patch.
            None if drift is expected to manifest as a URL change rather
            than a field-shape change (rare).
        notes: short human-readable rationale (printed in healing notifications).
    """

    tier: str
    parser_module: str | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# T1 endpoints (15) — auto-fix without verification
# ---------------------------------------------------------------------------
# Notifications, mock interview, AmbitionBox scrapes, taxonomy/targeting/CCS,
# resume builder GETs, GET-only follow-status. None of these influence agent
# apply decisions; a bad fix is at worst noisy.

_T1_ENTRIES: dict[str, TierEntry] = {
    "NOTIFICATION_FEED_API": TierEntry(
        TIER_T1, "naukri_server.tools.notifications", "Notification feed — display only"
    ),
    "NOTIFICATION_COUNT_API": TierEntry(
        TIER_T1, "naukri_server.tools.notifications", "Unread count — display only"
    ),
    "MOCK_INTERVIEW_TOPICS_API": TierEntry(
        TIER_T1, "naukri_server.tools.mock_interview", "Mock interview topic list"
    ),
    "MOCK_INTERVIEW_HISTORY_API": TierEntry(
        TIER_T1, "naukri_server.tools.mock_interview", "Past mock interview history"
    ),
    "MOCK_INTERVIEW_ROLE_API": TierEntry(
        TIER_T1, "naukri_server.tools.mock_interview", "Role-based mock interview metadata"
    ),
    "MOCK_INTERVIEW_OTHER_TOPICS_API": TierEntry(
        TIER_T1, "naukri_server.tools.mock_interview", "Additional mock interview topics"
    ),
    "MOCK_INTERVIEW_QUESTION_API": TierEntry(
        TIER_T1, "naukri_server.tools.mock_interview", "Mock interview question fetch"
    ),
    "RESUME_DOWNLOAD_API": TierEntry(
        TIER_T1, "naukri_server.tools.resume_photo", "Resume binary download — no parsing risk"
    ),
    "RESUME_BUILDER_CONFIG_API": TierEntry(
        TIER_T1, "naukri_server.tools.resume_builder", "Resume builder config (templates)"
    ),
    "RESUME_BUILDER_STATUS_API": TierEntry(
        TIER_T1, "naukri_server.tools.resume_builder", "Resume builder feature status"
    ),
    # AmbitionBox bridge — 8 endpoints, all under tools.ambitionbox_rest
    "AB_SALARY_API": TierEntry(TIER_T1, "naukri_server.tools.ambitionbox_rest", "AmbitionBox salary REST"),
    "AB_BENEFITS_API": TierEntry(TIER_T1, "naukri_server.tools.ambitionbox_rest", "AmbitionBox benefits REST"),
    "AB_REVIEW_DIST_API": TierEntry(TIER_T1, "naukri_server.tools.ambitionbox_rest", "AmbitionBox review distribution"),
    "AB_REVIEW_FILTERS_API": TierEntry(TIER_T1, "naukri_server.tools.ambitionbox_rest", "AmbitionBox review filters"),
    "AB_INTERVIEW_QS_API": TierEntry(TIER_T1, "naukri_server.tools.ambitionbox_rest", "AmbitionBox top interview questions"),
    "AB_COMPANY_COMPARE_API": TierEntry(TIER_T1, "naukri_server.tools.ambitionbox_rest", "AmbitionBox company comparison"),
    "AB_COMPANY_LOCATIONS_API": TierEntry(TIER_T1, "naukri_server.tools.ambitionbox_rest", "AmbitionBox company locations"),
    "AB_INSIGHTS_APPLIED_API": TierEntry(TIER_T1, "naukri_server.tools.ambitionbox_rest", "AmbitionBox applied-jobs insights"),
    "ENTITY_TAXONOMY_API": TierEntry(
        TIER_T1, "naukri_server.tools.insights", "Job taxonomy (departments/roles)"
    ),
    "DFP_PROFILE_API": TierEntry(
        TIER_T1, "naukri_server.tools.profile_targeting", "Ad-targeting DFP profile"
    ),
    "CCS_PAGE_API": TierEntry(
        TIER_T1, "naukri_server.tools.insights", "CCS widget page state"
    ),
    "N360_CONFIG_API": TierEntry(
        TIER_T1, "naukri_server.tools.subscription", "Naukri 360 config / subscription"
    ),
    "ALERT_DETAIL_API": TierEntry(
        TIER_T1, "naukri_server.tools.alerts", "Single-alert detail GET"
    ),
    "COMPANY_FOLLOW_STATUS_API": TierEntry(
        TIER_T1, "naukri_server.tools.companies",
        "GET-only follow-status check (POST variant is T3)"
    ),
}

# ---------------------------------------------------------------------------
# T2 endpoints (22) — auto-fix WITH snapshot+revert verification
# ---------------------------------------------------------------------------
# Anything that feeds agent search/score/match decisions. A bad fix here would
# silently change which jobs surface or score; we revert unless the validator
# confirms the endpoint is still healthy after ~10 minutes.

_T2_ENTRIES: dict[str, TierEntry] = {
    "SEARCH_API": TierEntry(TIER_T2, "naukri_server.services.search_service", "Job search — feeds agent decisions"),
    "RECOMMENDED_JOBS_API": TierEntry(TIER_T2, "naukri_server.services.search_service", "Recommendations — feeds agent decisions"),
    "JOB_DETAIL_API": TierEntry(TIER_T2, "naukri_server.tools.jobs", "Job detail v3 — feeds fit scoring"),
    "JOB_DETAIL_V1_API": TierEntry(TIER_T2, "naukri_server.tools.jobs", "Job detail v1 — walk-in / contact / metrics"),
    "JOB_MATCH_SCORE_API": TierEntry(TIER_T2, "naukri_server.tools.jobs", "Naukri's own match score — feeds fit"),
    "SIMILAR_JOBS_API": TierEntry(TIER_T2, "naukri_server.tools.jobs", "Similar-jobs widget — feeds agent expansion"),
    "MATCH_ANALYTICS_API": TierEntry(TIER_T2, "naukri_server.services.application_service", "Apply-match analytics"),
    "APPLY_MATCH_SCORE_API": TierEntry(TIER_T2, "naukri_server.services.application_service", "Apply-match score (alias of MATCH_ANALYTICS_API)"),
    "APPLIED_JOBS_API": TierEntry(TIER_T2, "naukri_server.services.sync_service", "Applied-jobs history — feeds agent dedup"),
    "SAVED_JOBS_API": TierEntry(TIER_T2, "naukri_server.services.sync_service", "Saved jobs — feeds agent candidate pool"),
    "APPLICATION_STATUS_API": TierEntry(TIER_T2, "naukri_server.services.application_service", "Application status detail"),
    "INBOX_API": TierEntry(TIER_T2, "naukri_server.tools.inbox", "Inbox POST endpoint"),
    "MESSAGE_API": TierEntry(TIER_T2, "naukri_server.tools.inbox", "Single-message read"),
    "INBOX_REST_API": TierEntry(TIER_T2, "naukri_server.tools.inbox", "Inbox GET endpoint (Tier 21)"),
    "DASHBOARD_API": TierEntry(TIER_T2, "naukri_server.services.profile_service", "Dashboard — feeds agent profile-quality signal"),
    "JOB_ALERTS_LIST_API": TierEntry(TIER_T2, "naukri_server.tools.alerts", "Job alerts list — read"),
    "PROFILE_API": TierEntry(TIER_T2, "naukri_server.services.profile_service", "Profile read — feeds fit scoring"),
    "FULLPROFILES_API": TierEntry(TIER_T2, "naukri_server.services.profile_service", "Full profile read"),
    "COMPANY_SEARCH_API": TierEntry(TIER_T2, "naukri_server.services.company_service", "Company search"),
    "BULK_JOBS_API": TierEntry(TIER_T2, "naukri_server.tools.jobs", "Bulk job fetch — feeds batch flows"),
    "EARLY_ACCESS_API": TierEntry(TIER_T2, "naukri_server.tools.early_access", "Early-access roles — feeds candidate pool"),
    "RECOMMEND_NOTIFY_API": TierEntry(TIER_T2, "naukri_server.tools.notifications", "Notification dashboard summary"),
    "SEARCH_IMPRESSIONS_API": TierEntry(TIER_T2, "naukri_server.tools.performance", "Search impressions / recruiter visibility"),
    "RECRUITER_ACTIVITY_API": TierEntry(TIER_T2, "naukri_server.tools.performance", "Recruiter activity log"),
    "ACTIVITY_LEVEL_API": TierEntry(TIER_T2, "naukri_server.tools.performance", "Profile activity level"),
}

# ---------------------------------------------------------------------------
# T3 endpoints (11) — NEVER auto-applied; only HealingProposal notifications
# ---------------------------------------------------------------------------
# Anything that mutates server-side state in a way that can't be cheaply
# undone: applies, mark-interested, save/unsave, alerts CRUD, settings,
# blocked companies, photo, fraud reports, follow-status POSTs.

_T3_ENTRIES: dict[str, TierEntry] = {
    "APPLY_WORKFLOW_API": TierEntry(TIER_T3, "naukri_server.tools.apply", "Apply workflow — irreversible"),
    "INBOX_MARK_INTERESTED_API": TierEntry(TIER_T3, "naukri_server.tools.inbox", "Recruiter signal — irreversible"),
    "NOTIFICATION_READ_API": TierEntry(TIER_T3, "naukri_server.tools.notifications", "Mark-as-read mutation"),
    "JOB_ALERT_API": TierEntry(TIER_T3, "naukri_server.tools.alerts", "Alert create POST"),
    "SAVE_JOB_API": TierEntry(TIER_T3, "naukri_server.tools.saved_jobs", "Save-job POST — modifies remote state"),
    "UNSAVE_JOB_API": TierEntry(TIER_T3, "naukri_server.tools.saved_jobs", "Unsave-job POST — modifies remote state"),
    "REPORT_FRAUD_API": TierEntry(TIER_T3, "naukri_server.tools.jobs", "Fraud report — sent to Naukri trust team"),
    "BATCH_FOLLOW_STATUS_API": TierEntry(TIER_T3, "naukri_server.tools.companies",
        "Batch follow-status POST (GET variant is T1)"),
    "FORMATTED_SETTINGS_API": TierEntry(TIER_T3, "naukri_server.tools.settings", "Account settings"),
    "SETTINGS_API": TierEntry(TIER_T3, "naukri_server.tools.settings", "Account settings (raw)"),
    "BLOCKED_COMPANIES_API": TierEntry(TIER_T3, "naukri_server.tools.settings", "Blocked-companies list"),
    "PHOTO_API": TierEntry(TIER_T3, "naukri_server.tools.resume_photo", "Profile photo upload/delete"),
}


# ---------------------------------------------------------------------------
# Aggregated registry — MUST be consulted via tier_for() / has_tier(), never
# mutated directly by callers.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, TierEntry] = {**_T1_ENTRIES, **_T2_ENTRIES, **_T3_ENTRIES}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tier_for(constant_name: str) -> TierEntry | None:
    """Return the TierEntry for a config constant name, or None if unmapped."""
    return _REGISTRY.get(constant_name)


def has_tier(constant_name: str) -> bool:
    """True if the constant has a tier assignment."""
    return constant_name in _REGISTRY


def all_constants() -> tuple[str, ...]:
    """Tuple of every constant name that has a tier assignment."""
    return tuple(_REGISTRY.keys())


def constants_by_tier(tier: str) -> tuple[str, ...]:
    """Constants belonging to the given tier (T1/T2/T3). Order is insertion order."""
    return tuple(name for name, entry in _REGISTRY.items() if entry.tier == tier)


def tier_counts() -> dict[str, int]:
    """{T1: n1, T2: n2, T3: n3} — useful for daily-brief healing summary."""
    counts = {TIER_T1: 0, TIER_T2: 0, TIER_T3: 0}
    for entry in _REGISTRY.values():
        counts[entry.tier] = counts[entry.tier] + 1
    return counts
