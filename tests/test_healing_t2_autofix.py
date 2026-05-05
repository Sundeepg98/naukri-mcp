"""Tests for naukri_server.healing.t2_autofix.

T2 = same edit as T1 + insert auto_fix_pending row + scheduler-driven
verification that either confirms or reverts the commit.

Tests use a real `git init` repo with seeded fake naukri_server tree (same
fixture as T1), plus a tmp_path SQLite DB so insert/list/update CRUD round-trip.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server.healing import circuit, t2_autofix


# ---------------------------------------------------------------------------
# Fixtures (same shape as T1 fixture; we redirect DB_PATH for isolation)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path):
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
        'PROFILE_API = "/profile/v2"\n',
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


# ---------------------------------------------------------------------------
# VERIFY_DELAY_SECONDS — must be a constant, not a magic number
# ---------------------------------------------------------------------------


def test_verify_delay_is_constant_not_magic():
    """SAFETY-CRITICAL: spec test #3 — the verification delay must be a named
    constant, never a magic number sprinkled in code. Importing it directly
    is the contract."""
    assert hasattr(t2_autofix, "VERIFY_DELAY_SECONDS")
    assert isinstance(t2_autofix.VERIFY_DELAY_SECONDS, int)
    # 600 = 10 minutes per spec; allow tests to override via apply_t2_fix arg
    # but the production default must be at least 5 minutes (the scheduler
    # period) and at most 1 hour (so bad fixes don't sit around all day).
    assert 300 <= t2_autofix.VERIFY_DELAY_SECONDS <= 3600


# ---------------------------------------------------------------------------
# apply_t2_fix — guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_t2_fix_skips_when_disabled(fake_repo, tmp_path):
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()
        circuit.disable("test")
        out = await t2_autofix.apply_t2_fix(
            fake_repo, "SEARCH_API", drift_type="url", new_url="/x",
        )
    assert out.applied is False
    assert out.skipped_reason == "healer disabled"


@pytest.mark.asyncio
async def test_apply_t2_fix_refuses_t1_endpoint(fake_repo, tmp_path):
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()
        out = await t2_autofix.apply_t2_fix(
            fake_repo, "NOTIFICATION_FEED_API", drift_type="url", new_url="/x",
        )
    assert out.applied is False
    assert "T1" in (out.skipped_reason or "")


@pytest.mark.asyncio
async def test_apply_t2_fix_refuses_t3_endpoint(fake_repo, tmp_path):
    """SAFETY: T3 must never reach T2."""
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()
        out = await t2_autofix.apply_t2_fix(
            fake_repo, "APPLY_WORKFLOW_API", drift_type="url", new_url="/x",
        )
    assert out.applied is False
    assert "T3" in (out.skipped_reason or "")


# ---------------------------------------------------------------------------
# apply_t2_fix — happy path: commit + pending row inserted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_t2_fix_creates_commit_and_pending_row(fake_repo, tmp_path):
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_auto_fix_pending
        await init_db()
        out = await t2_autofix.apply_t2_fix(
            fake_repo, "SEARCH_API", drift_type="field",
            canonical_key="title", new_alias="newJobTitle",
            verify_delay_seconds=60, skip_emit=True,
        )
        assert out.applied is True, f"expected applied=True, got {out}"
        assert out.commit_sha is not None
        assert out.pre_fix_sha is not None
        assert out.pre_fix_sha != out.commit_sha  # commit advanced HEAD
        assert out.pending_row_id is not None
        assert out.file_path.endswith("search_service.py")

        rows = await list_auto_fix_pending(status="pending")
    assert len(rows) == 1
    assert rows[0]["commit_sha"] == out.commit_sha
    assert rows[0]["pre_fix_sha"] == out.pre_fix_sha
    assert rows[0]["constant_name"] == "SEARCH_API"


@pytest.mark.asyncio
async def test_apply_t2_fix_emits_autofix_applied_with_t2_tier(fake_repo, tmp_path):
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()
        with patch("naukri_server.healing.t2_autofix.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            out = await t2_autofix.apply_t2_fix(
                fake_repo, "SEARCH_API", drift_type="url",
                new_url="/jobapi/v4/search",
            )
        assert out.applied is True

        from naukri_server.events import AutoFixApplied
        emitted = [c for c in mock_emit.call_args_list
                   if isinstance(c.args[0], AutoFixApplied)]
        assert len(emitted) == 1
        assert emitted[0].args[0].tier == "T2"


# ---------------------------------------------------------------------------
# apply_t2_fix — pre-commit safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_t2_fix_aborts_on_user_wip(fake_repo, tmp_path):
    user_wip = fake_repo / "user.txt"
    user_wip.write_text("wip\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(fake_repo), "add", "user.txt"], check=True)

    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_auto_fix_pending
        await init_db()
        out = await t2_autofix.apply_t2_fix(
            fake_repo, "SEARCH_API", drift_type="url", new_url="/x",
        )
        assert out.applied is False
        assert "WIP" in (out.skipped_reason or "")
        # No pending row should have been created
        rows = await list_auto_fix_pending(status="pending")
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# verify_pending_row — confirmed path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_pending_row_confirms_when_revalidate_passes(fake_repo, tmp_path):
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import (
            init_db, list_auto_fix_pending, get_auto_fix_pending,
        )
        await init_db()
        applied = await t2_autofix.apply_t2_fix(
            fake_repo, "SEARCH_API", drift_type="url",
            new_url="/jobapi/v4/search", skip_emit=True,
        )
        rows = await list_auto_fix_pending()
        assert len(rows) == 1

        async def always_healthy(_constant):
            return True

        result = await t2_autofix.verify_pending_row(
            fake_repo, rows[0], always_healthy, skip_emit=True,
        )
        assert result.confirmed is True
        assert result.reverted is False

        # DB row should now be 'confirmed'
        row = await get_auto_fix_pending(applied.pending_row_id)
    assert row["status"] == "confirmed"


# ---------------------------------------------------------------------------
# verify_pending_row — revert path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_pending_row_reverts_when_revalidate_fails(fake_repo, tmp_path):
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import (
            init_db, list_auto_fix_pending, get_auto_fix_pending,
        )
        await init_db()
        applied = await t2_autofix.apply_t2_fix(
            fake_repo, "SEARCH_API", drift_type="url",
            new_url="/jobapi/v4/search", skip_emit=True,
        )
        rows = await list_auto_fix_pending()

        async def still_drifting(_constant):
            return False

        with patch("naukri_server.healing.t2_autofix.event_bus.emit",
                   new_callable=AsyncMock) as mock_emit:
            result = await t2_autofix.verify_pending_row(
                fake_repo, rows[0], still_drifting,
            )

        assert result.reverted is True
        assert result.confirmed is False
        assert result.revert_commit_sha is not None

        # AutoFixReverted emitted with both SHAs
        from naukri_server.events import AutoFixReverted
        emitted = [c for c in mock_emit.call_args_list
                   if isinstance(c.args[0], AutoFixReverted)]
        assert len(emitted) == 1
        ev = emitted[0].args[0]
        assert ev.original_commit_sha == applied.commit_sha
        assert ev.revert_commit_sha == result.revert_commit_sha
        assert ev.constant_name == "SEARCH_API"

        # DB row marked 'reverted'
        row = await get_auto_fix_pending(applied.pending_row_id)
    assert row["status"] == "reverted"

    # The reverted commit should have undone the URL change
    config_src = (fake_repo / "naukri_server" / "config.py").read_text()
    assert "/jobapi/v3/search" in config_src
    assert "/jobapi/v4/search" not in config_src


@pytest.mark.asyncio
async def test_verify_treats_revalidate_exception_as_failure(fake_repo, tmp_path):
    """If revalidate raises, treat the endpoint as still bad and revert."""
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db, list_auto_fix_pending
        await init_db()
        await t2_autofix.apply_t2_fix(
            fake_repo, "SEARCH_API", drift_type="url",
            new_url="/jobapi/v4/search", skip_emit=True,
        )
        rows = await list_auto_fix_pending()

        async def boom(_):
            raise RuntimeError("revalidate crashed")

        result = await t2_autofix.verify_pending_row(
            fake_repo, rows[0], boom, skip_emit=True,
        )
    assert result.reverted is True


# ---------------------------------------------------------------------------
# verify_pending_row — revert FAILS (worst-case state)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_revert_failure_disables_healer(fake_repo, tmp_path):
    """If git revert itself fails, the healer must DISABLE itself + raise alert."""
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import (
            init_db, list_auto_fix_pending, get_auto_fix_pending,
        )
        await init_db()
        applied = await t2_autofix.apply_t2_fix(
            fake_repo, "SEARCH_API", drift_type="url",
            new_url="/jobapi/v4/search", skip_emit=True,
        )
        rows = await list_auto_fix_pending()

        async def still_drifting(_):
            return False

        # Mock revert_commit to fail
        from naukri_server.healing.snapshot import GitResult
        bad_revert = GitResult(ok=False, stdout="", stderr="merge conflict simulated", returncode=1)
        with patch("naukri_server.healing.t2_autofix.snapshot.revert_commit",
                   return_value=bad_revert):
            result = await t2_autofix.verify_pending_row(
                fake_repo, rows[0], still_drifting,
            )

        assert result.revert_failed is True
        assert result.reverted is False
        assert result.confirmed is False
        # Healer must now be disabled
        assert circuit.is_disabled() is True
        assert "git revert" in circuit.status()["reason"]

        row = await get_auto_fix_pending(applied.pending_row_id)
    assert row["status"] == "revert_failed"
    assert "merge conflict" in (row["error"] or "")


# ---------------------------------------------------------------------------
# verify_due_pending_rows — batch processing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_due_pending_rows_batches_outcomes(fake_repo, tmp_path):
    """Multiple pending rows: each gets verified, counts returned per outcome."""
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()

        # Create 3 pending fixes
        await t2_autofix.apply_t2_fix(
            fake_repo, "SEARCH_API", drift_type="url",
            new_url="/jobapi/v4/search", skip_emit=True,
        )
        await t2_autofix.apply_t2_fix(
            fake_repo, "PROFILE_API", drift_type="url",
            new_url="/profile/v3", skip_emit=True,
        )
        await t2_autofix.apply_t2_fix(
            fake_repo, "SEARCH_API", drift_type="field",
            canonical_key="company", new_alias="employerName",
            skip_emit=True,
        )

        # Revalidate: SEARCH healthy, PROFILE not, SEARCH-field healthy
        async def revalidate(constant):
            return constant in ("SEARCH_API",)

        # Use a verify cutoff far in the future so all rows are "due"
        from datetime import datetime, timezone, timedelta
        far_future = datetime.now(timezone.utc) + timedelta(days=1)
        counts = await t2_autofix.verify_due_pending_rows(
            fake_repo, revalidate, now=far_future,
        )

    # SEARCH_API appears twice (once URL, once field) — both confirmed by our
    # revalidate(). PROFILE_API once — reverted. So: 2 confirmed, 1 reverted.
    assert counts["confirmed"] == 2
    assert counts["reverted"] == 1
    assert counts["revert_failed"] == 0
