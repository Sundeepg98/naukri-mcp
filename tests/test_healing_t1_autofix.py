"""Tests for naukri_server.healing.t1_autofix.

End-to-end against a real git repo in tmp_path: each test seeds a fake
naukri_server/config.py + a fake parser module under naukri_server/tools/,
runs apply_t1_fix, and inspects both the file contents and git history.

The healer's circuit breaker is enabled at the top of every test (it
defaults to disabled) and reset in the fixture teardown.
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server.healing import circuit, t1_autofix
from naukri_server.healing.t1_autofix import (
    apply_t1_fix,
    locate_field_aliases,
    locate_constant_assignment,
    patch_field_alias,
    patch_constant_url,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path):
    """Initialize a real git repo with a minimal fake naukri_server tree."""
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@local"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test-user"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)

    pkg = repo / "naukri_server"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    # fake config.py with a few API constants
    config_src = (
        '"""Fake config for healer tests."""\n\n'
        'SEARCH_API = "/jobapi/v3/search"\n'
        'NOTIFICATION_FEED_API = "/notif/v0/feed"\n'
        'AB_SALARY_API = "/ab/salary"\n'
    )
    (pkg / "config.py").write_text(config_src, encoding="utf-8")

    tools = pkg / "tools"
    tools.mkdir()
    (tools / "__init__.py").write_text("", encoding="utf-8")

    # fake notifications parser with a FIELD_ALIASES dict
    notif_src = (
        '"""Fake notifications parser."""\n\n'
        'FIELD_ALIASES = {\n'
        '    "title": ["title", "displayTitle"],\n'
        '    "id": ["id", "notificationId"],\n'
        '}\n'
    )
    (tools / "notifications.py").write_text(notif_src, encoding="utf-8")

    # Initial commit so the repo has a HEAD
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial", "--no-gpg-sign"],
        check=True,
    )

    yield repo


@pytest.fixture(autouse=True)
def enable_healer():
    """Each test starts with healer enabled; reset to disabled afterwards."""
    circuit._reset_for_tests()
    circuit.enable()
    yield
    circuit._reset_for_tests()


# ---------------------------------------------------------------------------
# AST helpers — pure-function tests, no git needed
# ---------------------------------------------------------------------------


def test_locate_field_aliases_finds_node():
    src = 'FIELD_ALIASES = {"a": ["x"]}\n'
    node = locate_field_aliases(src)
    assert node is not None


def test_locate_field_aliases_missing_returns_none():
    assert locate_field_aliases("X = 1\n") is None


def test_locate_field_aliases_handles_syntax_error():
    """Garbage source must not raise — return None and let caller fall back."""
    assert locate_field_aliases("def broken(:\n") is None


def test_patch_field_alias_appends_new_alias():
    src = 'FIELD_ALIASES = {"title": ["title", "displayTitle"]}\n'
    out = patch_field_alias(src, "title", "newTitleField")
    assert out is not None
    assert "newTitleField" in out
    # Original aliases preserved
    assert "displayTitle" in out


def test_patch_field_alias_idempotent_when_alias_exists():
    src = 'FIELD_ALIASES = {"title": ["title", "displayTitle"]}\n'
    out = patch_field_alias(src, "title", "displayTitle")
    assert out is None  # no-op signaled


def test_patch_field_alias_unknown_canonical_returns_none():
    src = 'FIELD_ALIASES = {"title": ["title"]}\n'
    out = patch_field_alias(src, "nonexistent", "x")
    assert out is None


def test_patch_field_alias_validates_via_ast_parse():
    """Result must be syntactically valid Python — patch_field_alias re-parses it."""
    src = 'FIELD_ALIASES = {"k": ["a"]}\n'
    out = patch_field_alias(src, "k", "b")
    assert out is not None
    import ast
    ast.parse(out)  # raises SyntaxError if invalid


def test_locate_constant_assignment_finds_node():
    src = 'SEARCH_API = "/jobapi/v3/search"\n'
    assert locate_constant_assignment(src, "SEARCH_API") is not None


def test_patch_constant_url_replaces_string():
    src = 'SEARCH_API = "/jobapi/v3/search"\nOTHER = "/x"\n'
    out = patch_constant_url(src, "SEARCH_API", "/jobapi/v4/search")
    assert out is not None
    assert "/jobapi/v4/search" in out
    assert "/jobapi/v3/search" not in out
    # ast.unparse normalizes string literals to single quotes — accept either
    assert ("OTHER = '/x'" in out) or ('OTHER = "/x"' in out)


def test_patch_constant_url_unknown_constant_returns_none():
    src = 'SEARCH_API = "/x"\n'
    assert patch_constant_url(src, "NOT_A_CONSTANT", "/y") is None


# ---------------------------------------------------------------------------
# apply_t1_fix — disabled / wrong-tier guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_t1_fix_skips_when_healer_disabled(fake_repo):
    circuit.disable("test")
    out = await apply_t1_fix(
        fake_repo, "NOTIFICATION_FEED_API", drift_type="field",
        canonical_key="title", new_alias="newField",
    )
    assert out.applied is False
    assert out.skipped_reason == "healer disabled"


@pytest.mark.asyncio
async def test_apply_t1_fix_refuses_t3_endpoint(fake_repo):
    """SAFETY: a T3 endpoint name reaching the T1 path must hard-fail."""
    out = await apply_t1_fix(
        fake_repo, "APPLY_WORKFLOW_API", drift_type="url", new_url="/whatever",
    )
    assert out.applied is False
    assert "T3" in (out.skipped_reason or "")


@pytest.mark.asyncio
async def test_apply_t1_fix_refuses_unmapped_endpoint(fake_repo):
    out = await apply_t1_fix(
        fake_repo, "TOTALLY_BOGUS_API", drift_type="url", new_url="/x",
    )
    assert out.applied is False
    assert "no tier mapping" in (out.skipped_reason or "")


# ---------------------------------------------------------------------------
# apply_t1_fix — pre-commit guard against user WIP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_commit_aborts_if_user_wip_staged(fake_repo):
    """SAFETY: if user has staged work, refuse to commit (don't absorb it)."""
    user_wip = fake_repo / "user_change.txt"
    user_wip.write_text("user is mid-edit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(fake_repo), "add", "user_change.txt"], check=True)

    out = await apply_t1_fix(
        fake_repo, "NOTIFICATION_FEED_API", drift_type="field",
        canonical_key="title", new_alias="newDisplayTitle",
    )
    assert out.applied is False
    assert "WIP" in (out.skipped_reason or "")
    assert out.commit_sha is None

    # And the file MUST NOT have been edited
    notif_src = (fake_repo / "naukri_server" / "tools" / "notifications.py").read_text()
    assert "newDisplayTitle" not in notif_src


# ---------------------------------------------------------------------------
# apply_t1_fix — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_t1_fix_field_drift_creates_commit(fake_repo):
    out = await apply_t1_fix(
        fake_repo, "NOTIFICATION_FEED_API", drift_type="field",
        canonical_key="title", new_alias="newDisplayTitle",
        skip_emit=True,
    )
    assert out.applied is True, f"expected applied=True, got {out}"
    assert out.commit_sha is not None and len(out.commit_sha) == 40
    assert out.file_path.endswith("notifications.py")

    # File contents updated
    notif_src = (fake_repo / "naukri_server" / "tools" / "notifications.py").read_text()
    assert "newDisplayTitle" in notif_src

    # Author matches healer signature
    log = subprocess.run(
        ["git", "-C", str(fake_repo), "log", "-1", "--format=%an <%ae>"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert "naukri-mcp-healer" in log


@pytest.mark.asyncio
async def test_apply_t1_fix_url_drift_replaces_constant(fake_repo):
    out = await apply_t1_fix(
        fake_repo, "NOTIFICATION_FEED_API", drift_type="url",
        new_url="/notif/v1/feed",
        skip_emit=True,
    )
    assert out.applied is True
    config_src = (fake_repo / "naukri_server" / "config.py").read_text()
    assert "/notif/v1/feed" in config_src
    assert "/notif/v0/feed" not in config_src


@pytest.mark.asyncio
async def test_apply_t1_fix_emits_event_on_success(fake_repo):
    """Success path emits AutoFixApplied with correct payload."""
    with patch("naukri_server.healing.t1_autofix.event_bus.emit",
               new_callable=AsyncMock) as mock_emit:
        out = await apply_t1_fix(
            fake_repo, "NOTIFICATION_FEED_API", drift_type="field",
            canonical_key="title", new_alias="anotherAlias",
        )
    assert out.applied is True
    # One emission, of type AutoFixApplied
    from naukri_server.events import AutoFixApplied
    emitted = [c for c in mock_emit.call_args_list
               if isinstance(c.args[0], AutoFixApplied)]
    assert len(emitted) == 1
    ev = emitted[0].args[0]
    assert ev.constant_name == "NOTIFICATION_FEED_API"
    assert ev.tier == "T1"
    assert ev.drift_type == "field"
    assert ev.file_path.endswith("notifications.py")


# ---------------------------------------------------------------------------
# apply_t1_fix — error / no-op paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_t1_fix_field_drift_unknown_canonical_errors(fake_repo):
    out = await apply_t1_fix(
        fake_repo, "NOTIFICATION_FEED_API", drift_type="field",
        canonical_key="not_a_real_key", new_alias="x",
    )
    assert out.applied is False
    assert out.error is not None


@pytest.mark.asyncio
async def test_apply_t1_fix_field_drift_missing_args(fake_repo):
    out = await apply_t1_fix(
        fake_repo, "NOTIFICATION_FEED_API", drift_type="field",
        # canonical_key + new_alias intentionally missing
    )
    assert out.applied is False
    assert "requires canonical_key and new_alias" in (out.error or "")


@pytest.mark.asyncio
async def test_apply_t1_fix_url_drift_missing_url(fake_repo):
    out = await apply_t1_fix(
        fake_repo, "NOTIFICATION_FEED_API", drift_type="url",
    )
    assert out.applied is False
    assert "new_url" in (out.error or "")


@pytest.mark.asyncio
async def test_apply_t1_fix_unknown_drift_type(fake_repo):
    out = await apply_t1_fix(
        fake_repo, "NOTIFICATION_FEED_API", drift_type="weird_thing",
    )
    assert out.applied is False
    assert "unknown drift_type" in (out.error or "")


@pytest.mark.asyncio
async def test_apply_t1_fix_target_file_missing_errors(fake_repo):
    """If the parser_module file isn't in the repo, error cleanly (don't crash)."""
    # AB_SALARY_API points at tools.ambitionbox_rest, which doesn't exist in our fake
    out = await apply_t1_fix(
        fake_repo, "AB_SALARY_API", drift_type="field",
        canonical_key="x", new_alias="y",
    )
    assert out.applied is False
    assert "does not exist" in (out.error or "")
