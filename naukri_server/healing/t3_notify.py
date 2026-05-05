"""T3 notify — build a unified diff and store a HealingProposal notification.

T3 endpoints (apply-workflow, mark-interested, save/unsave, alerts CRUD,
settings, blocked-companies, photo, fraud reports, follow-status POSTs) are
NEVER auto-fixed. When the validator detects drift on one, this module
constructs the diff that *would* have been applied and persists it as a
high-priority notification for the user to review.

The user sees the diff in:
  - the daily brief (see notifier.py — pulls T3 proposals into the morning summary)
  - direct: naukri_get_notifications (filtered by event_type="HealingProposal")

NEVER touches disk, NEVER calls git, NEVER mutates anything beyond the
notifications table.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from naukri_server.healing import tier_registry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class T3NotifyOutcome:
    """Structured result of a T3 proposal notification."""

    delivered: bool
    notification_id: int | None = None
    constant_name: str = ""
    skipped_reason: str | None = None
    error: str | None = None


def build_unified_diff(
    file_path: str,
    before: str,
    after: str,
    *,
    n: int = 3,
) -> str:
    """Return a unified diff between `before` and `after` as a single string.

    Tags both file paths with the same `file_path` so the user sees one
    coherent header. `n` is the number of context lines (default 3).
    """
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=n,
    )
    return "".join(diff_lines)


async def notify_t3_proposal(
    constant_name: str,
    *,
    file_path: str,
    before_source: str,
    after_source: str,
    drift_summary: str = "",
) -> T3NotifyOutcome:
    """Build the diff and persist a HealingProposal notification.

    Args:
        constant_name: e.g. "APPLY_WORKFLOW_API". Used for the notification
            title and to look up the tier_registry entry for context.
        file_path: repo-relative path that *would* have been edited (for the
            diff header — the file is not actually modified).
        before_source: the current file contents
        after_source: what the healer's patch would have produced
        drift_summary: human-readable drift description (severity + key fields)

    Returns:
        T3NotifyOutcome with delivered=True + notification_id on success,
        or delivered=False + error on failure.
    """
    entry = tier_registry.tier_for(constant_name)
    if entry is None:
        return T3NotifyOutcome(
            delivered=False,
            constant_name=constant_name,
            skipped_reason=f"no tier mapping for {constant_name}",
        )

    # Belt-and-suspenders: this module is only for T3 endpoints. Catching here
    # prevents accidental call from a T1/T2 path (where auto-fix would be
    # silently downgraded to a notification, masking the bug).
    if entry.tier != tier_registry.TIER_T3:
        return T3NotifyOutcome(
            delivered=False,
            constant_name=constant_name,
            skipped_reason=f"{constant_name} is {entry.tier}, not T3 — wrong handler",
        )

    diff = build_unified_diff(file_path, before_source, after_source)
    if not diff:
        # No semantic difference — either the patch is a no-op or the AST
        # walker didn't find anything to change. Don't spam the user.
        return T3NotifyOutcome(
            delivered=False,
            constant_name=constant_name,
            skipped_reason="proposed patch is empty (no diff)",
        )

    summary = drift_summary or "(drift summary not provided)"
    body = (
        f"Drift detected on T3 endpoint {constant_name}.\n\n"
        f"Summary: {summary}\n\n"
        f"Proposed patch (NOT applied — T3 endpoints require user review):\n\n"
        f"{diff}"
    )

    try:
        from naukri_server.database import store_notification
        nid = await store_notification({
            "event_type": "HealingProposal",
            "title": f"Healing proposal: {constant_name} (T3 — review required)",
            "body": body,
            "priority": "high",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "constant_name": constant_name,
                "tier": entry.tier,
                "file_path": file_path,
                "notes": entry.notes,
            },
        })
    except Exception as exc:
        logger.warning("Failed to persist T3 HealingProposal: %s", exc)
        return T3NotifyOutcome(
            delivered=False,
            constant_name=constant_name,
            error=f"store_notification failed: {exc}",
        )

    return T3NotifyOutcome(
        delivered=True,
        notification_id=int(nid) if nid is not None else None,
        constant_name=constant_name,
    )
