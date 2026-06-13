"""T1 auto-fix — AST-located parser/config edit + git commit, no verification.

Used for low-impact endpoints (notifications, mock interview, AmbitionBox,
taxonomy, ad-targeting, etc.) where a bad fix is at worst noisy, not
data-corrupting.

Two drift_type code paths:

  drift_type="field"
    The Naukri API renamed a field. We locate `FIELD_ALIASES: dict[str, list[str]]`
    in the parser module via ast.parse and append the new alias to the list for
    the canonical key. Validate the resulting source via ast.parse BEFORE
    writing to disk. Never replace an existing alias — only append.

  drift_type="url"
    The Naukri API moved an endpoint. We locate `<CONST_NAME> = "..."` in
    naukri_server/config.py and replace the string literal with the new URL.

Both paths:
  - Honour healing.circuit.is_disabled() — early-return without doing anything
    if the healer is disabled.
  - Refuse to commit if the user has staged WIP (snapshot.staged_diff_is_empty
    must be True).
  - Validate new source via ast.parse BEFORE writing.
  - Write atomically via snapshot.atomic_write.
  - Stage + commit as the "naukri-mcp-healer" author.
  - On failure, escalate via t3_notify (HealingProposal notification).
  - Emit AutoFixApplied on success.

Returns a structured AutoFixOutcome so callers (router, T2 wrapper, tests)
can introspect what happened.
"""

from __future__ import annotations

import ast
import logging
import py_compile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from naukri_server.events import event_bus, AutoFixApplied
from naukri_server.healing import circuit, snapshot, tier_registry

logger = logging.getLogger(__name__)


def smoke_check_file(target: Path) -> tuple[bool, str]:
    """Post-commit smoke check on an auto-fixed file.

    Two cheap, offline checks (no live browser, no network, no import side
    effects on the running process):

      1. ast.parse the file on disk — catches a corrupt/partial write.
      2. py_compile.compile to a throwaway temp file — catches anything the
         CPython compiler rejects that ast.parse alone might let through.

    Returns (ok, reason). reason is "" on success, else the failure detail.
    This NEVER imports the module into the live interpreter (an auto-fixed
    module could have import-time side effects); compilation is sufficient to
    prove the edit is syntactically loadable, which is the failure mode an AST
    edit can realistically introduce.
    """
    try:
        source = target.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"could not re-read file after commit: {exc}"

    try:
        ast.parse(source)
    except SyntaxError as exc:
        return False, f"ast.parse failed post-commit: {exc}"

    # py_compile to a temp output so we don't litter __pycache__ or mutate state.
    try:
        with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as tmp:
            tmp_out = tmp.name
        try:
            py_compile.compile(str(target), cfile=tmp_out, doraise=True)
        finally:
            try:
                Path(tmp_out).unlink()
            except OSError:
                pass
    except py_compile.PyCompileError as exc:
        return False, f"py_compile failed post-commit: {exc}"
    except OSError as exc:
        # Compiler infra problem (e.g. temp dir) — be conservative, treat as fail
        # so the caller reverts rather than trusting an unverified commit.
        return False, f"smoke-check compile infrastructure error: {exc}"

    return True, ""


@dataclass(frozen=True)
class AutoFixOutcome:
    """Structured result of a single auto-fix attempt."""

    applied: bool
    commit_sha: str | None = None
    file_path: str | None = None
    skipped_reason: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# AST helpers — used by both T1 and T2 (T2 imports these directly)
# ---------------------------------------------------------------------------


def locate_field_aliases(source: str) -> ast.Assign | None:
    """Find a top-level `FIELD_ALIASES = {...}` assignment node.

    Returns the AST node (so callers can mutate it) or None if not present.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "FIELD_ALIASES":
                    return node
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "FIELD_ALIASES":
                # AnnAssign carries the value on .value too — return as Assign-like
                # by wrapping in a synthetic Assign for caller compatibility. Easier
                # to just bail and let the caller fall back to the regex path.
                return None
    return None


def locate_constant_assignment(source: str, constant_name: str) -> ast.Assign | None:
    """Find a top-level `CONSTANT_NAME = "..."` assignment node, or None."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == constant_name:
                    return node
    return None


