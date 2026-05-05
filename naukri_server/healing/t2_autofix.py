"""T2 auto-fix — same edit as T1 + snapshot+revert verification.

Used for endpoints whose responses feed agent decisions (search, recommendations,
job detail, match score, applied/saved jobs, dashboard, profile, inbox reads).
A bad fix here would silently change which jobs the agent applies to, so every
T2 fix is provisional until the post-apply validator confirms the endpoint is
healthy ~10 minutes later.

Flow:
  1. Reuse T1 AST + git logic, but pass tier="T2" through.
  2. BEFORE the auto-fix commit, capture pre_fix_sha = head_sha(repo).
  3. AFTER the commit succeeds, INSERT a row into auto_fix_pending with
     status='pending' and verify_at = applied_at + verify_delay_seconds.
  4. The t2_verify_pending scheduler task (scheduler_tasks.py) walks pending
     rows whose verify_at has passed and runs verify_pending_row() below.

The scheduler-driven verification is split out (verify_pending_row) so tests
can drive it directly without spinning up a real scheduler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from naukri_server.events import event_bus, AutoFixApplied, AutoFixReverted
from naukri_server.healing import circuit, snapshot, tier_registry, t1_autofix

logger = logging.getLogger(__name__)


# Constant — NOT a magic number sprinkled in code. Tests assert this is used.
# 10 minutes = enough for Naukri's CDN to settle, short enough that a bad
# T2 fix doesn't sit in production for hours.
VERIFY_DELAY_SECONDS: int = 600


@dataclass(frozen=True)
class T2ApplyOutcome:
    """Result of a single T2 fix attempt (apply phase only)."""

    applied: bool
    commit_sha: str | None = None
    pre_fix_sha: str | None = None
    pending_row_id: int | None = None
    file_path: str | None = None
    skipped_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class T2VerifyOutcome:
    """Result of verifying a pending T2 row."""

    confirmed: bool
    reverted: bool
    revert_failed: bool
    revert_commit_sha: str | None = None
    error: str | None = None


def _module_to_path(repo_root: Path, dotted: str) -> Path:
    """Same convention as t1_autofix._module_to_path (re-implemented to avoid
    importing a private symbol)."""
    return repo_root / Path(*dotted.split(".")).with_suffix(".py")


# ---------------------------------------------------------------------------
# Apply phase
# ---------------------------------------------------------------------------


async def apply_t2_fix(
    repo_root: Path,
    constant_name: str,
    drift_type: str,
    *,
    canonical_key: str | None = None,
    new_alias: str | None = None,
    new_url: str | None = None,
    skip_emit: bool = False,
    verify_delay_seconds: int = VERIFY_DELAY_SECONDS,
) -> T2ApplyOutcome:
    """Apply a T2 auto-fix end-to-end. Returns T2ApplyOutcome.

    Same arg semantics as t1_autofix.apply_t1_fix, plus:
      verify_delay_seconds: override for tests (production = VERIFY_DELAY_SECONDS = 600).
    """
    if circuit.is_disabled():
        return T2ApplyOutcome(applied=False, skipped_reason="healer disabled")

    entry = tier_registry.tier_for(constant_name)
    if entry is None:
        return T2ApplyOutcome(applied=False, skipped_reason=f"no tier mapping for {constant_name}")
    if entry.tier != tier_registry.TIER_T2:
        return T2ApplyOutcome(
            applied=False,
            skipped_reason=f"{constant_name} is {entry.tier}, not T2 — wrong handler",
        )

    # Capture pre-fix SHA BEFORE any commit so we can revert if needed.
    pre_fix_sha = snapshot.head_sha(repo_root)
    if pre_fix_sha is None:
        return T2ApplyOutcome(
            applied=False,
            error="cannot determine HEAD sha — repo empty or git unavailable",
        )

    # Locate target file
    if drift_type == "url":
        target = repo_root / "naukri_server" / "config.py"
    elif drift_type == "field":
        if entry.parser_module is None:
            return T2ApplyOutcome(
                applied=False,
                error=f"T2 entry for {constant_name} has no parser_module",
            )
        target = _module_to_path(repo_root, entry.parser_module)
    else:
        return T2ApplyOutcome(applied=False, error=f"unknown drift_type: {drift_type!r}")

    if not target.exists():
        return T2ApplyOutcome(
            applied=False, error=f"target file does not exist: {target}",
        )

    # Read + patch + validate
    source = target.read_text(encoding="utf-8")
    if drift_type == "field":
        if not canonical_key or not new_alias:
            return T2ApplyOutcome(
                applied=False,
                error="drift_type='field' requires canonical_key and new_alias",
            )
        new_source = t1_autofix.patch_field_alias(source, canonical_key, new_alias)
        if new_source is None:
            return T2ApplyOutcome(
                applied=False,
                error="FIELD_ALIASES patch failed (key missing, alias already present, or AST mismatch)",
            )
    else:
        if not new_url:
            return T2ApplyOutcome(applied=False, error="drift_type='url' requires new_url")
        new_source = t1_autofix.patch_constant_url(source, constant_name, new_url)
        if new_source is None:
            return T2ApplyOutcome(
                applied=False, error=f"constant {constant_name} not found in {target}",
            )

    if not snapshot.staged_diff_is_empty(repo_root):
        return T2ApplyOutcome(
            applied=False,
            skipped_reason="user has staged WIP — refusing to commit",
        )

    try:
        snapshot.atomic_write(target, new_source)
    except OSError as exc:
        return T2ApplyOutcome(applied=False, error=f"atomic_write failed: {exc}")

    stage_result = snapshot.stage_file(repo_root, target)
    if not stage_result.ok:
        return T2ApplyOutcome(applied=False, error=f"git add failed: {stage_result.stderr}")

    commit_msg = (
        f"auto-heal: {drift_type} drift on {constant_name}\n\n"
        f"Tier: T2 (snapshot+revert protected; verifies in {verify_delay_seconds}s)\n"
        f"Notes: {entry.notes or '(no notes)'}\n"
    )
    commit_result = snapshot.commit_with_author(repo_root, commit_msg)
    if not commit_result.ok:
        return T2ApplyOutcome(applied=False, error=f"git commit failed: {commit_result.stderr}")

    new_sha = snapshot.head_sha(repo_root)
    if new_sha is None:
        return T2ApplyOutcome(
            applied=False, error="HEAD sha unreadable after commit — aborting",
        )

    rel_path = str(target.relative_to(repo_root)).replace("\\", "/")

    # Insert pending verification row
    now = datetime.now(timezone.utc)
    verify_at = (now + timedelta(seconds=verify_delay_seconds)).isoformat()
    try:
        from naukri_server.database import insert_auto_fix_pending
        row_id = await insert_auto_fix_pending(
            commit_sha=new_sha,
            pre_fix_sha=pre_fix_sha,
            constant_name=constant_name,
            applied_at=now.isoformat(),
            verify_at=verify_at,
        )
    except Exception as exc:
        # If we can't track the pending row, the snapshot+revert protection is
        # gone — so revert the commit immediately to keep the safety contract.
        logger.warning("insert_auto_fix_pending failed: %s — reverting commit %s",
                       exc, new_sha)
        snapshot.revert_commit(repo_root, new_sha)
        return T2ApplyOutcome(
            applied=False,
            error=f"insert_auto_fix_pending failed: {exc} (auto-revert attempted)",
        )

    if not skip_emit:
        try:
            await event_bus.emit(AutoFixApplied(
                commit_sha=new_sha,
                constant_name=constant_name,
                tier=tier_registry.TIER_T2,
                drift_type=drift_type,
                file_path=rel_path,
            ))
        except Exception as exc:
            logger.warning("Failed to emit AutoFixApplied (T2): %s", exc)

    return T2ApplyOutcome(
        applied=True,
        commit_sha=new_sha,
        pre_fix_sha=pre_fix_sha,
        pending_row_id=row_id,
        file_path=rel_path,
    )


# ---------------------------------------------------------------------------
# Verify phase — called by the scheduler task
# ---------------------------------------------------------------------------


async def verify_pending_row(
    repo_root: Path,
    row: dict,
    revalidate: Callable[[str], Awaitable[bool]],
    *,
    skip_emit: bool = False,
) -> T2VerifyOutcome:
    """Verify a single pending T2 row.

    Args:
        repo_root: Path to the repo (for git revert).
        row: dict from list_auto_fix_pending — has commit_sha, constant_name, id.
        revalidate: async callable that returns True if the endpoint is now
            healthy. Caller injects this (typically a closure around
            api_validator.validate_endpoint) so this module stays
            dependency-free for testing.
        skip_emit: tests use this to verify event payloads inline.

    Outcome cases:
        confirmed=True            -> endpoint healthy, mark status='confirmed'
        reverted=True             -> endpoint still bad, git-revert succeeded,
                                      mark status='reverted', emit AutoFixReverted
        revert_failed=True        -> revert itself failed, healer DISABLED,
                                      mark status='revert_failed', high-priority notif
    """
    constant_name = row["constant_name"]
    commit_sha = row["commit_sha"]
    row_id = row["id"]

    # Re-validate the endpoint
    try:
        is_healthy = await revalidate(constant_name)
    except Exception as exc:
        # Revalidation itself crashed — be conservative, treat as failure.
        is_healthy = False
        logger.warning("Revalidation of %s raised: %s — treating as failed",
                       constant_name, exc)

    from naukri_server.database import update_auto_fix_status

    if is_healthy:
        await update_auto_fix_status(row_id, "confirmed")
        return T2VerifyOutcome(confirmed=True, reverted=False, revert_failed=False)

    # Endpoint still drifted — revert
    revert_result = snapshot.revert_commit(repo_root, commit_sha)
    if not revert_result.ok:
        # Revert failure is the worst case: the bad fix is still in HEAD AND
        # we can't undo it. Disable the healer so further drift events don't
        # pile up more bad fixes; the user must intervene.
        circuit.disable(
            f"git revert of auto-fix {commit_sha} failed: {revert_result.stderr.strip()}"
        )
        await update_auto_fix_status(
            row_id, "revert_failed", error=revert_result.stderr.strip(),
        )
        try:
            from naukri_server.database import store_notification
            await store_notification({
                "event_type": "HealerCriticalFailure",
                "title": f"Healer git-revert FAILED on {constant_name}",
                "body": (
                    f"Auto-fix commit {commit_sha} for {constant_name} could not be "
                    f"reverted. Healer disabled. Manual intervention required.\n\n"
                    f"git stderr:\n{revert_result.stderr}"
                ),
                "priority": "high",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "metadata": {
                    "constant_name": constant_name,
                    "commit_sha": commit_sha,
                    "row_id": row_id,
                },
            })
        except Exception as exc:
            logger.warning("Failed to store HealerCriticalFailure notification: %s", exc)
        return T2VerifyOutcome(
            confirmed=False, reverted=False, revert_failed=True,
            error=revert_result.stderr.strip(),
        )

    # Revert succeeded
    revert_sha = snapshot.head_sha(repo_root)
    await update_auto_fix_status(row_id, "reverted")

    if not skip_emit:
        try:
            await event_bus.emit(AutoFixReverted(
                original_commit_sha=commit_sha,
                revert_commit_sha=revert_sha or "",
                constant_name=constant_name,
                reason="post-fix validation still reported drift",
            ))
        except Exception as exc:
            logger.warning("Failed to emit AutoFixReverted: %s", exc)

    return T2VerifyOutcome(
        confirmed=False, reverted=True, revert_failed=False,
        revert_commit_sha=revert_sha,
    )


async def verify_due_pending_rows(
    repo_root: Path,
    revalidate: Callable[[str], Awaitable[bool]],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Walk all pending rows whose verify_at has passed and process each.

    Returns a counts dict {confirmed, reverted, revert_failed} for the
    scheduler / daily brief to surface.
    """
    cutoff = (now or datetime.now(timezone.utc)).isoformat()
    from naukri_server.database import list_auto_fix_pending
    rows = await list_auto_fix_pending(status="pending", due_before=cutoff)
    counts = {"confirmed": 0, "reverted": 0, "revert_failed": 0}
    for row in rows:
        outcome = await verify_pending_row(repo_root, row, revalidate)
        if outcome.confirmed:
            counts["confirmed"] += 1
        elif outcome.reverted:
            counts["reverted"] += 1
        elif outcome.revert_failed:
            counts["revert_failed"] += 1
    return counts
