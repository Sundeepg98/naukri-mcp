"""Tests for naukri_server.healing.synthesis — the evidence-derived auto-fix
synthesis (enrichment) layer that turns RAW detected drift into a concrete,
dry-run-gated, shadow-defaulted fix.

Two things are proven here, exhaustively and OFFLINE:

  1. It HEALS correctly — a known field-add drift yields the correct alias
     candidate, the dry-run passes, the fix applies through the unchanged
     router->apply->verify->revert path, and verification confirms it.

  2. It NEVER ships a bad fix — ambiguous/unknown drift yields no candidate
     (notify); a candidate the dry-run rejects is never applied; a fix that
     passes dry-run but later regresses is auto-reverted; and in shadow mode
     (the default) a perfectly-good candidate is PROPOSED but commits nothing.

Everything is mocked: a real `git init` repo in tmp_path, a tmp SQLite DB, and
an injected dry-run validator. No live browser, no Playwright, no network.
Marked implicitly "not e2e" so it runs in the default suite.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server import config
from naukri_server.events import EndpointDriftDetected, AutoFixReverted
from naukri_server.healing import circuit, router, synthesis, t2_autofix
from naukri_server.healing.router import KEY_CANONICAL, KEY_NEW_ALIAS
from naukri_server.healing.synthesis import (
    derive_alias_candidate,
    read_field_aliases_from_source,
    synthesize_candidate,
    enrich_drift_event,
)


# ---------------------------------------------------------------------------
# Fixtures — a real git repo with FIELD_ALIASES dicts the synthesizer can read
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path):
    """Real git repo with a fake naukri_server tree carrying FIELD_ALIASES.

    SEARCH_API is a T2 endpoint whose parser_module is
    naukri_server.services.search_service (per the real tier_registry), so we
    seed a FIELD_ALIASES dict there with a canonical 'title' the synthesizer can
    extend. NOTIFICATION_FEED_API is a T1 endpoint -> tools/notifications.py.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@local"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test-user"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)

    pkg = repo / "naukri_server"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "config.py").write_text(
        '"""Fake config."""\n'
        'SEARCH_API = "/jobapi/v3/search"\n'
        'NOTIFICATION_FEED_API = "/notif/v0/feed"\n',
        encoding="utf-8",
    )

    services = pkg / "services"
    services.mkdir()
    (services / "__init__.py").write_text("", encoding="utf-8")
    (services / "search_service.py").write_text(
        '"""Fake search service."""\n'
        'FIELD_ALIASES = {\n'
        '    "title": ["title"],\n'
        '    "company": ["company", "companyName"],\n'
        '}\n',
        encoding="utf-8",
    )

    tools = pkg / "tools"
    tools.mkdir()
    (tools / "__init__.py").write_text("", encoding="utf-8")
    (tools / "notifications.py").write_text(
        '"""Fake notifications parser."""\n'
        'FIELD_ALIASES = {\n'
        '    "title": ["title"],\n'
        '}\n',
        encoding="utf-8",
    )

    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial", "--no-gpg-sign"],
        check=True,
    )
    yield repo


@pytest.fixture(autouse=True)
def enable_healer():
    circuit._reset_for_tests()
    circuit.enable()
    yield
    circuit._reset_for_tests()


@pytest.fixture(autouse=True)
def point_router_at_fake_repo(fake_repo):
    """Force router to operate on the tmp fake_repo, never the real checkout."""
    with patch("naukri_server.healing.router._repo_root", return_value=fake_repo):
        yield


@pytest.fixture
def autofix_on(monkeypatch):
    """Turn auto-fix ON (out of the default shadow mode) for happy-path tests."""
    monkeypatch.setattr(config, "HEALING_AUTOFIX_ENABLED", True, raising=False)
    yield


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit_count(repo: Path) -> int:
    return int(subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())


def _search_src(repo: Path) -> str:
    return (repo / "naukri_server" / "services" / "search_service.py").read_text()


# ===========================================================================
# UNIT: evidence extraction is deterministic + conservative
# ===========================================================================


def test_read_field_aliases_parses_str_to_list():
    src = 'FIELD_ALIASES = {"title": ["title", "jobTitle"]}\n'
    out = read_field_aliases_from_source(src)
    assert out == {"title": ["title", "jobTitle"]}


