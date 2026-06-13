"""Fix-synthesis (enrichment) layer — turn detected drift into a CONCRETE fix.

This is the piece that makes the wired self-healing pipeline genuinely
*auto-heal* instead of merely *notify*. The drift detector
(probes/drift_detector.py) reports RAW drift — ``{dotted_path: change_desc}`` —
and the healing router (router.py) will ONLY auto-fix when an event carries
explicit, validated fix parameters (``__canonical_key__`` / ``__new_alias__``
for field drift, ``__new_url__`` for url drift). Nothing populates those today,
so nothing auto-fixes.

This module derives those parameters FROM EVIDENCE, safely, and gates the
result behind multiple checks so a bad fix can never ship. It does NOT weaken
any existing guard — a synthesized fix still flows through the unchanged
router -> apply -> verify -> revert path (smoke check for T1, snapshot+revert
for T2).

================================ SAFETY MODEL ================================

We synthesize fixes for EXACTLY ONE bounded, deterministic drift class:

  field drift, severity "added"  ->  FIELD_ALIASES alias append

  Rationale: when Naukri renames/duplicates a JSON field (the classic
  backwards-compatible drift — e.g. a row that had ``title`` now ALSO carries
  ``jobTitle``), the correct remediation is to append the new leaf name as an
  alias of the canonical key the parser already reads. The evidence for this
  is fully observable: the *new* leaf name is in ``changed_fields`` (as
  "added"), and the canonical key + its current aliases are statically
  readable from the parser module's ``FIELD_ALIASES`` dict. We append; we
  never replace, never delete, never rewrite logic.

Everything ELSE stays notify-only — there is no safe, evidence-derived
candidate, so we do not guess:

  * field "removed"      -> a field vanished; there is no replacement name to
                            alias to. NOTIFY.
  * field "type_change"  -> a field's type changed; an alias append cannot fix
                            a type change (the parser logic must change). NOTIFY.
  * url drift            -> a schema-drift event carries no evidence of the NEW
                            url (url moves are found by the discovery probe, a
                            separate channel that does not feed fix params).
                            Synthesizing a url would be a guess. NOTIFY.
  * ambiguous field add  -> the new leaf maps to zero, or to MORE THAN ONE,
                            canonical key; or the parser has no FIELD_ALIASES;
                            or the alias already exists. NOTIFY.

The five safety mechanisms (all enforced here):

  1. Deterministic, evidence-derived synthesis only. The candidate is DERIVED
     from the observed payload + the existing FIELD_ALIASES (read via AST, no
     import / no code execution). Never free-form / LLM-generated.
  2. Dry-run confidence gate. Before a candidate is declared eligible, we
     dry-run it: apply the alias append IN MEMORY (no commit) and re-validate.
     Only a candidate that demonstrably resolves the drift in the dry-run is
     eligible. A confidence score is attached; below
     HEALING_AUTOFIX_MIN_CONFIDENCE -> notify.
  3. Reuse the existing apply->verify->revert. We return an *enriched event*;
     the unchanged router applies it through t1/t2 autofix, which still
     smoke-checks / snapshots+reverts. We do not commit here.
  4. Shadow mode (default-on). With HEALING_AUTOFIX_ENABLED false (the
     default), we SYNTHESIZE + LOG the proposed fix and store a notification,
     but return None so NOTHING is committed. Flip the flag on to let correct
     proposals actually apply.
  5. Hard scope limits. Only the single bounded class above is auto-fix
     eligible. No open-ended edits.

This module performs NO git, NO disk writes, NO network of its own — the
dry-run revalidation is an INJECTED callable so the whole module is
unit-testable with mocked probes.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from naukri_server.events import AutoFixApplied, EndpointDriftDetected
from naukri_server.framework.registry import subscriber
from naukri_server.healing import t1_autofix, tier_registry

logger = logging.getLogger(__name__)

# Reserved keys the router consumes (kept in sync with router.py). Importing the
# router here would be circular (router imports nothing from synthesis at module
# load, but synthesis is imported BY the router), so we re-declare the literals.
KEY_NEW_URL = "__new_url__"
KEY_CANONICAL = "__canonical_key__"
KEY_NEW_ALIAS = "__new_alias__"

# A dry-run revalidation callable. Given the candidate fix, re-probe the live
# endpoint and return True iff the candidate is provably sound WITHOUT
# committing anything: the newly-added field the alias targets is actually
# present in a fresh response, and no OTHER (removed / type-change) drift has
# appeared. Injected so this module performs NO I/O itself and stays fully
# unit-testable with a mocked probe. Signature: async (FixCandidate) -> bool.
DryRunValidator = Callable[["FixCandidate"], Awaitable[bool]]


@dataclass(frozen=True)
class FixCandidate:
    """A concrete, evidence-derived fix proposal for one drift event.

    Attributes:
        constant_name: the drifted endpoint constant (e.g. "SEARCH_API").
        drift_type: always "field" for now (the only synthesizable class).
        canonical_key: the FIELD_ALIASES key the new alias attaches to.
        new_alias: the newly-observed leaf field name to append as an alias.
        evidence: human-readable note on WHY this mapping was derived (the
            sibling path + canonical key it matched). Surfaced in the proposal.
        confidence: 0.0-1.0. 1.0 only when the mapping is unambiguous (the new
            leaf maps to exactly one canonical key) AND the dry-run passed.
        dry_run_passed: whether the in-memory dry-run resolved the drift.
    """

    constant_name: str
    drift_type: str
    canonical_key: str
    new_alias: str
    evidence: str = ""
    confidence: float = 0.0
    dry_run_passed: bool = False


@dataclass(frozen=True)
class EnrichmentOutcome:
    """Result of attempting to enrich a raw drift event into an applicable fix.

    Exactly one of (enriched_event, None) is meaningful to the router:
      * enriched_event is not None -> a vetted, dry-run-passing, above-threshold
        candidate that auto-fix is ENABLED for. Router should apply it.
      * enriched_event is None     -> notify-only (no candidate, low confidence,
        dry-run failed, OR shadow mode). The router takes its existing notify
        path. `candidate` and `reason` explain why (for logging / proposals).
    """

    enriched_event: EndpointDriftDetected | None
    candidate: FixCandidate | None = None
    reason: str = ""
    shadow: bool = False  # True when a good candidate was withheld by shadow mode


# ---------------------------------------------------------------------------
# Evidence extraction — pure functions over the drift payload + parser source
# ---------------------------------------------------------------------------


def _leaf(dotted_path: str) -> str:
    """Last segment of a dotted path, ignoring numeric list indices.

    "jobDetails.0.jobTitle" -> "jobTitle"; "title" -> "title".
    Numeric-only trailing segments (list indices) are skipped so we compare on
    the actual field name, not its position.
    """
    parts = [p for p in dotted_path.split(".") if not p.isdigit()]
    return parts[-1] if parts else dotted_path


def _parent(dotted_path: str) -> str:
    """Everything except the final non-index segment.

    "jobDetails.0.jobTitle" -> "jobDetails.0"; "title" -> "".
    Used to require that an added field is a SIBLING (same parent) of a field
    already known to be an alias — the evidence that they're the same datum.
    """
    parts = dotted_path.split(".")
    if len(parts) <= 1:
        return ""
    return ".".join(parts[:-1])


def read_field_aliases_from_source(source: str) -> dict[str, list[str]] | None:
    """Statically extract a top-level ``FIELD_ALIASES = {str: [str, ...]}`` dict.

    Uses ast.literal_eval on the located node's value — NO import, NO code
    execution (the parser module is never run). Returns the dict, or None if
    there is no such top-level assignment / it isn't a str->list[str] mapping.
    """
    node = t1_autofix.locate_field_aliases(source)
    if node is None or not isinstance(node.value, ast.Dict):
        return None
    try:
        raw = ast.literal_eval(node.value)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(raw, dict):
        return None
    result: dict[str, list[str]] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, list):
            return None
        if not all(isinstance(e, str) for e in v):
            return None
        result[k] = list(v)
    return result


def _all_known_aliases(aliases: dict[str, list[str]]) -> set[str]:
    """Flatten every alias (and canonical key) currently known to the parser."""
    known: set[str] = set(aliases.keys())
    for vals in aliases.values():
        known.update(vals)
    return known


def _canonical_of_alias(aliases: dict[str, list[str]], leaf: str) -> str | None:
    """Return the single canonical key that owns ``leaf`` as an alias (or is it).

    Returns None if ``leaf`` belongs to zero or to MORE THAN ONE canonical key
    (an alias shared across canonicals is ambiguous and must not be auto-fixed).
    """
    owners = [
        canonical for canonical, alias_list in aliases.items()
        if leaf == canonical or leaf in alias_list
    ]
    return owners[0] if len(owners) == 1 else None


def derive_alias_candidate(
    changed_fields: dict[str, str],
    aliases: dict[str, list[str]],
    sibling_leaves_by_parent: dict[str, set[str]] | None = None,
) -> tuple[str | None, str | None, str]:
    """Derive (canonical_key, new_alias, evidence) from drift + existing aliases.

    Two deterministic, evidence-complete patterns are accepted. Both require the
    derived mapping to be UNAMBIGUOUS (exactly one canonical key) and the batch
    to yield exactly one distinct candidate; anything else is a hard NO.

    PRIMARY — hard rename (evidence fully inside ``changed_fields``):
        A field was REMOVED whose leaf is a known alias of exactly one canonical
        key, AND a field was ADDED at the SAME dotted parent whose leaf is not
        yet known. The removed-known-alias + added-sibling pair is strong
        evidence the API renamed that field; appending the new name as an alias
        restores parsing. This is the case that actually breaks the parser (old
        name gone), so it is the highest-value fix.

    SECONDARY — additive rename (needs full-response sibling evidence):
        The API now returns BOTH the old and new names (old not removed). The
        drift report only shows the ADDED leaf; the unchanged sibling is not in
        ``changed_fields``. So this path is only taken when the caller supplies
        ``sibling_leaves_by_parent`` (built from a full response, e.g. by the
        dry-run): an added leaf whose parent also contains a known alias of
        exactly one canonical key maps to that canonical.

    Returns (None, None, reason) for every non-derivable / ambiguous case.
    """
    known = _all_known_aliases(aliases)

    added_paths = [p for p, desc in changed_fields.items() if desc == "added"]
    if not added_paths:
        return None, None, "no added fields to alias"

    removed_paths = [p for p, desc in changed_fields.items() if desc == "removed"]

    candidates: list[tuple[str, str, str]] = []  # (canonical, alias, evidence)

    # -- PRIMARY: a clean 1:1 swap at one parent ------------------------------
    # At the parent where the rename happens we require EXACTLY one removed
    # known-alias and EXACTLY one added unknown field. More than one removed
    # known-alias at a parent is potential data loss (which datum did the added
    # field replace?), so we decline rather than guess. This makes the pairing
    # unambiguous by construction.
    parents = {_parent(p) for p in added_paths} | {_parent(p) for p in removed_paths}
    for parent in parents:
        added_unknown = [
            p for p in added_paths if _parent(p) == parent and _leaf(p) not in known
        ]
        removed_known = [
            p for p in removed_paths
            if _parent(p) == parent and _canonical_of_alias(aliases, _leaf(p)) is not None
        ]
        if len(added_unknown) == 1 and len(removed_known) == 1:
            a_leaf = _leaf(added_unknown[0])
            r_leaf = _leaf(removed_known[0])
            canonical = _canonical_of_alias(aliases, r_leaf)
            candidates.append((
                canonical, a_leaf,
                f"removed known alias '{r_leaf}' and added sibling '{a_leaf}' at "
                f"'{parent or '<root>'}' (canonical '{canonical}') — rename",
            ))
        elif len(removed_known) > 1:
            # Multiple known aliases vanished at one parent — can't safely map.
            return (
                None, None,
                f"multiple known aliases removed at '{parent or '<root>'}' "
                f"{sorted(_leaf(p) for p in removed_known)} — possible data loss, "
                f"not auto-fixing",
            )

    # -- SECONDARY: additive rename, only with explicit full-response evidence -
    if sibling_leaves_by_parent:
        for added in added_paths:
            a_leaf = _leaf(added)
            if a_leaf in known:
                continue
            a_parent = _parent(added)
            sibling_leaves = sibling_leaves_by_parent.get(a_parent, set())
            owners = {
                _canonical_of_alias(aliases, s)
                for s in sibling_leaves if _canonical_of_alias(aliases, s) is not None
            }
            owners.discard(None)
            if len(owners) == 1:
                canonical = next(iter(owners))
                candidates.append((
                    canonical, a_leaf,
                    f"added '{a_leaf}' is a sibling of an existing alias of "
                    f"canonical '{canonical}' at '{a_parent or '<root>'}' — additive rename",
                ))
            elif len(owners) > 1:
                return (
                    None, None,
                    f"added '{added}' sibling of aliases for multiple canonicals "
                    f"{sorted(owners)} — ambiguous, not auto-fixing",
                )

    if not candidates:
        return None, None, "no added field maps unambiguously to a known canonical alias"

    distinct = sorted(set(candidates))
    if len(distinct) > 1:
        return (
            None, None,
            f"multiple distinct alias candidates {distinct} — ambiguous, not auto-fixing",
        )
    canonical, new_alias, evidence = distinct[0]
    return canonical, new_alias, evidence


# ---------------------------------------------------------------------------
# Synthesis + dry-run + gating
# ---------------------------------------------------------------------------


def _parser_source_for(repo_root: Path, constant_name: str) -> tuple[str | None, str]:
    """Read the parser module source for a constant's tier entry.

    Returns (source, reason). source is None when the endpoint is unmapped, has
    no parser module, or the file is unreadable (reason explains which).
    """
    entry = tier_registry.tier_for(constant_name)
    if entry is None:
        return None, f"{constant_name} not in tier registry"
    if entry.parser_module is None:
        return None, f"{constant_name} has no parser_module"
    target = repo_root / Path(*entry.parser_module.split(".")).with_suffix(".py")
    if not target.exists():
        return None, f"parser module file not found: {target}"
    try:
        return target.read_text(encoding="utf-8"), ""
    except OSError as exc:
        return None, f"could not read parser module: {exc}"


def synthesize_candidate(
    event: EndpointDriftDetected,
    repo_root: Path,
) -> tuple[FixCandidate | None, str]:
    """Derive a (pre-dry-run) FixCandidate from a raw drift event, or explain why not.

    Pure / offline: reads the parser source from disk and the event payload;
    runs NO probe and NO git. Confidence here is the *structural* confidence
    (1.0 for an unambiguous single mapping); the dry-run later confirms it.

    Returns (candidate, reason). candidate is None for every non-synthesizable
    case (see module docstring); reason is a short explanation for logs/notify.
    """
    # Scope limit: only field-"added" drift is synthesizable.
    if event.drift_type != "field":
        return None, f"drift_type '{event.drift_type}' is not auto-synthesizable (only field adds)"

    changed = event.changed_fields or {}
    # Strip any reserved keys that might already be present (defensive).
    changed = {k: v for k, v in changed.items()
               if k not in (KEY_NEW_URL, KEY_CANONICAL, KEY_NEW_ALIAS)}
    if not changed:
        return None, "no real changed fields in event"

    # A type change can never be remediated by an alias append — the parser
    # logic must change. Any type_change in the batch ⇒ notify, never half-fix.
    if any(desc.startswith("type_change") for desc in changed.values()):
        return None, "drift includes a type change — alias append can't fix type changes (notify)"

    source, reason = _parser_source_for(repo_root, event.constant_name)
    if source is None:
        return None, reason

    aliases = read_field_aliases_from_source(source)
    if aliases is None:
        return None, "parser module has no usable FIELD_ALIASES dict — nothing to extend (notify)"

    # PRIMARY derivation uses only changed_fields (the hard-rename case). The
    # SECONDARY additive case needs full-response sibling evidence, which only
    # the dry-run has, so it is not attempted at this offline stage.
    canonical, new_alias, evidence = derive_alias_candidate(changed, aliases)
    if canonical is None or new_alias is None:
        return None, evidence  # evidence holds the explanatory reason here

    # Safety: refuse if there is an ORPHAN removal — a removed known-alias that
    # is NOT the rename pair we just consumed. That means a canonical's datum was
    # genuinely dropped (data loss), which aliasing cannot fix. Only the single
    # removed field at the candidate's parent (the rename) is acceptable.
    consumed_parent = _parent(
        next(p for p in changed if _leaf(p) == new_alias and changed[p] == "added")
    )
    for p, desc in changed.items():
        if desc != "removed":
            continue
        r_leaf = _leaf(p)
        if _canonical_of_alias(aliases, r_leaf) is None:
            continue  # removal of an unknown field — irrelevant to our parser
        # A known-alias removal is only OK if it is the rename pair (same parent
        # as the added alias AND maps to the same canonical we're extending).
        if not (_parent(p) == consumed_parent
                and _canonical_of_alias(aliases, r_leaf) == canonical):
            return (
                None,
                f"drift removes known alias '{r_leaf}' unrelated to the rename — "
                f"possible data loss, not auto-fixing (notify)",
            )

    return (
        FixCandidate(
            constant_name=event.constant_name,
            drift_type="field",
            canonical_key=canonical,
            new_alias=new_alias,
            evidence=evidence,
            confidence=1.0,  # unambiguous single mapping; dry-run gates the rest
            dry_run_passed=False,
        ),
        evidence,
    )


def build_candidate_source(parser_source: str, candidate: FixCandidate) -> str | None:
    """Produce the patched parser source the candidate WOULD write (in memory).

    Reuses the exact same patch function the real apply path uses
    (t1_autofix.patch_field_alias) so the dry-run validates the identical edit.
    Returns None if the patch is a no-op / fails (which would itself mean the
    candidate is not applicable).
    """
    return t1_autofix.patch_field_alias(
        parser_source, candidate.canonical_key, candidate.new_alias,
    )


async def dry_run_candidate(
    candidate: FixCandidate,
    repo_root: Path,
    dry_run_validate: DryRunValidator,
) -> tuple[bool, str]:
    """Confirm the candidate is sound IN MEMORY, committing nothing.

    Two independent confirmations:
      1. The candidate's edit is real: applying it to the on-disk parser source
         (via the exact same patch function the real apply path uses) produces a
         valid, changed source. A no-op/failed patch ⇒ reject.
      2. The injected ``dry_run_validate`` re-probes the live endpoint and
         confirms the candidate is sound (the added field the alias targets is
         actually present in a fresh response, and no new removed/type-change
         drift appeared). Any exception ⇒ reject (conservative).

    Returns (passed, reason). NO git, NO disk write here.
    """
    source, reason = _parser_source_for(repo_root, candidate.constant_name)
    if source is None:
        return False, f"dry-run could not read parser source: {reason}"

    patched = build_candidate_source(source, candidate)
    if patched is None:
        return False, "dry-run patch was a no-op or failed (candidate not applicable)"

    try:
        ok = await dry_run_validate(candidate)
    except Exception as exc:  # noqa: BLE001 — any dry-run failure ⇒ not eligible
        logger.warning("dry-run validation raised for %s: %s — treating as failed",
                       candidate.constant_name, exc)
        return False, f"dry-run validation raised: {exc}"

    if not ok:
        return False, "dry-run did not confirm the candidate (rejected)"
    return True, "dry-run confirmed the candidate"


def _enriched_event(event: EndpointDriftDetected, candidate: FixCandidate) -> EndpointDriftDetected:
    """Clone the drift event, injecting the synthesized fix params for the router."""
    new_fields = dict(event.changed_fields or {})
    new_fields[KEY_CANONICAL] = candidate.canonical_key
    new_fields[KEY_NEW_ALIAS] = candidate.new_alias
    return EndpointDriftDetected(
        constant_name=event.constant_name,
        url=event.url,
        severity=event.severity,
        drift_type="field",
        changed_fields=new_fields,
        snapshot_age_days=event.snapshot_age_days,
    )


def _autofix_enabled() -> bool:
    """Read the shadow-mode gate live (so tests/monkeypatch see changes)."""
    from naukri_server import config
    return bool(getattr(config, "HEALING_AUTOFIX_ENABLED", False))


def _min_confidence() -> float:
    from naukri_server import config
    return float(getattr(config, "HEALING_AUTOFIX_MIN_CONFIDENCE", 0.99))


async def enrich_drift_event(
    event: EndpointDriftDetected,
    repo_root: Path,
    dry_run_validate: DryRunValidator,
) -> EnrichmentOutcome:
    """Top-level entry: turn a raw drift event into an applicable fix, or not.

    Pipeline (every gate must pass for enriched_event to be returned):
      1. synthesize a deterministic, evidence-derived candidate (scope-limited
         to field "added" -> alias append). No candidate -> notify.
      2. confidence >= HEALING_AUTOFIX_MIN_CONFIDENCE. Below -> notify.
      3. dry-run: apply in memory + revalidate; must resolve the drift. Fail -> notify.
      4. shadow gate: if HEALING_AUTOFIX_ENABLED is false, WITHHOLD (return None
         with shadow=True) so nothing commits — but the candidate is computed
         and surfaced for observation.

    The router calls this for param-less T1/T2 field events. A returned
    enriched_event flows through the UNCHANGED apply->verify->revert path.
    """
    candidate, reason = synthesize_candidate(event, repo_root)
    if candidate is None:
        return EnrichmentOutcome(enriched_event=None, candidate=None, reason=reason)

    # Confidence gate (structural). Dry-run is the empirical confirmation.
    if candidate.confidence < _min_confidence():
        return EnrichmentOutcome(
            enriched_event=None, candidate=candidate,
            reason=(f"confidence {candidate.confidence:.2f} below threshold "
                    f"{_min_confidence():.2f} — notify only"),
        )

    passed, dr_reason = await dry_run_candidate(candidate, repo_root, dry_run_validate)
    candidate = FixCandidate(
        constant_name=candidate.constant_name,
        drift_type=candidate.drift_type,
        canonical_key=candidate.canonical_key,
        new_alias=candidate.new_alias,
        evidence=candidate.evidence,
        confidence=candidate.confidence,
        dry_run_passed=passed,
    )
    if not passed:
        return EnrichmentOutcome(
            enriched_event=None, candidate=candidate,
            reason=f"dry-run failed: {dr_reason} — notify only",
        )

    # Shadow mode: a GOOD candidate exists, but auto-fix is not enabled. Withhold
    # the commit; surface the proposal. This is the safe default.
    if not _autofix_enabled():
        logger.info(
            "SHADOW: would auto-fix %s by appending alias '%s' to canonical '%s' "
            "(%s); HEALING_AUTOFIX_ENABLED is false — proposing only, not applying.",
            candidate.constant_name, candidate.new_alias, candidate.canonical_key,
            candidate.evidence,
        )
        return EnrichmentOutcome(
            enriched_event=None, candidate=candidate, shadow=True,
            reason=("shadow mode (HEALING_AUTOFIX_ENABLED=false): candidate "
                    "synthesized + dry-run passed, but not applied"),
        )

    logger.info(
        "AUTOFIX: enriching drift on %s -> append alias '%s' to canonical '%s' (%s)",
        candidate.constant_name, candidate.new_alias, candidate.canonical_key,
        candidate.evidence,
    )
    return EnrichmentOutcome(
        enriched_event=_enriched_event(event, candidate),
        candidate=candidate,
        reason="candidate synthesized, dry-run passed, auto-fix enabled",
    )


@subscriber(AutoFixApplied)
async def _absorb_added_field_after_field_fix(event: AutoFixApplied) -> None:
    """Re-snapshot an endpoint's baseline after a successful FIELD auto-fix.

    Why this is needed AND safe:

      An "added"-field fix appends an alias so the parser can read the new field
      name. It does NOT remove the added field from the API response, so the
      drift detector's recorded baseline still lacks that field — meaning the
      T2 verify pass (which re-diffs against the baseline) would otherwise see
      the still-"added" field and spuriously REVERT a correct fix.

      We only reach AutoFixApplied AFTER the dry-run proved the added field is
      real and present, so absorbing it into the baseline is the correct,
      evidence-backed update. Verification then guards against *subsequent*
      regressions (new removed/type-change drift after the fix), which remains
      meaningful — a genuinely bad later change is still reverted.

      Scope: field fixes only. URL fixes change the endpoint path (the old
      snapshot key is stale anyway) and are left to the api_validator to
      re-baseline naturally. Never raises.
    """
    if event.drift_type != "field":
        return
    try:
        from naukri_server import config as _cfg
        from naukri_server.interfaces import api_client
        from naukri_server.probes.drift_detector import detector as _detector

        try:
            path = getattr(_cfg, event.constant_name)
        except AttributeError:
            return
        if not _detector.has_snapshot(path):
            # No baseline to refresh — api_validator will snapshot on next run.
            return
        response = await api_client.get(path)
        _detector.snapshot_endpoint(path, response)
        logger.info("Re-snapshotted %s baseline after field auto-fix (absorbed new field)",
                    event.constant_name)
    except Exception as exc:  # noqa: BLE001 — never let a re-snapshot wedge the bus
        logger.warning("Post-fix re-snapshot of %s failed: %s", event.constant_name, exc)


def proposal_summary(candidate: FixCandidate, *, applied: bool, shadow: bool) -> str:
    """One-line human summary for a synthesized-fix notification body."""
    if applied:
        head = "Auto-fix applied"
    elif shadow:
        head = "Auto-fix PROPOSED (shadow mode — not applied)"
    else:
        head = "Auto-fix proposed (not applied)"
    return (
        f"{head}: append alias '{candidate.new_alias}' to FIELD_ALIASES"
        f"['{candidate.canonical_key}'] for {candidate.constant_name}. "
        f"Evidence: {candidate.evidence}. "
        f"confidence={candidate.confidence:.2f}, dry_run_passed={candidate.dry_run_passed}."
    )
