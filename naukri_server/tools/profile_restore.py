"""Put a profile section back the way a snapshot found it -- through the gate.

The write layer (``profile_sections.write_section``) takes a snapshot before
every write. This module is the other half of that bargain: the path that
reads one back and uses it.

IT NEVER TOUCHES THE TRANSPORT. Every restore is issued as an ordinary
``write_section`` call, so it inherits, unchanged, the four things that make a
profile write survivable: the ``confirm`` gate, its own pre-write snapshot,
the post-write RE-READ that decides ``verified``, and the collateral detector
that notices rows disappearing. A restore that wrote straight to
``fullprofiles_write`` would be a second, unguarded write route -- and the
first time it were used would be the moment the profile was already damaged,
which is the worst possible moment to be running unverified code.

WHAT THIS PATH CAN AND CANNOT DO -- stated, not papered over
------------------------------------------------------------
``write_section`` sends ONE row per call for a row collection. That single
fact decides everything below.

``SCALAR_BLOCK`` (summary, resumeHeadline, keySkills, careerPreferences)
    Fully restorable in one call. The section IS a set of fields inside the
    ``profile`` object, and the snapshot carries all of them.

``WHOLE_LIST`` (languages, onlineProfiles, workSamples, presentations,
    publications, patents)
    The payload for these IS the entire list, so ONE call can make the live
    list equal the snapshot list -- but only when the two lists differ in
    exactly ONE row that exists on both sides and carries the write spec id
    field. That is the case that matters in practice: undoing the write whose
    pre-write snapshot this is. Anything wider -- two or more rows moved, a
    row added since the snapshot, a row deleted since the snapshot, or a row
    the write spec cannot address -- is reported ``unsupported`` with the row
    ids, because ``_merge_whole_list`` builds its payload from the LIVE list
    and can therefore neither drop a row nor add one back under its original
    id.

``SINGLE_ROW`` (employments, educations, schools, itskills, projects,
    certifications)
    NOT restorable by this path, ever. The payload is one row, so no single
    call can certify that the whole section came back. These return
    ``unsupported`` naming exactly which row ids differ, and the caller
    restores them one at a time with ``naukri_update_profile_section``,
    passing the row id field.

AND THE ONE NO PATH CAN FIX: a row DELETED on naukri.com cannot be recreated
with its original id. Re-sending it without an id creates a NEW row with a NEW
id; anything that referenced the old one still points at nothing. A restore
can return a row CONTENT, never its identity.

NO HALF-RESTORES. When this module cannot complete a section it writes
nothing at all and says so. A partial restore reported as success is strictly
worse than a refusal: it leaves the caller believing the section is whole.
"""

from typing import Any

from naukri_server.config import logger
from naukri_server.tools.profile_sections import (
    SCALAR_ALLOWED_FIELDS,
    SCALAR_BLOCK,
    SECTION_SPECS,
    SINGLE_ROW,
    _read_profile,
    write_section,
)
from naukri_server.tools.profile_snapshot import (
    detect_id_field,
    diff_section,
    list_snapshots,
    load_snapshot,
    resolve_section,
)

#: How many snapshots ``list_profile_snapshots`` returns when asked for none.
DEFAULT_SNAPSHOT_LIMIT = 20


def _error(code: str, message: str, **extra) -> dict:
    payload = {"status": "error", "error_code": code, "message": message}
    payload.update(extra)
    return payload


def _spec_for(section: str):
    """``(spec, None)`` or ``(None, error)``. Never raises on a bad name."""
    spec = SECTION_SPECS.get(section)
    if spec is None:
        return None, _error(
            "VALIDATION_ERROR",
            "Unknown profile section %r. Restorable sections: %s"
            % (section, ", ".join(sorted(SECTION_SPECS))),
        )
    return spec, None


def _load(snapshot: str):
    """``(snapshot_dict, None)`` or ``(None, error)``.

    The traversal guard lives in ``profile_snapshot._safe_snapshot_path`` and
    is inherited whole -- its ``ValueError`` is surfaced as a validation error
    rather than swallowed, so a caller sees WHICH rule refused the name.
    """
    try:
        data = load_snapshot(snapshot)
    except ValueError as exc:
        return None, _error("VALIDATION_ERROR", str(exc), snapshot=snapshot,
                            written=False)
    except FileNotFoundError as exc:
        return None, _error("NOT_FOUND", str(exc), snapshot=snapshot,
                            written=False)
    except Exception as exc:  # noqa: BLE001 - a corrupt file must not crash
        return None, _error(
            "SNAPSHOT_UNREADABLE",
            "Snapshot %r will not parse: %s: %s"
            % (snapshot, type(exc).__name__, exc),
            snapshot=snapshot, written=False,
        )

    if not isinstance(data, dict) or not isinstance(data.get("profile"), dict):
        return None, _error(
            "SNAPSHOT_UNREADABLE",
            "Snapshot %r carries no profile object, so there is nothing to "
            "restore from it." % snapshot,
            snapshot=snapshot, written=False,
        )
    return data, None