def test_read_field_aliases_rejects_non_mapping():
    assert read_field_aliases_from_source("X = 1\n") is None
    assert read_field_aliases_from_source('FIELD_ALIASES = {"k": "notalist"}\n') is None


def test_derive_candidate_hard_rename_maps_to_single_canonical():
    """PRIMARY case: a known alias is REMOVED and a sibling is ADDED at the same
    parent -> the added field is the rename, mapped to that canonical."""
    aliases = {"title": ["title"]}
    changed = {
        "jobDetails.0.title": "removed",   # known alias gone
        "jobDetails.0.jobTitle": "added",  # sibling appeared
    }
    canonical, new_alias, evidence = derive_alias_candidate(changed, aliases)
    assert canonical == "title"
    assert new_alias == "jobTitle"
    assert "rename" in evidence


def test_derive_candidate_additive_needs_sibling_evidence():
    """SECONDARY case: both names present (no removal). Without explicit
    full-response sibling evidence the derivation declines (conservative)."""
    aliases = {"title": ["title"]}
    changed = {"jobDetails.0.jobTitle": "added"}  # title unchanged, absent from report
    # No sibling evidence supplied -> None.
    canonical, alias, reason = derive_alias_candidate(changed, aliases)
    assert canonical is None and alias is None
    # WITH full-response sibling evidence (title is a sibling) -> maps.
    canonical, alias, _ = derive_alias_candidate(
        changed, aliases, sibling_leaves_by_parent={"jobDetails.0": {"title", "jobTitle"}},
    )
    assert canonical == "title" and alias == "jobTitle"


def test_derive_candidate_no_added_returns_none():
    canonical, alias, reason = derive_alias_candidate(
        {"jobDetails.0.title": "removed"}, {"title": ["title"]},
    )
    assert canonical is None and alias is None
    assert "no added" in reason


def test_derive_candidate_added_without_removed_sibling_is_none():
    """An added field with no removed known-alias sibling -> cannot map -> None."""
    aliases = {"title": ["title"]}
    changed = {"jobDetails.0.mysteryField": "added"}  # no removed pair
    canonical, alias, reason = derive_alias_candidate(changed, aliases)
    assert canonical is None and alias is None
    assert "unambiguously" in reason


def test_derive_candidate_removed_at_different_parent_is_none():
    """A removal at a DIFFERENT parent than the add is not a rename pair -> None."""
    aliases = {"title": ["title"]}
    changed = {
        "clusters.0.title": "removed",     # different parent
        "jobDetails.0.jobTitle": "added",
    }
    canonical, alias, reason = derive_alias_candidate(changed, aliases)
    assert canonical is None and alias is None


def test_derive_candidate_two_removals_same_parent_is_data_loss():
    """Two known aliases removed at the SAME parent is indistinguishable from
    data loss + additions -> hard NO (cannot pair safely)."""
    aliases = {"title": ["title"], "company": ["company"]}
    changed = {
        "row.0.title": "removed", "row.0.jobTitle": "added",
        "row.0.company": "removed", "row.0.orgName": "added",
    }
    canonical, alias, reason = derive_alias_candidate(changed, aliases)
    assert canonical is None and alias is None
    assert "data loss" in reason


def test_derive_candidate_two_rename_pairs_different_parents_is_ambiguous():
    """Two clean 1:1 rename pairs at DIFFERENT parents -> two distinct
    candidates -> ambiguous batch -> hard NO."""
    aliases = {"title": ["title"], "company": ["company"]}
    changed = {
        "jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added",
        "companyDetails.0.company": "removed", "companyDetails.0.orgName": "added",
    }
    canonical, alias, reason = derive_alias_candidate(changed, aliases)
    assert canonical is None and alias is None
    assert "ambiguous" in reason


def test_derive_candidate_added_already_known_alias_is_noop_none():
    """An 'added' field whose leaf is already a known alias -> nothing to do."""
    aliases = {"title": ["title", "jobTitle"]}
    changed = {"row.0.title": "removed", "row.0.jobTitle": "added"}  # jobTitle already known
    canonical, alias, reason = derive_alias_candidate(changed, aliases)
    assert canonical is None and alias is None


def test_derive_candidate_removed_unknown_field_is_not_evidence():
    """Removal of a field that is NOT a known alias provides no rename evidence."""
    aliases = {"title": ["title"]}
    changed = {"row.0.someTracker": "removed", "row.0.jobTitle": "added"}
    canonical, alias, reason = derive_alias_candidate(changed, aliases)
    assert canonical is None and alias is None


