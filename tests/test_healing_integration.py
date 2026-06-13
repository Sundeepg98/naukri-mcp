"""End-to-end integration tests for the wired self-healing pipeline.

Proves the full chain works AND, most importantly, that the SAFETY contract
holds: an auto-fix that does not verify is reverted automatically.

Chain under test:
    EndpointDriftDetected  →  event_bus / healing.router.on_endpoint_drift
        →  tier dispatch (t1 / t2 / t3)
        →  apply (git commit)  →  verify  →  revert-on-failure | confirm-on-success

All tests are OFFLINE: a real `git init` repo in tmp_path + a tmp SQLite DB.
No live browser, no Playwright, no network — `revalidate` is an injected
callable / mock, exactly like the existing healing unit tests.

Marked implicitly "not e2e" (no e2e marker) so they run in the default suite.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server.events import (
    event_bus, EndpointDriftDetected, AutoFixReverted,
)
from naukri_server.healing import circuit, router, t1_autofix, t2_autofix
from naukri_server.healing.router import (
    KEY_NEW_URL, KEY_CANONICAL, KEY_NEW_ALIAS,
)


# ---------------------------------------------------------------------------
# Fixtures — same shape as the T1/T2 unit-test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path):
    """Real git repo with a fake naukri_server tree the healer can edit."""
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
        'PROFILE_API = "/profile/v2"\n'
        'NOTIFICATION_FEED_API = "/notif/v0/feed"\n'
        'APPLY_WORKFLOW_API = "/apply/v1/submit"\n',
        encoding="utf-8",
    )

    services = pkg / "services"
    services.mkdir()
    (services / "__init__.py").write_text("", encoding="utf-8")
    (services / "search_service.py").write_text(
        '"""Fake search service."""\n'
        'FIELD_ALIASES = {\n'
        '    "title": ["title", "jobTitle"],\n'
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
        '    "title": ["title", "displayTitle"],\n'
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
    """Force the router to operate on the tmp fake_repo, never the real checkout."""
    with patch("naukri_server.healing.router._repo_root", return_value=fake_repo):
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


# ===========================================================================
# Joint #2 wiring — EndpointDriftDetected actually reaches the router
# ===========================================================================


def test_router_is_the_live_subscriber_of_drift_event():
    """The wiring (subscribers.py imports healing → wire_subscribers) must have
    subscribed router.on_endpoint_drift to the GLOBAL event_bus."""
    handlers = event_bus._subscribers.get(EndpointDriftDetected, [])
    assert router.on_endpoint_drift in handlers, (
        "router.on_endpoint_drift is not subscribed to the live event_bus — "
        "the drift→heal joint is severed"
    )


@pytest.mark.asyncio
async def test_emitting_drift_on_bus_invokes_router(fake_repo, tmp_path):
    """Emit a real EndpointDriftDetected on the global bus and confirm the
    router handled it (here: a T3 endpoint → notification stored)."""
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_undelivered_notifications
        await init_db()
        await event_bus.emit(EndpointDriftDetected(
            constant_name="APPLY_WORKFLOW_API", url="/apply/v1/submit",
            severity="removed", drift_type="field",
            changed_fields={"someField": "removed"},
        ))
        notifs = await list_undelivered_notifications(limit=10)
    # A T3 drift with no fix params → a stored notification, nothing applied.
    assert any(n["event_type"] in ("EndpointDrift", "HealingProposal") for n in notifs)


# ===========================================================================
# SAFETY default — raw detector output (no fix params) NEVER auto-commits
# ===========================================================================


@pytest.mark.asyncio
async def test_field_drift_without_params_is_notify_only(fake_repo, tmp_path):
    """The conservative default: a T2 field drift with NO explicit alias mapping
    must NOT auto-commit (it would be a guess) — it falls back to notify."""
    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_undelivered_notifications
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", url="/jobapi/v3/search",
            severity="added", drift_type="field",
            changed_fields={"newMysteryField": "added"},  # no __canonical_key__/__new_alias__
        ))
        notifs = await list_undelivered_notifications(limit=10)
    assert _commit_count(fake_repo) == before_commits, "no auto-commit should have happened"
    assert any("no verified alias mapping" in (n.get("body") or "") for n in notifs)


@pytest.mark.asyncio
async def test_unmapped_endpoint_is_notify_only(fake_repo, tmp_path):
    """Drift on an endpoint with no tier classification: never guess a tier."""
    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_undelivered_notifications
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="TOTALLY_UNKNOWN_API", drift_type="url",
            changed_fields={KEY_NEW_URL: "/x/y/z"},  # even WITH params, unmapped → notify
        ))
        notifs = await list_undelivered_notifications(limit=10)
    assert _commit_count(fake_repo) == before_commits
    assert any("not in tier registry" in (n.get("body") or "") for n in notifs)


@pytest.mark.asyncio
async def test_t3_endpoint_never_autofixes_even_with_params(fake_repo, tmp_path):
    """SAFETY: a T3 endpoint must NEVER auto-fix, even if fix params are present."""
    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_undelivered_notifications
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="APPLY_WORKFLOW_API", drift_type="url",
            changed_fields={KEY_NEW_URL: "/apply/v2/submit"},
        ))
        notifs = await list_undelivered_notifications(limit=10)
    assert _commit_count(fake_repo) == before_commits, "T3 must never auto-commit"
    # A HealingProposal (with the would-be diff) should have been stored.
    assert any(n["event_type"] == "HealingProposal" for n in notifs)


@pytest.mark.asyncio
async def test_disabled_healer_routes_to_notify(fake_repo, tmp_path):
    """If the circuit is disabled, even a fully-parameterised T1/T2 drift is
    notify-only (the healer must be explicitly enabled to touch code)."""
    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_undelivered_notifications
        await init_db()
        circuit.disable("test: disabled")
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="url",
            changed_fields={KEY_NEW_URL: "/jobapi/v4/search"},
        ))
        notifs = await list_undelivered_notifications(limit=10)
    assert _commit_count(fake_repo) == before_commits
    assert any("healer disabled" in (n.get("body") or "") for n in notifs)


# ===========================================================================
# GOOD fix lands — T1 (with explicit params + passing smoke check)
# ===========================================================================


@pytest.mark.asyncio
async def test_good_t1_url_fix_lands(fake_repo, tmp_path):
    """A T1 url drift WITH a verified new_url applies and survives the smoke check."""
    db_path = tmp_path / "naukri.db"
    before_commits = _commit_count(fake_repo)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="NOTIFICATION_FEED_API", drift_type="url",
            changed_fields={KEY_NEW_URL: "/notif/v1/feed"},
        ))
    assert _commit_count(fake_repo) == before_commits + 1, "a good T1 fix should commit"
    config_src = (fake_repo / "naukri_server" / "config.py").read_text()
    assert "/notif/v1/feed" in config_src
    assert "/notif/v0/feed" not in config_src


# ===========================================================================
# GOOD fix lands — T2 (with explicit params) then verify CONFIRMS
# ===========================================================================


@pytest.mark.asyncio
async def test_good_t2_fix_lands_then_verify_confirms(fake_repo, tmp_path):
    """Full T2 happy path: router applies a field fix (pending row created),
    then a healthy revalidate confirms it — the commit STAYS."""
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_auto_fix_pending
        await init_db()
        # Route a T2 field drift with an explicit, valid alias mapping.
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={KEY_CANONICAL: "title", KEY_NEW_ALIAS: "newJobTitle"},
        ))
        pending = await list_auto_fix_pending(status="pending")
        assert len(pending) == 1, "router should have created a pending T2 row"
        head_after_apply = _head_sha(fake_repo)

        async def healthy(_constant):
            return True

        result = await t2_autofix.verify_pending_row(
            fake_repo, pending[0], healthy, skip_emit=True,
        )
    assert result.confirmed is True
    assert _head_sha(fake_repo) == head_after_apply, "confirmed fix must not move HEAD"
    src = (fake_repo / "naukri_server" / "services" / "search_service.py").read_text()
    assert "newJobTitle" in src


# ===========================================================================
# *** BAD fix is REVERTED *** — the headline safety test (T2 verify path)
# ===========================================================================


@pytest.mark.asyncio
async def test_bad_t2_fix_is_reverted_full_chain(fake_repo, tmp_path):
    """SAFETY-CRITICAL: drift → router → T2 apply → verify FAILS → AUTO-REVERT.

    Proves an auto-fix that does not verify cannot survive: the edit is undone
    by `git revert`, the row is marked 'reverted', and AutoFixReverted fires.
    """
    db_path = tmp_path / "naukri.db"
    original_src = (fake_repo / "naukri_server" / "services" / "search_service.py").read_text()
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import (
            init_db, list_auto_fix_pending, get_auto_fix_pending,
        )
        await init_db()
        # 1. Drift → router applies a T2 fix (a real but ultimately-bad alias).
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="SEARCH_API", drift_type="field",
            changed_fields={KEY_CANONICAL: "title", KEY_NEW_ALIAS: "wrongGuessAlias"},
        ))
        pending = await list_auto_fix_pending(status="pending")
        assert len(pending) == 1
        row_id = pending[0]["id"]

        # The fix DID land provisionally (commit + file edited).
        applied_src = (fake_repo / "naukri_server" / "services" / "search_service.py").read_text()
        assert "wrongGuessAlias" in applied_src

        # 2. Verify with a revalidate that says the endpoint is STILL broken.
        async def still_drifting(_constant):
            return False

        with patch("naukri_server.healing.t2_autofix.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await t2_autofix.verify_pending_row(
                fake_repo, pending[0], still_drifting,
            )

        # 3. The bad fix MUST be reverted.
        assert result.reverted is True
        assert result.confirmed is False
        assert result.revert_commit_sha is not None

        emitted = [c for c in mock_emit.call_args_list
                   if isinstance(c.args[0], AutoFixReverted)]
        assert len(emitted) == 1, "AutoFixReverted must fire on revert"

        row = await get_auto_fix_pending(row_id)
    assert row["status"] == "reverted"

    # 4. The file is back to its original content — the bad edit is GONE.
    final_src = (fake_repo / "naukri_server" / "services" / "search_service.py").read_text()
    assert "wrongGuessAlias" not in final_src
    assert final_src == original_src


# ===========================================================================
# *** T1 BAD fix is REVERTED *** — synchronous smoke-check safety (joint #4)
# ===========================================================================


@pytest.mark.asyncio
async def test_t1_smoke_check_reverts_a_broken_commit(fake_repo, tmp_path):
    """SAFETY-CRITICAL (joint #4): if a T1 edit somehow produces a file that
    won't compile, the post-commit smoke check must AUTO-REVERT it so no
    broken auto-fix survives in the repo.

    We simulate corruption by making atomic_write emit invalid Python (the AST
    validation happens earlier on new_source; this models a bad/partial write).
    """
    config_path = fake_repo / "naukri_server" / "config.py"
    before_commits = _commit_count(fake_repo)

    real_atomic_write = t1_autofix.snapshot.atomic_write

    def corrupting_write(target: Path, new_source: str):
        # Write syntactically BROKEN python regardless of the validated source.
        real_atomic_write(target, "def broken(:\n    pass\n")

    with patch("naukri_server.healing.t1_autofix.snapshot.atomic_write",
               side_effect=corrupting_write):
        out = await t1_autofix.apply_t1_fix(
            fake_repo, "NOTIFICATION_FEED_API", drift_type="url",
            new_url="/notif/v9/feed", skip_emit=True,
        )

    # The fix must report NOT applied, and mention the smoke check + revert.
    assert out.applied is False
    assert "smoke check failed" in (out.error or "")
    assert "auto-reverted" in (out.error or "")

    # Net effect on the repo: the broken edit is reverted away. HEAD advanced by
    # the commit + the revert (2 commits), but the FILE content is valid again.
    final_src = config_path.read_text()
    import ast
    ast.parse(final_src)  # must be valid Python again
    assert "def broken(" not in final_src
    # The original config constant is restored by the revert.
    assert 'NOTIFICATION_FEED_API = "/notif/v0/feed"' in final_src
    assert _commit_count(fake_repo) == before_commits + 2  # commit + revert


@pytest.mark.asyncio
async def test_t1_smoke_check_disables_healer_if_revert_also_fails(fake_repo, tmp_path):
    """Worst case: smoke check fails AND the revert fails too → disable healer."""
    real_atomic_write = t1_autofix.snapshot.atomic_write

    def corrupting_write(target: Path, new_source: str):
        real_atomic_write(target, "def broken(:\n")

    from naukri_server.healing.snapshot import GitResult
    bad_revert = GitResult(ok=False, stdout="", stderr="revert conflict simulated", returncode=1)

    with patch("naukri_server.healing.t1_autofix.snapshot.atomic_write",
               side_effect=corrupting_write), \
         patch("naukri_server.healing.t1_autofix.snapshot.revert_commit",
               return_value=bad_revert):
        out = await t1_autofix.apply_t1_fix(
            fake_repo, "NOTIFICATION_FEED_API", drift_type="url",
            new_url="/notif/v9/feed", skip_emit=True,
        )

    assert out.applied is False
    assert "healer disabled" in (out.error or "")
    assert circuit.is_disabled() is True
    assert "smoke check failed" in circuit.status()["reason"]


# ===========================================================================
# Scheduler joint #3 — the t2_verify_pending task reverts a bad pending fix
# ===========================================================================


@pytest.mark.asyncio
async def test_scheduler_t2_verify_task_reverts_bad_fix(fake_repo, tmp_path):
    """The registered t2_verify_pending scheduler task must drive verification:
    a pending fix whose endpoint is still broken gets reverted via the task.

    We exercise verify_due_pending_rows (the function the task calls) with an
    injected revalidate, against a fix the router applied — proving the
    scheduler half of the safety net actually reverts.
    """
    db_path = tmp_path / "naukri.db"
    original_src = (fake_repo / "naukri_server" / "config.py").read_text()
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_auto_fix_pending
        await init_db()
        # Apply a T2 url fix via the router.
        await router.on_endpoint_drift(EndpointDriftDetected(
            constant_name="PROFILE_API", drift_type="url",
            changed_fields={KEY_NEW_URL: "/profile/v3"},
        ))
        assert len(await list_auto_fix_pending(status="pending")) == 1

        async def still_broken(_constant):
            return False

        # Drive the SAME function the scheduler task calls, with a far-future
        # cutoff so the row is due.
        from datetime import datetime, timezone, timedelta
        counts = await t2_autofix.verify_due_pending_rows(
            fake_repo, still_broken,
            now=datetime.now(timezone.utc) + timedelta(days=1),
        )
    assert counts["reverted"] == 1
    assert counts["confirmed"] == 0
    # The bad url change is reverted away.
    final_src = (fake_repo / "naukri_server" / "config.py").read_text()
    assert "/profile/v3" not in final_src
    assert final_src == original_src


@pytest.mark.asyncio
async def test_scheduler_t2_verify_task_is_registered():
    """Joint #3: the t2_verify_pending task must be wired into the production
    scheduler set (register_all), else pending fixes never get verified."""
    from naukri_server import scheduler_tasks

    class _FakeScheduler:
        def __init__(self):
            self.registered = []

        def register(self, task):
            self.registered.append(task.name)

    fake = _FakeScheduler()
    scheduler_tasks.register_all(fake)
    assert "t2_verify_pending" in fake.registered