def _scalar_block(profile: dict) -> dict:
    """The scalar field bag inside a profile READ.

    The read returns ``profile`` as an ARRAY OF ONE OBJECT (measured:
    ``_audit/_slices/naukri-profile-read-shape.md``, section 15), while a
    write body nests the same fields under a plain ``profile`` object. Both
    shapes are accepted here so a snapshot taken from either survives.
    """
    _, value, found = resolve_section(profile, "profile")
    if not found:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _scalar_fields(profile: dict, section: str) -> dict:
    """The keys THIS scalar section owns, as the snapshot has them.

    Filtered against ``SCALAR_ALLOWED_FIELDS`` because all four scalar
    sections share the ``profile`` envelope: handing ``write_section`` the
    whole block would rewrite the neighbouring sections too, and it refuses
    exactly that.
    """
    allowed = SCALAR_ALLOWED_FIELDS.get(section) or set()
    block = _scalar_block(profile)
    return {key: block[key] for key in sorted(allowed) if key in block}


def _rows(value: Any) -> list:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _all_carry(rows: list, id_field: str) -> bool:
    return bool(id_field) and all(
        row.get(id_field) not in (None, "") for row in rows)


def _index(rows: list, id_field: str) -> dict:
    return {str(row[id_field]): row for row in rows
            if row.get(id_field) not in (None, "")}


def _classify(live_rows: list, snap_rows: list, id_field: str) -> dict:
    """Which rows moved between the live read and the snapshot.

    ``id_field`` is the WRITE spec id -- the only key ``write_section`` can
    target a row by. When the rows do not carry it, a reporting id is detected
    structurally instead so the caller still learns WHICH rows differ, and
    ``addressable`` goes False so no restore is attempted through a key the
    write route cannot match on.
    """
    addressable = (_all_carry(live_rows, id_field)
                   and _all_carry(snap_rows, id_field))
    key = id_field if addressable else detect_id_field(live_rows, snap_rows)

    if not key:
        return {"id_field": None, "addressable": False, "identified": False,
                "only_live": [], "only_snapshot": [], "changed": [],
                "_live_map": {}, "_snap_map": {}}

    live_map = _index(live_rows, key)
    snap_map = _index(snap_rows, key)
    changed = [rid for rid in snap_map
               if rid in live_map and snap_map[rid] != live_map[rid]]
    return {
        "id_field": key,
        "addressable": addressable,
        "identified": True,
        "only_live": sorted(set(live_map) - set(snap_map)),
        "only_snapshot": sorted(set(snap_map) - set(live_map)),
        "changed": sorted(changed),
        "_live_map": live_map,
        "_snap_map": snap_map,
    }


def _row_guidance(moved: dict) -> str:
    """One clause per way the section moved, row ids named."""
    parts = []
    if moved["changed"]:
        parts.append("%d row(s) changed: %s"
                     % (len(moved["changed"]), ", ".join(moved["changed"])))
    if moved["only_snapshot"]:
        parts.append(
            "%d row(s) in the snapshot and GONE from the live profile: %s -- "
            "a deleted row CANNOT be recreated with its original id; "
            "re-sending it makes a NEW row with a NEW id"
            % (len(moved["only_snapshot"]), ", ".join(moved["only_snapshot"])))
    if moved["only_live"]:
        parts.append(
            "%d row(s) added since the snapshot: %s -- this path cannot "
            "delete a row, so it cannot undo those"
            % (len(moved["only_live"]), ", ".join(moved["only_live"])))
    if not parts:
        parts.append("no rows differ")
    return "; ".join(parts)