# ===========================================================================
# UNIT: synthesize_candidate scope limits
# ===========================================================================


def test_synthesize_rejects_url_drift(fake_repo):
    cand, reason = synthesize_candidate(
        EndpointDriftDetected(constant_name="SEARCH_API", drift_type="url",
                              changed_fields={"x": "added"}),
        fake_repo,
    )
    assert cand is None
    assert "not auto-synthesizable" in reason


def test_synthesize_rejects_orphan_removed_field(fake_repo):
    """A removed known-alias with NO matching added sibling = data loss -> None."""
    cand, reason = synthesize_candidate(
        EndpointDriftDetected(constant_name="SEARCH_API", drift_type="field",
                              changed_fields={"jobDetails.0.title": "removed"}),
        fake_repo,
    )
    assert cand is None
    # no added field at all -> "no added fields to alias"
    assert "no added" in reason


def test_synthesize_rejects_unrelated_extra_removal(fake_repo):
    """A valid rename pair PLUS an unrelated known-alias removal -> data loss -> None.

    search_service aliases 'company' -> ['company','companyName']; removing
    'company' while renaming title is an orphan removal (the company datum is
    gone), so we must NOT half-fix.
    """
    cand, reason = synthesize_candidate(
        EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={
                "jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added",
                "jobDetails.0.company": "removed",  # unrelated known-alias removal
            },
        ),
        fake_repo,
    )
    assert cand is None
    assert "data loss" in reason


def test_synthesize_rejects_type_change(fake_repo):
    cand, reason = synthesize_candidate(
        EndpointDriftDetected(constant_name="SEARCH_API", drift_type="field",
                              changed_fields={"jobDetails.0.minSal": "type_change: number -> str"}),
        fake_repo,
    )
    assert cand is None
    assert "type change" in reason


def test_synthesize_rejects_endpoint_without_field_aliases(fake_repo):
    """PROFILE_API's parser_module file doesn't exist in the fake repo -> no aliases -> None."""
    cand, reason = synthesize_candidate(
        EndpointDriftDetected(constant_name="PROFILE_API", drift_type="field",
                              changed_fields={"row.0.title": "removed", "row.0.newField": "added"}),
        fake_repo,
    )
    assert cand is None


def test_synthesize_happy_returns_unambiguous_candidate(fake_repo):
    cand, reason = synthesize_candidate(
        EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={"jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added"},
        ),
        fake_repo,
    )
    assert cand is not None
    assert cand.canonical_key == "title"
    assert cand.new_alias == "jobTitle"
    assert cand.confidence == 1.0
    assert cand.dry_run_passed is False  # not dry-run yet at this stage


# ===========================================================================
# (a) HEALS CORRECTLY: known field drift -> candidate -> dry-run passes ->
#     applied -> verified
# ===========================================================================


@pytest.mark.asyncio
async def test_a_known_field_drift_synthesizes_applies_and_verifies(fake_repo, tmp_path, autofix_on):
    """End-to-end happy path with auto-fix ENABLED.

    Raw (param-less) field-add drift on SEARCH_API (T2) -> router synthesizes
    the 'jobTitle' alias -> injected dry-run PASSES -> fix applies (commit +
    pending row) -> a healthy verify CONFIRMS -> the commit stays, file edited.
    """
    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)

    async def dry_run_ok(_candidate):
        return True

    with patch("naukri_server.database.DB_PATH", db_path), \
         patch("naukri_server.healing.router._default_dry_run_validate",
               new=AsyncMock(side_effect=dry_run_ok)):
        from naukri_server.database import init_db, list_auto_fix_pending
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", url="/jobapi/v3/search",
            severity="added", drift_type="field",
            changed_fields={"jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added"},
        ))
        pending = await list_auto_fix_pending(status="pending")
        assert len(pending) == 1, "synthesized T2 fix should create a pending row"

        async def healthy(_constant):
            return True

        head_after_apply = _head_sha(fake_repo)
        result = await t2_autofix.verify_pending_row(
            fake_repo, pending[0], healthy, skip_emit=True,
        )

    assert _commit_count(fake_repo) == before_commits + 1, "synthesized fix must commit"
    assert "jobTitle" in _search_src(fake_repo), "the alias must be appended"
    assert result.confirmed is True
    assert _head_sha(fake_repo) == head_after_apply, "confirmed fix must not move HEAD"


