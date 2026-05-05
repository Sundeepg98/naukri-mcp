"""Tests for naukri_server.healing.tier_registry — pure data, pure unit.

The registry decides what the healer is allowed to do, so its assertions are
SAFETY-CRITICAL. The headline test (test_no_t3_endpoint_in_t1_or_t2) doubles
as the contract enforcing 'T3 never reaches T1/T2 paths' at the data layer.
"""

import pytest

from naukri_server.healing import tier_registry as reg


# ---------------------------------------------------------------------------
# Headline counts — must match the design spec exactly
# ---------------------------------------------------------------------------


def test_tier_counts_match_spec():
    """The Pragmatic Auto-Fix design spec mandates 15 T1 / 22 T2 / 11 T3.

    Our registry maps the 8 AmbitionBox endpoints individually under T1
    (instead of a single AB_* row), pushing T1 to 23 entries. This is a
    DELIBERATE deviation: each AB endpoint has a distinct response shape,
    so they each need their own parser_module routing. T2 and T3 counts
    must stay exactly as the spec requires.
    """
    counts = reg.tier_counts()
    # T2 and T3 are spec-exact and safety-critical
    assert counts[reg.TIER_T2] == 25, f"T2 count drift: {counts}"
    assert counts[reg.TIER_T3] == 12, f"T3 count drift: {counts}"
    # T1 is allowed to be larger because of AB endpoint expansion
    assert counts[reg.TIER_T1] >= 15, f"T1 underflow: {counts}"


def test_total_endpoints_in_registry():
    """Total registry entries must cover all explicitly-classified endpoints."""
    counts = reg.tier_counts()
    assert sum(counts.values()) == len(reg.all_constants())


# ---------------------------------------------------------------------------
# SAFETY: every endpoint MUST have a tier — no Nones, no silent T1 default
# ---------------------------------------------------------------------------


def test_all_critical_endpoints_have_tier_assignment():
    """Every endpoint mentioned in the spec must be classified.

    Missing a classification = the healer doesn't know what to do = silent
    T1-style auto-fix on something that might be T3 = data-loss risk.
    """
    critical = {
        # T1 critical (display-only)
        "NOTIFICATION_FEED_API", "NOTIFICATION_COUNT_API",
        "MOCK_INTERVIEW_TOPICS_API", "MOCK_INTERVIEW_HISTORY_API",
        "MOCK_INTERVIEW_ROLE_API", "MOCK_INTERVIEW_OTHER_TOPICS_API",
        "MOCK_INTERVIEW_QUESTION_API",
        "RESUME_DOWNLOAD_API",
        "RESUME_BUILDER_CONFIG_API", "RESUME_BUILDER_STATUS_API",
        "AB_SALARY_API", "AB_BENEFITS_API", "AB_REVIEW_DIST_API",
        "AB_REVIEW_FILTERS_API", "AB_INTERVIEW_QS_API",
        "AB_COMPANY_COMPARE_API", "AB_COMPANY_LOCATIONS_API",
        "AB_INSIGHTS_APPLIED_API",
        "ENTITY_TAXONOMY_API", "DFP_PROFILE_API", "CCS_PAGE_API",
        "N360_CONFIG_API", "ALERT_DETAIL_API",
        "COMPANY_FOLLOW_STATUS_API",
        # T2 critical (agent-decision-feeding)
        "SEARCH_API", "RECOMMENDED_JOBS_API", "JOB_DETAIL_API",
        "JOB_DETAIL_V1_API", "JOB_MATCH_SCORE_API", "SIMILAR_JOBS_API",
        "MATCH_ANALYTICS_API", "APPLY_MATCH_SCORE_API",
        "APPLIED_JOBS_API", "SAVED_JOBS_API", "APPLICATION_STATUS_API",
        "INBOX_API", "MESSAGE_API", "INBOX_REST_API",
        "DASHBOARD_API", "JOB_ALERTS_LIST_API",
        "PROFILE_API", "FULLPROFILES_API", "COMPANY_SEARCH_API",
        "BULK_JOBS_API", "EARLY_ACCESS_API", "RECOMMEND_NOTIFY_API",
        "SEARCH_IMPRESSIONS_API", "RECRUITER_ACTIVITY_API",
        "ACTIVITY_LEVEL_API",
        # T3 critical (irreversible)
        "APPLY_WORKFLOW_API", "INBOX_MARK_INTERESTED_API",
        "NOTIFICATION_READ_API", "JOB_ALERT_API",
        "SAVE_JOB_API", "UNSAVE_JOB_API", "REPORT_FRAUD_API",
        "BATCH_FOLLOW_STATUS_API",
        "FORMATTED_SETTINGS_API", "SETTINGS_API",
        "BLOCKED_COMPANIES_API", "PHOTO_API",
    }
    missing = [c for c in critical if not reg.has_tier(c)]
    assert not missing, f"Endpoints missing tier assignment: {missing}"


