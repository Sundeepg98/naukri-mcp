"""Healing router — subscribes to EndpointDriftDetected and dispatches by tier.

This is the joint that connects DETECTION (api_validator emits
EndpointDriftDetected) to REMEDIATION (t1/t2 auto-fix or t3 notify). It is the
ONLY subscriber of EndpointDriftDetected.

Dispatch policy (tier comes from tier_registry):

  T3  → always notify-only (notify_t3_proposal). Never auto-applied.
  T1  → auto-fix via t1_autofix.apply_t1_fix, but ONLY if the drift event
        carries explicit, validated fix parameters (see "fix parameters"
        below). T1 is gated by a post-commit smoke check that auto-reverts
        on failure (t1_autofix enforces this).
  T2  → auto-fix via t2_autofix.apply_t2_fix (snapshot+revert protected;
        scheduler verifies later), again ONLY with explicit fix parameters.
  unmapped constant → notify-only fallback (we never guess a tier).

================================ SAFETY ================================
The drift DETECTOR (probes/drift_detector.py) reports *what changed* as
{dotted_path: change_desc} — it does NOT know the correct remediation. It
cannot tell you that a newly-appeared field "newJobTitle" should be added as
an alias of the canonical key "title", nor what the new URL for a moved
endpoint is. Auto-applying a fix derived by *guessing* from a drift report
would be exactly the kind of unverified change the safety bar forbids.

Therefore this router will ONLY take an auto-fix path when the drift event
carries EXPLICIT fix parameters (placed in changed_fields under the reserved
keys below by an enrichment layer or a hand-built event). When those are
absent — which is the case for raw detector output today — EVERY tier,
including T1/T2, falls back to detect→notify. Conservative by construction:
ambiguous ⇒ notify, never auto-commit a guess.

Reserved changed_fields keys carrying fix parameters:
  __new_url__         -> str   (drift_type="url"): the verified new endpoint URL
  __canonical_key__   -> str   (drift_type="field"): canonical FIELD_ALIASES key
  __new_alias__       -> str   (drift_type="field"): the alias to append

When both the canonical key and alias (field) or the new url (url) are present
AND the healer circuit is enabled, T1/T2 auto-fix runs through the
verify-and-revert path. Otherwise notify_t3_proposal-style notification.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from naukri_server.events import EndpointDriftDetected
from naukri_server.framework.registry import subscriber
from naukri_server.healing import circuit, tier_registry, t1_autofix, t2_autofix, t3_notify

logger = logging.getLogger(__name__)

# Reserved keys an enrichment layer may add to EndpointDriftDetected.changed_fields
# to supply explicit, validated fix parameters. Absent ⇒ notify-only.
KEY_NEW_URL = "__new_url__"
KEY_CANONICAL = "__canonical_key__"
KEY_NEW_ALIAS = "__new_alias__"


def _repo_root() -> Path:
    """Repo root = the directory that contains the naukri_server package.

    healing/router.py -> healing/ -> naukri_server/ -> <repo root>.
    """
    return Path(__file__).resolve().parent.parent.parent


def _has_field_fix_params(event: EndpointDriftDetected) -> bool:
    cf = event.changed_fields or {}
    return bool(cf.get(KEY_CANONICAL)) and bool(cf.get(KEY_NEW_ALIAS))


def _has_url_fix_params(event: EndpointDriftDetected) -> bool:
    cf = event.changed_fields or {}
    return bool(cf.get(KEY_NEW_URL))


def _drift_summary(event: EndpointDriftDetected) -> str:
    """Human-readable one-liner for notifications."""
    real_changes = {
        k: v for k, v in (event.changed_fields or {}).items()
        if k not in (KEY_NEW_URL, KEY_CANONICAL, KEY_NEW_ALIAS)
    }
    return (
        f"severity={event.severity}, drift_type={event.drift_type}, "
        f"changed={real_changes or '(none reported)'}, "
        f"snapshot_age_days={event.snapshot_age_days:.1f}"
    )


async def _store_drift_notification(
    constant_name: str, reason: str, event: EndpointDriftDetected,
) -> None:
    """Persist a plain drift notification (used when we can't safely auto-fix).

    Lazy import + try/except so a notification failure never crashes the
    event bus dispatch.
    """
    try:
        from naukri_server.database import store_notification
        await store_notification({
            "event_type": "EndpointDrift",
            "title": f"API drift on {constant_name} ({reason})",
            "body": (
                f"Drift detected on {constant_name}.\n\n"
                f"{_drift_summary(event)}\n\n"
                f"No auto-fix applied: {reason}."
            ),
            "priority": "high" if event.severity == "removed" else "medium",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "constant_name": constant_name,
                "severity": event.severity,
                "drift_type": event.drift_type,
                "reason": reason,
            },
        })
    except Exception as exc:  # noqa: BLE001 — never crash dispatch
        logger.warning("Failed to store drift notification for %s: %s",
                       constant_name, exc)


async def _notify_t3(event: EndpointDriftDetected, repo_root: Path) -> None:
    """Build the would-be diff for a T3 endpoint and store a HealingProposal.

    If we have enough fix parameters to compute the patch the healer *would*
    have applied, show it as a diff. Otherwise fall back to a plain drift
    notification (no diff). Either way: NEVER apply.
    """
    entry = tier_registry.tier_for(event.constant_name)
    if entry is None or entry.parser_module is None:
        await _store_drift_notification(event.constant_name, "no parser module mapped", event)
        return

    # Compute before/after only if we can do so safely (params present + file exists).
    try:
        if event.drift_type == "url" and _has_url_fix_params(event):
            target = repo_root / "naukri_server" / "config.py"
            before = target.read_text(encoding="utf-8")
            after = t1_autofix.patch_constant_url(
                before, event.constant_name, event.changed_fields[KEY_NEW_URL],
            )
        elif event.drift_type == "field" and _has_field_fix_params(event):
            target = repo_root / Path(*entry.parser_module.split(".")).with_suffix(".py")
            before = target.read_text(encoding="utf-8")
            after = t1_autofix.patch_field_alias(
                before,
                event.changed_fields[KEY_CANONICAL],
                event.changed_fields[KEY_NEW_ALIAS],
            )
        else:
            after = None
            before = ""
            target = None
    except OSError as exc:
        logger.warning("T3 notify could not read target for %s: %s", event.constant_name, exc)
        after = None
        before = ""
        target = None

    if after is not None and target is not None:
        rel = str(target.relative_to(repo_root)).replace("\\", "/")
        outcome = await t3_notify.notify_t3_proposal(
            event.constant_name,
            file_path=rel,
            before_source=before,
            after_source=after,
            drift_summary=_drift_summary(event),
        )
        if not outcome.delivered:
            # diff was empty or store failed — fall back to plain notification
            await _store_drift_notification(
                event.constant_name,
                outcome.skipped_reason or outcome.error or "proposal not delivered",
                event,
            )
    else:
        await _store_drift_notification(
            event.constant_name, "T3 endpoint — manual review required", event,
        )


@subscriber(EndpointDriftDetected)
async def on_endpoint_drift(event: EndpointDriftDetected) -> None:
    """The single subscriber of EndpointDriftDetected. Routes by tier.

    Returns nothing; all outcomes are persisted (notifications / git commits /
    pending rows) and surfaced via AutoFixApplied/AutoFixReverted events. Never
    raises — the event bus isolates handlers but we defend here too so one bad
    drift event can't wedge the healer.
    """
    constant_name = event.constant_name
    if not constant_name:
        logger.warning("EndpointDriftDetected with empty constant_name — ignoring")
        return

    repo_root = _repo_root()

    try:
        entry = tier_registry.tier_for(constant_name)

        # Unmapped constant: we have NO tier classification, so we cannot make a
        # safety judgement. Notify, never auto-fix.
        if entry is None:
            logger.info("Drift on unmapped endpoint %s — notify only", constant_name)
            await _store_drift_notification(constant_name, "endpoint not in tier registry", event)
            return

        # T3 is NEVER auto-applied, regardless of params or circuit state.
        if entry.tier == tier_registry.TIER_T3:
            logger.info("Drift on T3 endpoint %s — proposal only", constant_name)
            await _notify_t3(event, repo_root)
            return

        # T1/T2 require the healer to be enabled AND explicit fix parameters.
        # Missing either ⇒ conservative fallback to notify-only.
        if circuit.is_disabled():
            logger.info("Drift on %s but healer disabled — notify only", constant_name)
            await _store_drift_notification(constant_name, "healer disabled", event)
            return

        if event.drift_type == "url":
            if not _has_url_fix_params(event):
                logger.info("Drift on %s (url) lacks explicit new_url — notify only", constant_name)
                await _store_drift_notification(
                    constant_name, "no verified new_url to apply (would be a guess)", event,
                )
                return
            new_url = event.changed_fields[KEY_NEW_URL]
            canonical_key = new_alias = None
        elif event.drift_type == "field":
            if not _has_field_fix_params(event):
                logger.info("Drift on %s (field) lacks explicit alias mapping — notify only",
                            constant_name)
                await _store_drift_notification(
                    constant_name, "no verified alias mapping to apply (would be a guess)", event,
                )
                return
            canonical_key = event.changed_fields[KEY_CANONICAL]
            new_alias = event.changed_fields[KEY_NEW_ALIAS]
            new_url = None
        else:
            logger.warning("Drift on %s has unknown drift_type=%r — notify only",
                           constant_name, event.drift_type)
            await _store_drift_notification(
                constant_name, f"unknown drift_type {event.drift_type!r}", event,
            )
            return

        # Dispatch to the correct verify-protected auto-fix handler.
        if entry.tier == tier_registry.TIER_T1:
            outcome = await t1_autofix.apply_t1_fix(
                repo_root, constant_name, drift_type=event.drift_type,
                canonical_key=canonical_key, new_alias=new_alias, new_url=new_url,
            )
            if not outcome.applied:
                logger.info("T1 auto-fix for %s not applied (%s) — escalating to notify",
                            constant_name, outcome.skipped_reason or outcome.error)
                await _store_drift_notification(
                    constant_name,
                    outcome.skipped_reason or outcome.error or "T1 auto-fix failed",
                    event,
                )
        elif entry.tier == tier_registry.TIER_T2:
            outcome = await t2_autofix.apply_t2_fix(
                repo_root, constant_name, drift_type=event.drift_type,
                canonical_key=canonical_key, new_alias=new_alias, new_url=new_url,
            )
            if not outcome.applied:
                logger.info("T2 auto-fix for %s not applied (%s) — escalating to notify",
                            constant_name, outcome.skipped_reason or outcome.error)
                await _store_drift_notification(
                    constant_name,
                    outcome.skipped_reason or outcome.error or "T2 auto-fix failed",
                    event,
                )
    except Exception as exc:  # noqa: BLE001 — defend the event bus
        logger.error("Healing router crashed on drift for %s: %s", constant_name, exc)