@pytest.mark.asyncio
async def test_a_enrich_returns_enriched_event_when_enabled(fake_repo, autofix_on):
    """enrich_drift_event returns an event carrying the synthesized fix params."""
    async def dry_run_ok(_c):
        return True

    outcome = await enrich_drift_event(
        EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={"jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added"},
        ),
        fake_repo, dry_run_ok,
    )
    assert outcome.enriched_event is not None
    cf = outcome.enriched_event.changed_fields
    assert cf[KEY_CANONICAL] == "title"
    assert cf[KEY_NEW_ALIAS] == "jobTitle"
    assert outcome.candidate.dry_run_passed is True


# ===========================================================================
# (b) AMBIGUOUS / UNKNOWN drift -> NO candidate -> safe notify (nothing applied)
# ===========================================================================


@pytest.mark.asyncio
async def test_b_ambiguous_drift_is_notify_only_nothing_applied(fake_repo, tmp_path, autofix_on):
    """Two distinct candidates in one drift batch is ambiguous -> notify, no commit.

    Even with auto-fix ENABLED and a dry-run that WOULD pass, an ambiguous drift
    must never apply (synthesis declines before the dry-run is even relevant).
    """
    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)

    async def dry_run_ok(_c):
        return True

    with patch("naukri_server.database.DB_PATH", db_path), \
         patch("naukri_server.healing.router._default_dry_run_validate",
               new=AsyncMock(side_effect=dry_run_ok)):
        from naukri_server.database import init_db, list_auto_fix_pending, list_undelivered_notifications
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={
                "jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added",
                "jobDetails.0.company": "removed", "jobDetails.0.orgName": "added",
            },
        ))
        assert len(await list_auto_fix_pending(status="pending")) == 0, "ambiguous drift must not apply"
        notifs = await list_undelivered_notifications(limit=10)

    assert _commit_count(fake_repo) == before_commits, "no commit for ambiguous drift"
    assert any("no verified alias mapping" in (n.get("body") or "") for n in notifs)


@pytest.mark.asyncio
async def test_b_unknown_field_no_sibling_is_notify_only(fake_repo, tmp_path, autofix_on):
    """An added field with no known sibling can't be mapped -> notify, no commit."""
    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)
    with patch("naukri_server.database.DB_PATH", db_path), \
         patch("naukri_server.healing.router._default_dry_run_validate",
               new=AsyncMock(return_value=True)):
        from naukri_server.database import init_db, list_auto_fix_pending
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={"jobDetails.0.totallyNewThing": "added"},  # no sibling
        ))
        assert len(await list_auto_fix_pending(status="pending")) == 0
    assert _commit_count(fake_repo) == before_commits


# ===========================================================================
# (c) WRONG candidate -> dry-run FAILS -> NOT applied
# ===========================================================================


@pytest.mark.asyncio
async def test_c_candidate_failing_dry_run_is_not_applied(fake_repo, tmp_path, autofix_on):
    """A structurally-valid candidate whose dry-run REJECTS it never commits.

    Models the case where the 'added' field is transient/phantom: the dry-run
    re-probe can't confirm it, so the candidate is dropped -> notify-only.
    """
    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)

    async def dry_run_fail(_candidate):
        return False  # dry-run could not confirm the candidate

    with patch("naukri_server.database.DB_PATH", db_path), \
         patch("naukri_server.healing.router._default_dry_run_validate",
               new=AsyncMock(side_effect=dry_run_fail)):
        from naukri_server.database import init_db, list_auto_fix_pending, list_undelivered_notifications
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={"jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added"},
        ))
        assert len(await list_auto_fix_pending(status="pending")) == 0, "dry-run failure must not apply"
        notifs = await list_undelivered_notifications(limit=10)

    assert _commit_count(fake_repo) == before_commits, "no commit when dry-run fails"
    assert "jobTitle" not in _search_src(fake_repo)
    # The failing dry-run produces a notify fallback (no shadow proposal, since
    # dry_run_passed is False).
    assert any("no verified alias mapping" in (n.get("body") or "") for n in notifs)