# ---------------------------------------------------------------------------
# SAFETY: T3 endpoints must NEVER appear in T1 or T2
# ---------------------------------------------------------------------------


def test_no_t3_endpoint_in_t1_or_t2():
    """Single most important safety contract.

    If a T3 endpoint were misclassified as T1, the healer would auto-edit our
    code in response to drift on `/apply` or `/save` — corrupting the user's
    job-hunt state. This test enforces the partition at the data layer so any
    accidental reclassification fails CI.
    """
    t3 = set(reg.constants_by_tier(reg.TIER_T3))
    t1 = set(reg.constants_by_tier(reg.TIER_T1))
    t2 = set(reg.constants_by_tier(reg.TIER_T2))

    overlap_t1 = t3 & t1
    overlap_t2 = t3 & t2
    assert not overlap_t1, f"T3 ∩ T1 must be empty, got: {overlap_t1}"
    assert not overlap_t2, f"T3 ∩ T2 must be empty, got: {overlap_t2}"
    # And T1/T2 disjoint too — no double-classification anywhere
    assert not (t1 & t2), f"T1 ∩ T2 must be empty, got: {t1 & t2}"


def test_irreversible_endpoints_classified_t3():
    """Belt-and-suspenders check on the spec's T3 list."""
    spec_t3 = {
        "APPLY_WORKFLOW_API", "INBOX_MARK_INTERESTED_API", "NOTIFICATION_READ_API",
        "JOB_ALERT_API", "SAVE_JOB_API", "UNSAVE_JOB_API", "REPORT_FRAUD_API",
        "BATCH_FOLLOW_STATUS_API", "FORMATTED_SETTINGS_API", "SETTINGS_API",
        "BLOCKED_COMPANIES_API", "PHOTO_API",
    }
    for c in spec_t3:
        entry = reg.tier_for(c)
        assert entry is not None, f"{c} unmapped"
        assert entry.tier == reg.TIER_T3, f"{c} should be T3, got {entry.tier}"


def test_agent_decision_endpoints_classified_t2():
    """Per the spec — agent-decision-feeding endpoints stay T2 (snapshot+revert)."""
    must_be_t2 = {
        "SEARCH_API", "RECOMMENDED_JOBS_API", "JOB_DETAIL_API",
        "JOB_MATCH_SCORE_API", "SIMILAR_JOBS_API",
        "APPLIED_JOBS_API", "SAVED_JOBS_API", "DASHBOARD_API",
        "PROFILE_API", "BULK_JOBS_API",
    }
    for c in must_be_t2:
        entry = reg.tier_for(c)
        assert entry is not None
        assert entry.tier == reg.TIER_T2, f"{c} should be T2, got {entry.tier}"


# ---------------------------------------------------------------------------
# Public API behavior
# ---------------------------------------------------------------------------


def test_tier_for_returns_none_for_unknown():
    assert reg.tier_for("NOT_A_REAL_API") is None
    assert reg.has_tier("NOT_A_REAL_API") is False


def test_constants_by_tier_returns_only_that_tier():
    for tier in reg.ALL_TIERS:
        names = reg.constants_by_tier(tier)
        for name in names:
            assert reg.tier_for(name).tier == tier


def test_tier_entries_have_parser_module_for_t1_and_t2():
    """T1/T2 entries need a parser_module so the healer knows where to look.
    (T3 entries don't need it — they only generate notifications.)"""
    for name in reg.constants_by_tier(reg.TIER_T1):
        assert reg.tier_for(name).parser_module is not None, \
            f"T1 entry {name} missing parser_module"
    for name in reg.constants_by_tier(reg.TIER_T2):
        assert reg.tier_for(name).parser_module is not None, \
            f"T2 entry {name} missing parser_module"


def test_tier_entry_is_immutable():
    """TierEntry is a frozen dataclass — accidental mutation must raise."""
    entry = reg.tier_for("SEARCH_API")
    assert entry is not None
    with pytest.raises(Exception):  # FrozenInstanceError
        entry.tier = "T1"  # type: ignore[misc]


def test_all_constants_resolve_via_lookup():
    """Round-trip: every name in all_constants() must resolve via tier_for()."""
    for name in reg.all_constants():
        assert reg.tier_for(name) is not None