def _restorability(spec, section: str, live: dict, snap_profile: dict):
    """``(restorable, reason)`` -- the same test ``restore_section`` applies."""
    if spec.kind == SCALAR_BLOCK:
        fields = _scalar_fields(snap_profile, section)
        if not fields:
            return False, ("the snapshot carries none of the fields %r owns"
                           % section)
        return True, ("scalar section: all %d field(s) go back in one write"
                      % len(fields))

    if spec.kind == SINGLE_ROW:
        return False, (
            "%r is a row collection written ONE ROW PER CALL, so no single "
            "write can restore the whole section" % section)

    _, live_value, _ = resolve_section(live, section)
    _, snap_value, _ = resolve_section(snap_profile, section)
    moved = _classify(_rows(live_value), _rows(snap_value), spec.id_field)
    if not moved["identified"]:
        return False, "the rows in this section cannot be identified by id"
    if not moved["addressable"]:
        return False, (
            "the rows are keyed by %r in the profile read, but the write spec "
            "targets %r -- this path will not guess a mapping between them"
            % (moved["id_field"], spec.id_field))
    if moved["only_live"] or moved["only_snapshot"] or len(moved["changed"]) > 1:
        return False, _row_guidance(moved)
    if not moved["changed"]:
        return False, "no rows differ; the section already matches the snapshot"
    return True, ("whole-list section differing in exactly one row (%s), so "
                  "one write restores the entire list" % moved["changed"][0])


def _section_diff(spec, section: str, live: dict, snap_profile: dict) -> dict:
    """Diff ONE section, LIVE -> SNAPSHOT, at the granularity that is written.

    A scalar section is NOT a top-level key of the profile read: all four of
    them live inside the ``profile`` object, which is why ``write_section``
    verifies against ``spec.envelope`` rather than the section name. Diffing
    by section name here would resolve to nothing on both sides and report an
    empty change set for a restore that does change something. So the scalar
    case diffs the OWNED FIELDS ONLY, wrapped back in their envelope -- the
    neighbouring scalar sections share that object and are none of this
    section's business.
    """
    if spec.kind == SCALAR_BLOCK:
        return diff_section(
            {spec.envelope: _scalar_fields(live, section)},
            {spec.envelope: _scalar_fields(snap_profile, section)},
            spec.envelope,
        )
    return diff_section(live, snap_profile, section)


# ---------------------------------------------------------------------------
# 1. Read-only: what would a restore change?
# ---------------------------------------------------------------------------

async def preview_restore(snapshot: str, section: str) -> dict:
    """What restoring *section* from *snapshot* would change. WRITES NOTHING.

    Args:
        snapshot: a bare snapshot filename from ``list_profile_snapshots``.
        section: a key of ``SECTION_SPECS``, in either spelling.

    Returns:
        ``{status: "preview", would_change: <diff>, taken_at, ...}``. The diff
        runs LIVE -> SNAPSHOT, so ``changed[].to`` is the value a restore
        would put back. ``restorable`` says whether ``restore_section`` could
        actually carry it out, and ``reason`` says why not when it could not.
    """
    spec, err = _spec_for(section)
    if err:
        return err
    data, err = _load(snapshot)
    if err:
        return err

    snap_profile = data["profile"]
    if spec.kind == SCALAR_BLOCK:
        present = bool(_scalar_fields(snap_profile, section))
    else:
        _, _, present = resolve_section(snap_profile, section)
    if not present:
        return _error(
            "SECTION_ABSENT",
            "Snapshot %r does not carry section %r, so there is nothing to "
            "restore. Sections in that snapshot: %s"
            % (snapshot, section, ", ".join(data.get("sections") or [])),
            snapshot=snapshot, section=section, written=False,
        )

    live = await _read_profile()
    diff = _section_diff(spec, section, live, snap_profile)
    restorable, reason = _restorability(spec, section, live, snap_profile)
    return {
        "status": "preview",
        "snapshot": snapshot,
        "taken_at": data.get("taken_at"),
        "label": data.get("label"),
        "section": section,
        "kind": spec.kind,
        "would_change": diff,
        "restorable": restorable,
        "reason": reason,
        "written": False,
        "message": "Read-only. Nothing was written. `would_change` is the "
                   "diff from the LIVE profile to the snapshot -- what a "
                   "restore would put back.",
    }


# ---------------------------------------------------------------------------
# 2. The restore itself
# ---------------------------------------------------------------------------

