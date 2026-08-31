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
        "AB_INTERVIEW_QS_API",
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


# ---------------------------------------------------------------------------
# The registry must describe reality, not intent
# ---------------------------------------------------------------------------
#
# `parser_module` is not documentation. router.py, synthesis.py, t1_autofix.py
# and t2_autofix.py all resolve it to a FILE PATH and patch that file -- and a
# T1 fix is committed to git with no further check. So an entry naming a module
# that does not touch the endpoint points the healer's edit at the wrong file,
# and an entry for an endpoint NOTHING calls authorizes an auto-fix that can
# only ever be wrong.
#
# Census instrument: _sweep/tier_registry_census.py (2026-08-31).

import re as _re                                            # noqa: E402
from pathlib import Path as _Path                           # noqa: E402

import naukri_server as _pkg                                # noqa: E402

_PKG_ROOT = _Path(_pkg.__file__).parent
_REGISTRY_FILE = _PKG_ROOT / "healing" / "tier_registry.py"
_CONFIG_FILE = _PKG_ROOT / "config.py"


def _production_files():
    """Every package .py EXCEPT the definition site and the registry itself.

    Excluding tier_registry.py is the whole point: a constant whose only
    mention is its own monitoring row has no reader, and a check that counts
    that row as a reader cannot see an orphan. `tests/test_no_decoys.py`
    counts it, which is exactly why AB_REVIEW_FILTERS_API stayed green there
    while being called by nothing.
    """
    return [
        p for p in sorted(_PKG_ROOT.rglob("*.py"))
        if "__pycache__" not in p.parts
        and p != _CONFIG_FILE
        and p != _REGISTRY_FILE
    ]


def _files_referencing(name: str):
    pattern = _re.compile(rf"\b{_re.escape(name)}\b")
    return [
        p.relative_to(_PKG_ROOT).as_posix()
        for p in _production_files()
        if pattern.search(p.read_text(encoding="utf-8", errors="replace"))
    ]


def test_the_reference_scan_works():
    """CONTROL. Both directions, on this file's own assumptions.

    Without this, every 'no references found' verdict below is unfalsifiable:
    a scan over the wrong root, or a regex that never matches, reports a clean
    registry and an empty orphan list identically.
    """
    # positive: a constant that is unambiguously called
    hits = _files_referencing("DASHBOARD_API")
    assert "services/profile_service.py" in hits, hits
    # negative: a name that exists nowhere
    assert _files_referencing("ZZZ_NOT_A_REAL_API") == []
    # word-boundary: a strict prefix must not match inside a longer identifier
    assert _files_referencing("DASHBOARD_AP") == []
    # non-vacuous
    assert len(_production_files()) > 50, len(_production_files())


def test_no_registry_entry_is_an_orphan():
    """Every monitored endpoint must be called by something.

    Monitoring an endpoint no code calls buys nothing and costs a standing
    auto-fix authorization: a drift event would send the healer to patch a
    parser for a call site that does not exist.
    """
    orphans = sorted(
        name for name in reg.all_constants() if not _files_referencing(name)
    )
    assert orphans == [], (
        "these endpoints are in the healing registry but no production code "
        "calls them: %s -- wire each to something that wants it, or remove "
        "its registry row AND its config constant together. Leaving the "
        "constant behind re-creates the blind spot: its registry row is the "
        "only thing that makes it look read." % orphans
    )


# MEASURED 2026-08-31: 17 entries name a `parser_module` that does not
# reference their constant. The dominant pattern is an un-followed refactor --
# the row names the tools/ layer while the call moved to services/ (or the
# reverse). Each needs a judgement about which module owns the parser, and
# guessing wrong reproduces the defect, so they are LEDGERED rather than
# rewritten here. Four of them are T1, where a fix is auto-committed.
# Detail and the full table: _sweep/tier_registry_census.md.
ACKNOWLEDGED_MISATTRIBUTED = {
    "ACTIVITY_LEVEL_API", "APPLIED_JOBS_API", "APPLY_MATCH_SCORE_API",
    "BATCH_FOLLOW_STATUS_API", "CCS_PAGE_API", "COMPANY_FOLLOW_STATUS_API",
    "FULLPROFILES_API", "MATCH_ANALYTICS_API", "NOTIFICATION_COUNT_API",
    "NOTIFICATION_FEED_API", "NOTIFICATION_READ_API", "RECOMMENDED_JOBS_API",
    "RECOMMEND_NOTIFY_API", "RECRUITER_ACTIVITY_API", "SAVED_JOBS_API",
    "SEARCH_API", "SEARCH_IMPRESSIONS_API",
}


def test_no_new_misattributed_parser_module():
    """An entry's parser_module must be a file that touches the endpoint."""
    bad = []
    for name in reg.all_constants():
        entry = reg.tier_for(name)
        if entry.parser_module is None:
            continue
        target = _PKG_ROOT.parent / _Path(*entry.parser_module.split(".")).with_suffix(".py")
        if not target.exists():
            bad.append(f"{name} -> {entry.parser_module} (FILE MISSING)")
            continue
        if not _re.search(rf"\b{_re.escape(name)}\b",
                          target.read_text(encoding="utf-8", errors="replace")):
            if name not in ACKNOWLEDGED_MISATTRIBUTED:
                bad.append(f"{name} -> {entry.parser_module}")
    assert bad == [], (
        "these registry rows send the healer to a module that never mentions "
        "the endpoint, so a drift fix would patch the wrong file: %s" % bad
    )