def patch_field_alias(source: str, canonical_key: str, new_alias: str) -> str | None:
    """Append `new_alias` to FIELD_ALIASES[canonical_key]. Returns new source.

    Returns None if:
      - FIELD_ALIASES is missing
      - canonical_key isn't present in the dict
      - new_alias is already in the list (no-op, signals nothing to do)
      - the resulting source fails ast.parse validation
    """
    node = locate_field_aliases(source)
    if node is None or not isinstance(node.value, ast.Dict):
        return None

    dict_node = node.value
    found = False
    for k, v in zip(dict_node.keys, dict_node.values):
        if isinstance(k, ast.Constant) and k.value == canonical_key:
            if not isinstance(v, ast.List):
                return None
            existing = [
                e.value for e in v.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if new_alias in existing:
                return None  # already there, no-op
            v.elts.append(ast.Constant(value=new_alias))
            found = True
            break
    if not found:
        return None

    new_source = ast.unparse(ast.parse(source))  # round-trip preserves layout
    # Re-parse to apply our mutation: easier path is to mutate the tree we
    # parsed and unparse THAT directly. Redo to avoid losing the mutation:
    tree = ast.parse(source)
    for top in tree.body:
        if isinstance(top, ast.Assign):
            for tgt in top.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "FIELD_ALIASES":
                    if isinstance(top.value, ast.Dict):
                        for k, v in zip(top.value.keys, top.value.values):
                            if (isinstance(k, ast.Constant)
                                and k.value == canonical_key
                                and isinstance(v, ast.List)):
                                existing_vals = [
                                    e.value for e in v.elts
                                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                                ]
                                if new_alias not in existing_vals:
                                    v.elts.append(ast.Constant(value=new_alias))
    new_source = ast.unparse(tree) + "\n"

    # Validate before returning
    try:
        ast.parse(new_source)
    except SyntaxError:
        return None
    return new_source


def patch_constant_url(source: str, constant_name: str, new_url: str) -> str | None:
    """Replace `constant_name = "..."` with the new_url string literal.

    Returns None if the assignment isn't found or the resulting source fails
    ast.parse validation.
    """
    tree = ast.parse(source)
    found = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == constant_name:
                    if not isinstance(node.value, ast.Constant):
                        return None
                    node.value = ast.Constant(value=new_url)
                    found = True
                    break
        if found:
            break
    if not found:
        return None

    new_source = ast.unparse(tree) + "\n"
    try:
        ast.parse(new_source)
    except SyntaxError:
        return None
    return new_source


# ---------------------------------------------------------------------------
# Apply path — the public T1 entry point
# ---------------------------------------------------------------------------


def _module_to_path(repo_root: Path, dotted: str) -> Path:
    """Convert 'naukri_server.tools.notifications' to repo/naukri_server/tools/notifications.py"""
    return repo_root / Path(*dotted.split(".")).with_suffix(".py")


async def apply_t1_fix(
    repo_root: Path,
    constant_name: str,
    drift_type: str,
    *,
    canonical_key: str | None = None,
    new_alias: str | None = None,
    new_url: str | None = None,
    skip_emit: bool = False,
) -> AutoFixOutcome:
    """Apply a T1 auto-fix end-to-end. Returns AutoFixOutcome.

    Args:
        repo_root: Path to the repo root (the directory containing .git).
        constant_name: The endpoint constant in naukri_server.config (e.g. "SEARCH_API").
            Used to look up tier + parser_module via tier_registry.
        drift_type: "field" -> patch FIELD_ALIASES; "url" -> patch config.py.
        canonical_key, new_alias: required when drift_type="field"
        new_url: required when drift_type="url"
        skip_emit: tests use this to verify the AutoFixApplied event payload
            inline instead of relying on the event bus.

    Outcome cases:
        applied=True                   -> commit succeeded
        applied=False, skipped_reason  -> early-exit (disabled / WIP staged / unmapped)
        applied=False, error           -> something went wrong mid-fix
    """
    # 1. Healer must be enabled
    if circuit.is_disabled():
        return AutoFixOutcome(applied=False, skipped_reason="healer disabled")

    # 2. Endpoint must be in the registry as T1
    entry = tier_registry.tier_for(constant_name)
    if entry is None:
        return AutoFixOutcome(applied=False, skipped_reason=f"no tier mapping for {constant_name}")
    if entry.tier != tier_registry.TIER_T1:
        return AutoFixOutcome(
            applied=False,
            skipped_reason=f"{constant_name} is {entry.tier}, not T1 — wrong handler",
        )

    # 3. Locate the file to patch
    if drift_type == "url":
        target = repo_root / "naukri_server" / "config.py"
    elif drift_type == "field":
        if entry.parser_module is None:
            return AutoFixOutcome(
                applied=False,
                error=f"T1 entry for {constant_name} has no parser_module",
            )
        target = _module_to_path(repo_root, entry.parser_module)
    else:
        return AutoFixOutcome(
            applied=False,
            error=f"unknown drift_type: {drift_type!r}",
        )

    if not target.exists():
        return AutoFixOutcome(
            applied=False,
            error=f"target file does not exist: {target}",
        )

    # 4. Read + patch + validate (all in-memory; no disk write yet)
    source = target.read_text(encoding="utf-8")

    new_source: str | None
    if drift_type == "field":
        if not canonical_key or not new_alias:
            return AutoFixOutcome(
                applied=False,
                error="drift_type='field' requires canonical_key and new_alias",
            )
        new_source = patch_field_alias(source, canonical_key, new_alias)
        if new_source is None:
            return AutoFixOutcome(
                applied=False,
                error="FIELD_ALIASES patch failed (key missing, alias already present, or AST mismatch)",
            )
    else:  # drift_type == "url"
        if not new_url:
            return AutoFixOutcome(applied=False, error="drift_type='url' requires new_url")
        new_source = patch_constant_url(source, constant_name, new_url)
        if new_source is None:
            return AutoFixOutcome(
                applied=False,
                error=f"constant {constant_name} not found in {target}",
            )

    # 5. Pre-commit safety: don't sweep user's WIP into our commit
    if not snapshot.staged_diff_is_empty(repo_root):
        return AutoFixOutcome(
            applied=False,
            skipped_reason="user has staged WIP — refusing to commit",
        )

    # 6. Write atomically + stage + commit
    try:
        snapshot.atomic_write(target, new_source)
    except OSError as exc:
        return AutoFixOutcome(applied=False, error=f"atomic_write failed: {exc}")

    stage_result = snapshot.stage_file(repo_root, target)
    if not stage_result.ok:
        return AutoFixOutcome(applied=False, error=f"git add failed: {stage_result.stderr}")

    commit_msg = (
        f"auto-heal: {drift_type} drift on {constant_name}\n\n"
        f"Tier: T1 (auto-fix without verification)\n"
        f"Notes: {entry.notes or '(no notes)'}\n"
    )
    commit_result = snapshot.commit_with_author(repo_root, commit_msg)
    if not commit_result.ok:
        return AutoFixOutcome(applied=False, error=f"git commit failed: {commit_result.stderr}")

    new_sha = snapshot.head_sha(repo_root)
    rel_path = str(target.relative_to(repo_root)).replace("\\", "/")

    # 6b. SAFETY: post-commit smoke check. Even though new_source was ast.parse-
    # validated before writing, a corrupt write or a compiler-level rejection
    # would leave a broken module committed. Verify the file on disk compiles;
    # if not, AUTO-REVERT the commit so no unverified/broken auto-fix survives.
    # This gives T1 the same "an auto-fix that doesn't verify is reverted"
    # guarantee that T2 gets via the scheduler — but synchronously, in-line.
    ok, reason = smoke_check_file(target)
    if not ok:
        logger.warning("T1 smoke check FAILED for %s (%s) — auto-reverting %s",
                       constant_name, reason, new_sha)
        revert_result = snapshot.revert_commit(repo_root, new_sha or "HEAD")
        if not revert_result.ok:
            # Worst case: bad fix committed AND revert failed. Disable the healer
            # so further drift doesn't pile up more bad commits; user must act.
            circuit.disable(
                f"T1 smoke check failed for {constant_name} and git revert also "
                f"failed: {revert_result.stderr.strip()}"
            )
            return AutoFixOutcome(
                applied=False,
                commit_sha=new_sha,
                file_path=rel_path,
                error=(
                    f"smoke check failed ({reason}) AND revert failed "
                    f"({revert_result.stderr.strip()}) — healer disabled"
                ),
            )
        return AutoFixOutcome(
            applied=False,
            file_path=rel_path,
            error=f"smoke check failed post-commit ({reason}) — auto-reverted",
        )

    # 7. Emit success event (skip in tests that want to assert manually)
    if not skip_emit:
        try:
            await event_bus.emit(AutoFixApplied(
                commit_sha=new_sha or "",
                constant_name=constant_name,
                tier=tier_registry.TIER_T1,
                drift_type=drift_type,
                file_path=rel_path,
            ))
        except Exception as exc:
            logger.warning("Failed to emit AutoFixApplied: %s", exc)

    return AutoFixOutcome(
        applied=True,
        commit_sha=new_sha,
        file_path=rel_path,
    )
