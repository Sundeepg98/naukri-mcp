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
    """Structured result of a T3 proposal notification.

    `suppressed` is NOT a flavour of `delivered=False` - the router reads the
    two separately. Its fallback banks a plain EndpointDrift row whenever a
    proposal could not be delivered, which is right for a failure (he must
    still hear that the endpoint drifted) and exactly wrong for a duplicate:
    routing suppressed duplicates into the fallback would convert a
    HealingProposal storm into an EndpointDrift storm and change nothing.
    A suppression is a NO-OP; only `suppressed=False, delivered=False` falls
    back. See router._notify_t3.
    """

    delivered: bool
    suppressed: bool = False
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
        delivered=False + suppressed=True when an undelivered proposal for
        this constant already exists, or delivered=False + error on failure.

    AT MOST ONE LIVE PROPOSAL PER CONSTANT. A drifted T3 endpoint STAYS
    drifted until someone edits the parser by hand - that is the definition of
    T3 - and the scheduled api_validator probe re-reports it on every run, so
    an unconditional store banks a duplicate per constant per run. That is how
    three drifted constants became 81 EndpointDrift notifications, 41% of his
    undelivered queue; this path is the same producer on the same cadence and
    is latent only because the healer circuit is off.

    DEDUPE BY EXISTING UNDELIVERED ROW keyed on `constant_name`, not by a
    `last_notified_at` column plus an interval. An interval only sets the
    storm's RATE for a condition that never clears on its own. This predicate
    is bounded by construction - one undelivered HealingProposal per constant,
    ever - needs no schema migration, and re-arms the moment the brief delivers
    the notification.

    The key is the CONSTANT, deliberately, and not something coarser: a T3
    drift on an endpoint with nothing pending must announce itself immediately
    even while a backlog for other endpoints sits undelivered.

    Fail OPEN, and report a duplicate as `suppressed`, never as a plain
    `delivered=False` - the router's fallback turns the latter into an
    EndpointDrift row, which would move the storm rather than stop it.
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
        from naukri_server.database import store_notification, has_pending_notification

        # Fail OPEN: the proposal is the payload, the dedupe is an optimisation
        # over it. A broken predicate degrades to the old noise, which is
        # recoverable; swallowing a genuine NEW proposal is not.
        try:
            if constant_name and await has_pending_notification(
                    "HealingProposal", constant_name, metadata_key="constant_name"):
                logger.debug("HealingProposal for %s already pending - suppressed",
                             constant_name)
                return T3NotifyOutcome(
                    delivered=False,
                    suppressed=True,
                    constant_name=constant_name,
                    skipped_reason=(
                        "an undelivered HealingProposal for %s is already pending"
                        % constant_name),
                )
        except Exception as exc:  # noqa: BLE001 - notify anyway
            logger.warning("HealingProposal dedupe check failed, notifying anyway: %s", exc)

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
