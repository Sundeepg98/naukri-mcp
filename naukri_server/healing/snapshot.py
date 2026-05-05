"""Git subprocess wrappers used by the healer.

All subprocess calls go through these helpers so:
  - Timeout is uniform (10s, never blocks a probe forever).
  - GIT_TERMINAL_PROMPT=0 prevents git from blocking on credential prompts.
  - Pre-commit hooks are NEVER skipped (--no-verify forbidden per CLAUDE.md).
  - The repo path is explicit (`git -C <repo>`) so callers don't depend on cwd.

This module knows NOTHING about tier dispatch or AST editing — it only does
git operations and returns structured results. The healer modules import
from here.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
HEALER_AUTHOR = "naukri-mcp-healer <healer@local>"


@dataclass(frozen=True)
class GitResult:
    """Structured outcome of a git subprocess call."""

    ok: bool
    stdout: str
    stderr: str
    returncode: int


def _run_git(repo: Path, args: list[str], *, timeout: float = DEFAULT_TIMEOUT,
              extra_env: dict[str, str] | None = None) -> GitResult:
    """Run `git -C <repo> <args>` and capture the result.

    Always sets GIT_TERMINAL_PROMPT=0 so git never tries to ask for credentials
    interactively (would block forever in headless mode). Other env vars are
    inherited; callers can override via extra_env.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra_env:
        env.update(extra_env)

    cmd = ["git", "-C", str(repo), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git %s timed out after %.1fs", " ".join(args), timeout)
        return GitResult(ok=False, stdout="", stderr="timeout", returncode=-1)
    except FileNotFoundError as exc:
        # git binary missing — surface clearly, never crash the caller
        logger.error("git binary not found: %s", exc)
        return GitResult(ok=False, stdout="", stderr=str(exc), returncode=-1)

    return GitResult(
        ok=(proc.returncode == 0),
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def head_sha(repo: Path) -> str | None:
    """Current HEAD SHA, or None if the repo is empty / git is unavailable."""
    result = _run_git(repo, ["rev-parse", "HEAD"])
    if not result.ok:
        return None
    return result.stdout.strip() or None


def staged_diff_is_empty(repo: Path) -> bool:
    """True iff `git diff --cached --quiet` returns 0 (nothing staged).

    We MUST verify this before staging an auto-fix file — if the user has
    in-flight work staged, our `git add <file>` would absorb it into the
    healer commit. Refuse to commit in that case (caller escalates to T3).
    """
    # `--quiet` makes diff exit non-zero if there are differences, zero if clean
    result = _run_git(repo, ["diff", "--cached", "--quiet"])
    # exit 0 == clean, exit 1 == staged changes exist, anything else == error
    return result.returncode == 0


def stage_file(repo: Path, file_path: Path) -> GitResult:
    """`git add <file>` — explicit single-file stage, never `git add -A`."""
    return _run_git(repo, ["add", "--", str(file_path)])


def commit_with_author(repo: Path, message: str, author: str = HEALER_AUTHOR) -> GitResult:
    """Commit currently-staged changes with the healer author.

    Pre-commit hooks are NEVER skipped — if a hook fails, the commit fails
    and the caller must escalate. We do NOT pass --no-verify per project
    policy (CLAUDE.md).
    """
    return _run_git(
        repo, ["commit", "--author", author, "-m", message],
    )


def revert_commit(repo: Path, commit_sha: str) -> GitResult:
    """`git revert <sha> --no-edit` — preserves history, opposite of reset.

    Used by T2 verification when the post-fix validator still reports drift.
    Returns the GitResult; caller reads .ok and .stderr to decide whether
    to disable the healer (revert itself failing is the worst-case state).
    """
    return _run_git(repo, ["revert", "--no-edit", commit_sha])


def atomic_write(target: Path, new_source: str) -> None:
    """Write new_source to <target>.healer.tmp then os.replace -> target.

    Atomic across Windows + POSIX. Caller validates new_source via ast.parse
    BEFORE calling this so we never write syntactically invalid Python to
    disk. Raises OSError on disk failure — caller is expected to abort the
    auto-fix and emit a notification.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".healer.tmp")
    tmp.write_text(new_source, encoding="utf-8", newline="\n")
    os.replace(str(tmp), str(target))