@pytest.mark.asyncio
async def test_c_dry_run_exception_is_treated_as_failure(fake_repo, autofix_on):
    """If the dry-run validator raises, the candidate is rejected (conservative)."""
    async def dry_run_boom(_c):
        raise RuntimeError("probe exploded")

    outcome = await enrich_drift_event(
        EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={"jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added"},
        ),
        fake_repo, dry_run_boom,
    )
    assert outcome.enriched_event is None
    assert outcome.candidate is not None and outcome.candidate.dry_run_passed is False
    assert "dry-run failed" in outcome.reason


# ===========================================================================
# (d) Passes dry-run but LATER regresses -> auto-reverted
# ===========================================================================


@pytest.mark.asyncio
async def test_d_synthesized_fix_that_later_regresses_is_reverted(fake_repo, tmp_path, autofix_on):
    """SAFETY-CRITICAL: a synthesized fix that applies (dry-run passed) but then
    REGRESSES at verify time is auto-reverted by the existing T2 verify path.

    Proves synthesis does not bypass the snapshot+revert safety net: the edit is
    undone by `git revert`, the row is 'reverted', and AutoFixReverted fires.
    """
    db_path = tmp_path / "naukri.db"
    original_src = _search_src(fake_repo)

    async def dry_run_ok(_c):
        return True

    with patch("naukri_server.database.DB_PATH", db_path), \
         patch("naukri_server.healing.router._default_dry_run_validate",
               new=AsyncMock(side_effect=dry_run_ok)):
        from naukri_server.database import init_db, list_auto_fix_pending, get_auto_fix_pending
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={"jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added"},
        ))
        pending = await list_auto_fix_pending(status="pending")
        assert len(pending) == 1
        row_id = pending[0]["id"]
        assert "jobTitle" in _search_src(fake_repo), "fix landed provisionally"

        # Verify-time revalidation says the endpoint is now UNHEALTHY (regression).
        async def regressed(_constant):
            return False

        with patch("naukri_server.healing.t2_autofix.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await t2_autofix.verify_pending_row(fake_repo, pending[0], regressed)

        assert result.reverted is True and result.confirmed is False
        emitted = [c for c in mock_emit.call_args_list
                   if isinstance(c.args[0], AutoFixReverted)]
        assert len(emitted) == 1, "AutoFixReverted must fire on revert"
        row = await get_auto_fix_pending(row_id)
    assert row["status"] == "reverted"
    # The synthesized edit is GONE — file back to original.
    assert "jobTitle" not in _search_src(fake_repo)
    assert _search_src(fake_repo) == original_src


# ===========================================================================
# (e) SHADOW MODE (flag off, the DEFAULT) -> proposes + logs but commits NOTHING
# ===========================================================================


@pytest.mark.asyncio
async def test_e_shadow_mode_proposes_but_commits_nothing(fake_repo, tmp_path):
    """The safe default: HEALING_AUTOFIX_ENABLED is False.

    A perfectly-good candidate that PASSES the dry-run is still NOT applied — it
    is proposed (a HealingProposal notification is stored) and logged, with zero
    commits. This is what ships by default.
    """
    # Do NOT use autofix_on — rely on the real default (False).
    assert config.HEALING_AUTOFIX_ENABLED is False, "default must be shadow (off)"

    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)

    with patch("naukri_server.database.DB_PATH", db_path), \
         patch("naukri_server.healing.router._default_dry_run_validate",
               new=AsyncMock(return_value=True)):
        from naukri_server.database import init_db, list_auto_fix_pending, list_undelivered_notifications
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={"jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added"},
        ))
        assert len(await list_auto_fix_pending(status="pending")) == 0, "shadow mode must not apply"
        notifs = await list_undelivered_notifications(limit=10)

    assert _commit_count(fake_repo) == before_commits, "SHADOW MODE MUST NOT COMMIT"
    assert "jobTitle" not in _search_src(fake_repo), "no file edit in shadow mode"
    # A shadow proposal must have been recorded (proof it was synthesized + dry-run-passed).
    proposal = [n for n in notifs if n["event_type"] == "HealingProposal"
                and "jobTitle" in (n.get("body") or "")]
    assert proposal, "shadow mode must store a HealingProposal describing the would-be fix"
    assert "shadow" in (proposal[0].get("body") or "").lower()