async def restore_section(snapshot: str, section: str,
                          confirm: bool = False) -> dict:
    """Restore one section from *snapshot* THROUGH ``write_section``.

    Args:
        snapshot: bare snapshot filename. Anything that could escape the
            snapshot directory is refused by the ``load_snapshot`` guard.
        section: a key of ``SECTION_SPECS``.
        confirm: False (the default) writes NOTHING and returns the exact
            body that would be sent.

    Returns:
        Whatever ``write_section`` returned -- ``preview`` / ``updated`` /
        ``error`` -- with the restore provenance attached. Or
        ``{status: "unsupported"}`` when this path cannot restore the section
        completely, which names the differing row ids and writes nothing.
    """
    spec, err = _spec_for(section)
    if err:
        return err
    data, err = _load(snapshot)
    if err:
        return err

    snap_profile = data["profile"]
    provenance = {
        "restored_from": snapshot,
        "taken_at": data.get("taken_at"),
        "snapshot_label": data.get("label"),
        "section": section,
        "kind": spec.kind,
    }

    if spec.kind == SCALAR_BLOCK:
        fields = _scalar_fields(snap_profile, section)
        if not fields:
            return _error(
                "SECTION_ABSENT",
                "Snapshot %r carries none of the fields %r owns (%s), so "
                "there is nothing to restore."
                % (snapshot, section,
                   ", ".join(sorted(SCALAR_ALLOWED_FIELDS.get(section) or []))),
                written=False, **provenance)
        return await _delegate(section, fields, confirm, provenance)

    _, snap_value, snap_found = resolve_section(snap_profile, section)
    if not snap_found:
        return _error(
            "SECTION_ABSENT",
            "Snapshot %r does not carry section %r, so there is nothing to "
            "restore. Sections in that snapshot: %s"
            % (snapshot, section, ", ".join(data.get("sections") or [])),
            written=False, **provenance)

    live = await _read_profile()
    _, live_value, _ = resolve_section(live, section)
    moved = _classify(_rows(live_value), _rows(snap_value), spec.id_field)
    detail = {
        "differing_rows": {
            "id_field": moved["id_field"],
            "changed": moved["changed"],
            "only_in_snapshot": moved["only_snapshot"],
            "only_in_live_profile": moved["only_live"],
        },
    }

    if spec.kind == SINGLE_ROW:
        return _unsupported(
            "%r is written ONE ROW PER CALL, so this path cannot restore the "
            "whole section atomically and will not half-restore it. NOTHING "
            "WAS WRITTEN. What differs: %s. Restore them one at a time with "
            "naukri_update_profile_section(section=%r, fields={%r: <row id>, "
            "...}), taking each row values from the snapshot. A row DELETED "
            "on naukri.com cannot be recreated with its original id -- "
            "re-sending it creates a NEW row with a NEW id."
            % (section, _row_guidance(moved), section, spec.id_field),
            provenance, detail)

    restorable, reason = _restorability(spec, section, live, snap_profile)
    if not restorable:
        return _unsupported(
            "%r cannot be restored in a single write: %s. NOTHING WAS "
            "WRITTEN. Restore the rows one at a time with "
            "naukri_update_profile_section, and note that a row deleted on "
            "naukri.com cannot come back with its original id."
            % (section, reason),
            provenance, detail)

    row_id = moved["changed"][0]
    tagged = dict(provenance, restored_row=row_id, **detail)
    return await _delegate(section, dict(moved["_snap_map"][row_id]), confirm,
                           tagged)


def _unsupported(message: str, provenance: dict, detail: dict) -> dict:
    payload = {"status": "unsupported", "written": False, "message": message}
    payload.update(provenance)
    payload.update(detail)
    return payload


async def _delegate(section: str, fields: dict, confirm: bool,
                    provenance: dict) -> dict:
    """Hand the restore to ``write_section`` and tag the result.

    The confirm gate, the pre-write snapshot, the re-read verification and the
    collateral detector all live there. Nothing in this module writes.
    """
    result = await write_section(
        section=section, fields=fields, confirm=confirm,
        label="pre-restore-%s" % section,
    )
    if not isinstance(result, dict):  # pragma: no cover - defensive
        logger.warning("write_section returned %s, not a dict",
                       type(result).__name__)
        return result
    out = dict(result)
    out.update(provenance)
    out["restore"] = True
    if out.get("status") == "preview":
        out["message"] = (
            "Nothing was written. This is the exact body a restore of %r from "
            "%s would send. Re-run with confirm=True."
            % (section, provenance.get("restored_from")))
    return out


# ---------------------------------------------------------------------------
# 3. Listing what is available to restore from
# ---------------------------------------------------------------------------

async def list_profile_snapshots(limit: int = DEFAULT_SNAPSHOT_LIMIT) -> dict:
    """Snapshot METADATA, newest first. Never any profile content.

    ``profile_snapshot.list_snapshots`` returns file name, timestamp, label,
    size, section COUNT and readability -- deliberately no section values and
    no section names, so listing what can be restored never prints an
    employment history into a transcript.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return _error("VALIDATION_ERROR",
                      "limit must be an integer of at least 1, got %r"
                      % (limit,))
    entries = list_snapshots()
    return {
        "status": "success",
        "total": len(entries),
        "returned": min(limit, len(entries)),
        "snapshots": entries[:limit],
        "message": "Metadata only -- no profile content is returned here. "
                   "Pass a `file` value to naukri_restore_profile_section.",
    }
