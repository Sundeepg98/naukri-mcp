"""Tests for naukri_server.healing.snapshot — git wrappers + atomic write.

Most tests use a real git repo (initialized via `git init` in tmp_path) so
subprocess behavior is exercised end-to-end. A few tests mock subprocess to
verify timeout/error handling we can't reliably reproduce against real git.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from naukri_server.healing import snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    """Initialize a fresh git repo for tests. Sets a fake user so commits
    don't fail with 'please tell me who you are' on a clean machine."""
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@local"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test-user"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)


def _seed_commit(repo: Path) -> str:
    """Add an initial file and commit it. Returns the commit SHA."""
    f = repo / "README.md"
    f.write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed", "--no-gpg-sign"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return sha


# ---------------------------------------------------------------------------
# head_sha
# ---------------------------------------------------------------------------


def test_head_sha_returns_commit_sha(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sha = _seed_commit(repo)

    result = snapshot.head_sha(repo)
    assert result == sha
    assert len(result) == 40  # SHA-1 hex


def test_head_sha_returns_none_on_empty_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    # No commits yet -> rev-parse HEAD fails
    assert snapshot.head_sha(repo) is None


def test_head_sha_returns_none_when_not_a_repo(tmp_path):
    """`git rev-parse HEAD` on a non-repo dir fails — must return None, not raise."""
    not_repo = tmp_path / "not_a_repo"
    not_repo.mkdir()
    assert snapshot.head_sha(not_repo) is None


# ---------------------------------------------------------------------------
# staged_diff_is_empty — the "don't sweep user WIP" guard
# ---------------------------------------------------------------------------


def test_staged_diff_is_empty_on_clean_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    _seed_commit(repo)
    assert snapshot.staged_diff_is_empty(repo) is True


def test_staged_diff_is_empty_returns_false_when_user_has_wip_staged(tmp_path):
    """SAFETY: this is the guard against absorbing user WIP into the healer commit."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    _seed_commit(repo)

    user_wip = repo / "user_wip.txt"
    user_wip.write_text("user's in-progress work\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "user_wip.txt"], check=True)

    assert snapshot.staged_diff_is_empty(repo) is False


# ---------------------------------------------------------------------------
# stage_file + commit_with_author
# ---------------------------------------------------------------------------


def test_stage_file_then_commit_creates_commit_with_healer_author(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    _seed_commit(repo)

    new_file = repo / "patched.py"
    new_file.write_text("# auto-fix change\n", encoding="utf-8")

    stage = snapshot.stage_file(repo, new_file)
    assert stage.ok, f"stage failed: {stage.stderr}"

    commit = snapshot.commit_with_author(repo, "auto-heal: SEARCH_API")
    assert commit.ok, f"commit failed: {commit.stderr}"

    # Verify author propagated correctly
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert log == snapshot.HEALER_AUTHOR


def test_commit_with_explicit_author_overrides_default(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    _seed_commit(repo)

    new_file = repo / "x.py"
    new_file.write_text("\n", encoding="utf-8")
    snapshot.stage_file(repo, new_file)

    custom = "custom-healer <custom@local>"
    snapshot.commit_with_author(repo, "custom message", author=custom)
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert log == custom


# ---------------------------------------------------------------------------
# revert_commit
# ---------------------------------------------------------------------------


def test_revert_commit_creates_revert_commit_preserving_history(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    seed_sha = _seed_commit(repo)

    # Add a second commit to revert
    f = repo / "later.txt"
    f.write_text("later\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "later.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "later", "--no-gpg-sign"],
        check=True,
    )
    later_sha = snapshot.head_sha(repo)

    result = snapshot.revert_commit(repo, later_sha)
    assert result.ok, f"revert failed: {result.stderr}"

    # History should contain seed -> later -> revert (3 commits, not 1)
    count = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert count == "3"

    # The reverted file should be gone again
    assert not (repo / "later.txt").exists()


def test_revert_commit_returns_failure_on_unknown_sha(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    _seed_commit(repo)

    result = snapshot.revert_commit(repo, "0000000000000000000000000000000000000000")
    assert result.ok is False
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Mocked subprocess paths — error / timeout
# ---------------------------------------------------------------------------


def test_run_git_handles_timeout(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=0.1)

    with patch.object(subprocess, "run", side_effect=boom):
        result = snapshot._run_git(repo, ["status"], timeout=0.1)

    assert result.ok is False
    assert "timeout" in result.stderr.lower()


def test_run_git_handles_missing_git_binary(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()

    with patch.object(subprocess, "run", side_effect=FileNotFoundError("git not found")):
        result = snapshot._run_git(repo, ["status"])

    assert result.ok is False
    assert "git not found" in result.stderr


def test_run_git_sets_terminal_prompt_zero(tmp_path):
    """Critical: GIT_TERMINAL_PROMPT=0 so headless runs never block on credentials."""
    repo = tmp_path / "r"
    repo.mkdir()

    captured_env = {}

    real_run = subprocess.run

    def spy(*args, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        # Return a successful CompletedProcess so the test continues
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    with patch.object(subprocess, "run", side_effect=spy):
        snapshot._run_git(repo, ["status"])

    assert captured_env.get("GIT_TERMINAL_PROMPT") == "0"


# ---------------------------------------------------------------------------
# atomic_write
# ---------------------------------------------------------------------------


def test_atomic_write_creates_target_and_removes_tmp(tmp_path):
    target = tmp_path / "out.py"
    snapshot.atomic_write(target, "x = 1\n")

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "x = 1\n"
    # The .healer.tmp file should have been atomically renamed away
    tmp = target.with_suffix(target.suffix + ".healer.tmp")
    assert not tmp.exists()


def test_atomic_write_creates_parent_directory(tmp_path):
    target = tmp_path / "nested" / "deeper" / "out.py"
    snapshot.atomic_write(target, "y = 2\n")
    assert target.exists()


def test_atomic_write_overwrites_existing_file(tmp_path):
    target = tmp_path / "f.py"
    target.write_text("OLD\n", encoding="utf-8")
    snapshot.atomic_write(target, "NEW\n")
    assert target.read_text(encoding="utf-8") == "NEW\n"