@pytest.mark.asyncio
async def test_e_shadow_enrich_outcome_flags_shadow(fake_repo):
    """enrich_drift_event in shadow mode: no enriched event, shadow=True, good candidate."""
    assert config.HEALING_AUTOFIX_ENABLED is False
    outcome = await enrich_drift_event(
        EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={"jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added"},
        ),
        fake_repo, AsyncMock(return_value=True),
    )
    assert outcome.enriched_event is None
    assert outcome.shadow is True
    assert outcome.candidate is not None
    assert outcome.candidate.dry_run_passed is True
    assert outcome.candidate.new_alias == "jobTitle"


# ===========================================================================
# Confidence gate + T1 path coverage
# ===========================================================================


@pytest.mark.asyncio
async def test_confidence_below_threshold_is_notify_only(fake_repo, tmp_path, autofix_on, monkeypatch):
    """If the structural confidence is below the configured threshold -> notify.

    We raise the threshold above 1.0 so even the unambiguous candidate is gated
    out, proving the confidence gate is enforced (dry-run not even reached).
    """
    monkeypatch.setattr(config, "HEALING_AUTOFIX_MIN_CONFIDENCE", 1.01, raising=False)
    dry_run = AsyncMock(return_value=True)
    outcome = await enrich_drift_event(
        EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={"jobDetails.0.title": "removed", "jobDetails.0.jobTitle": "added"},
        ),
        fake_repo, dry_run,
    )
    assert outcome.enriched_event is None
    assert "below threshold" in outcome.reason
    dry_run.assert_not_awaited()  # gate short-circuits before the dry-run


@pytest.mark.asyncio
async def test_t1_endpoint_synthesized_fix_applies_with_smoke_check(fake_repo, tmp_path, autofix_on):
    """A T1 endpoint (NOTIFICATION_FEED_API) synthesized fix applies via the T1
    path (synchronous smoke-check), committing once."""
    db_path = tmp_path / "naukri.db"
    before = _commit_count(fake_repo)
    with patch("naukri_server.database.DB_PATH", db_path), \
         patch("naukri_server.healing.router._default_dry_run_validate",
               new=AsyncMock(return_value=True)):
        from naukri_server.database import init_db
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="NOTIFICATION_FEED_API", drift_type="field",
            changed_fields={"feed.0.title": "removed", "feed.0.headline": "added"},
        ))
    # T1 commits synchronously (no pending row); smoke check passes -> stays.
    assert _commit_count(fake_repo) == before + 1
    notif_src = (fake_repo / "naukri_server" / "tools" / "notifications.py").read_text()
    assert "headline" in notif_src


# ===========================================================================
# Re-snapshot subscriber: absorbs an added field after a field fix
# ===========================================================================


@pytest.mark.asyncio
async def test_resnapshot_subscriber_absorbs_added_field():
    """After a field AutoFixApplied, the endpoint baseline is refreshed so a
    subsequent drift check no longer reports the (now-expected) added field."""
    from naukri_server.probes.drift_detector import DriftDetector
    from naukri_server.events import AutoFixApplied

    det = DriftDetector()
    path = "/jobapi/v3/search"
    det.snapshot_endpoint(path, {"jobDetails": [{"title": "x"}]})
    # New shape has an extra field -> would be drift vs the old baseline.
    new_resp = {"jobDetails": [{"title": "x", "jobTitle": "x"}]}
    assert det.check_drift(path, new_resp) is not None  # drift present before re-snapshot

    fake_get = AsyncMock(return_value=new_resp)
    with patch("naukri_server.config.SEARCH_API", path, create=True), \
         patch("naukri_server.interfaces.api_client.get", new=fake_get), \
         patch("naukri_server.probes.drift_detector.detector", det):
        await synthesis._absorb_added_field_after_field_fix(AutoFixApplied(
            constant_name="SEARCH_API", tier="T2", drift_type="field",
            file_path="naukri_server/services/search_service.py",
        ))
    # Baseline now includes the added field -> no more drift on that shape.
    assert det.check_drift(path, new_resp) is None


@pytest.mark.asyncio
async def test_resnapshot_subscriber_skips_url_fixes():
    """URL fixes must NOT trigger a re-snapshot (old snapshot key is stale)."""
    from naukri_server.events import AutoFixApplied
    fake_get = AsyncMock()
    with patch("naukri_server.interfaces.api_client.get", new=fake_get):
        await synthesis._absorb_added_field_after_field_fix(AutoFixApplied(
            constant_name="SEARCH_API", tier="T2", drift_type="url",
            file_path="naukri_server/config.py",
        ))
    fake_get.assert_not_awaited()
