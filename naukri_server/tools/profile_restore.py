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
``write_section`` sends ONE row per call for a row collection. That is a
TRANSACTION SHAPE, not a limit on reach, and this module now says so with
writes instead of with a refusal. Where it used to decline every row
collection outright, it restores them ROW BY ROW and reports each row.

``SCALAR_BLOCK`` (summary, resumeHeadline, keySkills, careerPreferences)
    Fully restorable in ONE call, and atomic because it is one write. The
    section IS a set of fields inside the ``profile`` object and the snapshot
    carries all of them.

``SINGLE_ROW`` (employments, educations, schools, itskills, projects,
    certifications) and
``WHOLE_LIST`` (languages, onlineProfiles, workSamples, presentations,
    publications, patents)
    Restored one row per ``write_section`` call, in id order. Every row that
    differs between the live profile and the snapshot AND exists on both sides
    is written back. The two kinds differ only in what goes on the wire --
    a single-row section sends the one edited row, a whole-list section sends
    the merged whole list -- and that difference is entirely
    ``write_section``'s business, not this module's.

    NOT ATOMIC, AND IT NEVER PRETENDS TO BE. N differing rows are N writes
    with no transaction and no rollback around them. So: the run STOPS at the
    first row that does not land; every row comes back with its own outcome;
    and the status is ``partial`` -- never ``updated`` -- unless every planned
    row landed and nothing was skipped.

THE FOUR ROW OUTCOMES, because three would have to lie about one of them:
    ``landed``    ``write_section`` re-read the profile and SAW the row.
    ``not_sent``  refused before the transport. The only outcome that
                  certifies nothing reached Naukri.
    ``unknown``   dispatched and NOT confirmed -- a transport exception, which
                  cannot prove the server did not apply it, or a write Naukri
                  accepted that the re-read cannot see. Neither a success nor
                  a "nothing happened", and it is reported as neither.
    ``not_attempted``  never sent, because an earlier row stopped the run.

WHAT IS STILL REFUSED, and why it is not a shape problem
    - A row DELETED on naukri.com cannot be recreated with its original id.
      Re-sending it without an id creates a NEW row with a NEW id; anything
      that referenced the old one still points at nothing. A restore can
      return a row's CONTENT, never its identity. Those rows are SKIPPED by
      name, and their presence alone keeps the result off ``updated``.
    - A row ADDED since the snapshot cannot be removed. Deleting a row is a
      different route entirely (a per-row POST carrying
      ``X-HTTP-Method-Override: DELETE``, see ``profile_write``) which this
      server does not implement at all.
    - Rows the write spec cannot address by id. Guessing a key mapping is how
      a restore writes to the wrong row.

NO SILENT HALF-RESTORES. Partial is now a thing this module DOES -- it is not
a thing it HIDES. Every partial result names the rows that landed, the rows
that did not, and the rows it never tried.

THE SOURCE SNAPSHOT IS NEVER TOUCHED. It is read ONCE, into memory, before
any write, and every row is restored from that in-memory copy. Each
``write_section`` call takes its own fresh pre-write snapshot under a
different, collision-safe filename, so an N-row restore leaves N new
snapshots beside the source and overwrites nothing. A restore that failed at
row 3 can be retried from the same source file, unchanged.
"""

from typing import Any

from naukri_server.config import logger
from naukri_server.tools.profile_sections import (
    SCALAR_ALLOWED_FIELDS,
    SCALAR_BLOCK,
    SECTION_SPECS,
    _read_profile,
    write_section,
)
# classify_section_write reads a `write_section` reply and says what actually
# happened to it. It belongs beside `write_section` in profile_sections.py and
# is not there only because that file is another owner's in this slice; it
# lives in profile_update.py so the update path and the restore path grade a
# write by the SAME rule. Two graders would eventually disagree, and the one
# that was wrong would be reporting a write as landed when it had not.
from naukri_server.tools.profile_update import classify_section_write
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


#: Rows this path skips, and the measured reason for each. Kept as data so
#: the preview, the result and the message all quote the SAME sentence -- a
#: reason that drifts between the preview and the outcome is how an operator
#: approves one thing and gets another.
SKIP_REASONS = {
    "deleted_on_naukri":
        "in the snapshot and GONE from the live profile. A deleted row cannot "
        "be recreated with its original id: re-sending it makes a NEW row with "
        "a NEW id, so anything that referenced the old id would still point at "
        "nothing. A restore can return a row's content, never its identity.",
    "added_since_snapshot":
        "added since the snapshot. Removing it needs the row-DELETE route (a "
        "per-row POST carrying X-HTTP-Method-Override: DELETE), which this "
        "server does not implement, so this path cannot undo it.",
}


def _row_plan(moved: dict) -> tuple:
    """``(planned_row_ids, skipped)`` for a row collection.

    PLANNED are the rows that exist on BOTH sides and differ -- the only rows
    a write can put back as themselves. SKIPPED are named with their reason
    rather than folded into a single refusal, because the two reasons are not
    the same problem and only one of them is about this module.
    """
    skipped = [{"row_id": rid, "reason": "deleted_on_naukri",
                "detail": SKIP_REASONS["deleted_on_naukri"]}
               for rid in moved["only_snapshot"]]
    skipped += [{"row_id": rid, "reason": "added_since_snapshot",
                 "detail": SKIP_REASONS["added_since_snapshot"]}
                for rid in moved["only_live"]]
    return list(moved["changed"]), skipped


def _restorability(spec, section: str, live: dict, snap_profile: dict):
    """``(restorable, reason)`` -- the same test ``restore_section`` applies.

    ``restorable`` means "this path will write something". It does NOT mean
    the section will end up equal to the snapshot; a run with skipped rows is
    restorable AND incomplete. ``preview_restore`` reports both separately.
    """
    if spec.kind == SCALAR_BLOCK:
        fields = _scalar_fields(snap_profile, section)
        if not fields:
            return False, ("the snapshot carries none of the fields %r owns"
                           % section)
        return True, ("scalar section: all %d field(s) go back in one write"
                      % len(fields))

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

    planned, skipped = _row_plan(moved)
    if not planned:
        if skipped:
            return False, (
                "no row can be written back: %s" % _row_guidance(moved))
        return False, "no rows differ; the section already matches the snapshot"

    reason = ("%d row(s) go back one write per row (%s), which is NOT atomic"
              % (len(planned), ", ".join(planned)))
    if skipped:
        reason += ("; %d row(s) are SKIPPED and the section will NOT end up "
                   "equal to the snapshot: %s"
                   % (len(skipped), _row_guidance(moved)))
    return True, reason


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

    planned, skipped, writes = [], [], 1 if restorable else 0
    if spec.kind != SCALAR_BLOCK:
        _, live_value, _ = resolve_section(live, section)
        _, snap_value, _ = resolve_section(snap_profile, section)
        moved = _classify(_rows(live_value), _rows(snap_value), spec.id_field)
        if moved["identified"] and moved["addressable"]:
            planned, skipped = _row_plan(moved)
        writes = len(planned)

    return {
        "status": "preview",
        "snapshot": snapshot,
        "taken_at": data.get("taken_at"),
        "label": data.get("label"),
        "section": section,
        "kind": spec.kind,
        "would_change": diff,
        "restorable": restorable,
        # `restorable` says a write would happen. `restores_completely` says
        # the section would END UP equal to the snapshot. They differ exactly
        # when rows are skipped, and collapsing them is how a caller approves
        # a restore and gets a section that is still wrong.
        "restores_completely": bool(restorable and not skipped),
        "writes_required": writes,
        "atomic": spec.kind == SCALAR_BLOCK,
        "rows_planned": planned,
        "rows_skipped": skipped,
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
        For a scalar section, or a row section where exactly ONE row differs
        and nothing is skipped: whatever ``write_section`` returned --
        ``preview`` / ``updated`` / ``error`` -- with the restore provenance
        attached. That is one write, so it can honestly wear one write's
        result.

        For a row section needing MORE THAN ONE write, or with any row it
        cannot put back: an aggregate that can never be mistaken for a single
        atomic write -- ``{status: preview|updated|partial|error, atomic:
        False, rows: {planned, landed, unknown, not_attempted, skipped},
        row_results: [...]}``. ``updated`` requires every planned row to have
        landed AND nothing to have been skipped.

        ``{status: "unsupported"}`` only where no write is possible at all:
        rows that cannot be addressed by the write spec's id, or a section
        that already matches the snapshot.
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
        # One write, so `atomic` is True -- and it is always PRESENT, on this
        # path and on both row paths, so a caller can branch on it without
        # having to know which shape it got back.
        return await _delegate(section, fields, confirm,
                               dict(provenance, atomic=True))

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

    if not moved["identified"]:
        return _unsupported(
            "The rows of %r cannot be identified by id on both sides, so no "
            "write can target one without guessing which row it means. "
            "NOTHING WAS WRITTEN." % section,
            provenance, detail)

    if not moved["addressable"]:
        return _unsupported(
            "The rows of %r are keyed %r in the profile read but the write "
            "spec targets %r, and this path will not invent a mapping between "
            "them -- a wrong guess writes to the wrong row. NOTHING WAS "
            "WRITTEN." % (section, moved["id_field"], spec.id_field),
            provenance, detail)

    planned, skipped = _row_plan(moved)

    if not planned:
        return _unsupported(
            "Nothing in %r can be written back. %s. NOTHING WAS WRITTEN."
            % (section, _row_guidance(moved)),
            provenance, dict(detail, rows_skipped=skipped))

    # ONE row and nothing skipped is genuinely one write, so it comes back as
    # one write's own result -- verified, diff, collateral, rows_lost and all.
    # Anything wider gets the aggregate shape below, which cannot be read as
    # an atomic success.
    if len(planned) == 1 and not skipped:
        row_id = planned[0]
        tagged = dict(provenance, restored_row=row_id, atomic=True, **detail)
        return await _delegate(section, dict(moved["_snap_map"][row_id]),
                               confirm, tagged)

    return await _restore_rows(section, moved, planned, skipped, confirm,
                               dict(provenance, **detail))


# ---------------------------------------------------------------------------
# 2b. Row-by-row restore -- the non-atomic path, reported as such
# ---------------------------------------------------------------------------

def _row_unit(row_id: str, result: dict) -> dict:
    """One row's line in the report. Carries what a caller must act on."""
    return {
        "row_id": row_id,
        "outcome": classify_section_write(result),
        "status": result.get("status"),
        "error_code": result.get("error_code"),
        "verified": result.get("verified"),
        "snapshot": result.get("snapshot"),
        "would_send": result.get("would_send"),
        "rows_lost": result.get("rows_lost"),
        "collateral": result.get("collateral"),
        "message": result.get("message"),
    }


async def _restore_rows(section: str, moved: dict, planned: list,
                        skipped: list, confirm: bool, provenance: dict) -> dict:
    """Restore N rows, one ``write_section`` call each, and report every one.

    THIS IS NOT ATOMIC AND CANNOT BE MADE SO. There is no transaction across
    two fullprofiles writes and no undo on naukri.com. So the contract is:
    a fixed order, a STOP at the first row that does not land, and a per-row
    outcome for every planned row including the ones never attempted.

    The rows are read from the IN-MEMORY snapshot loaded once by the caller,
    never re-read from disk, so the source snapshot file cannot be affected by
    anything that happens here and a failed run can be retried from it
    unchanged. Each ``write_section`` call takes its OWN pre-write snapshot
    under a collision-safe name; those pile up beside the source, they do not
    replace it.
    """
    # One bucket per outcome, and they are EXHAUSTIVE: every planned row lands
    # in exactly one, asserted below. A summary that can silently drop a row --
    # by having no bucket for `not_sent` or for a write that landed and did
    # collateral damage -- is the same defect as a partial reported as success,
    # just one level up: the caller counts the buckets, sees them add up to
    # less than it asked for, and has no idea what happened to the difference.
    units, landed, unknown, not_attempted = [], [], [], []
    refused, with_collateral = [], []
    stopped = False

    for row_id in planned:
        if stopped:
            not_attempted.append(row_id)
            units.append({
                "row_id": row_id, "outcome": "not_attempted", "status": None,
                "message": "Not attempted: an earlier row in this restore did "
                           "not land, so this one was never sent.",
            })
            continue

        row = dict(moved["_snap_map"][row_id])
        try:
            result = await write_section(
                section=section, fields=row, confirm=confirm,
                label="pre-restore-%s" % section,
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not hide the rest
            logger.warning("Restore of %s row %s raised: %s: %s",
                           section, row_id, type(exc).__name__, exc)
            result = {
                "status": "error", "error_code": "API_ERROR",
                "message": "The write for row %s raised before it could be "
                           "verified, so whether Naukri applied it is "
                           "UNKNOWN: %s: %s"
                           % (row_id, type(exc).__name__, exc),
            }

        unit = _row_unit(row_id, result)
        units.append(unit)
        outcome = unit["outcome"]
        if outcome in ("landed", "preview"):
            landed.append(row_id)
        else:
            if outcome == "unknown":
                unknown.append(row_id)
            elif outcome == "not_sent":
                refused.append(row_id)
            elif outcome == "landed_with_collateral":
                with_collateral.append(row_id)
            else:  # pragma: no cover - defensive; an unnamed outcome is unknown
                unknown.append(row_id)
            stopped = True

    rows = {
        "planned": list(planned),
        "landed": landed,
        "landed_with_collateral": with_collateral,
        "refused": refused,
        "unknown": unknown,
        "not_attempted": not_attempted,
        "skipped": skipped,
    }
    accounted = (len(landed) + len(with_collateral) + len(refused)
                 + len(unknown) + len(not_attempted))
    if accounted != len(planned):  # pragma: no cover - the buckets are exhaustive
        logger.error("Restore of %s accounted for %d of %d planned rows -- "
                     "a row fell out of the summary", section, accounted,
                     len(planned))
    complete = len(landed) == len(planned) and not skipped

    if not confirm:
        return dict(provenance, **{
            "status": "preview", "written": False, "atomic": False,
            "restore": True, "rows": rows, "row_results": units,
            "restores_completely": complete,
            "message": (
                "NOTHING WAS WRITTEN. Restoring %r takes %d separate write(s), "
                "one per row, in the order shown -- they are NOT atomic, so a "
                "later row can fail after an earlier one has landed. Each "
                "`would_send` is what THAT call sends against the profile AS "
                "IT IS NOW; the ROW CONTENT shown is exactly what gets "
                "written, but for a whole-list section the surrounding list in "
                "a later body will differ once an earlier row has landed, "
                "because every call re-reads the live list when it runs.%s "
                "Re-run with confirm=True."
                % (section, len(planned),
                   ("" if not skipped else
                    " %d row(s) will be SKIPPED and the section will NOT end "
                    "up equal to the snapshot: %s."
                    % (len(skipped),
                       "; ".join("%s (%s)" % (s["row_id"], s["reason"])
                                 for s in skipped))))),
        })

    if complete:
        return dict(provenance, **{
            "status": "updated", "written": True, "atomic": False,
            "restore": True, "rows": rows, "row_results": units,
            "restores_completely": True,
            "message": "Restored all %d differing row(s) of %r -- %s -- each "
                       "confirmed in its own fresh read. This took %d separate "
                       "writes and was NOT atomic; it is reported complete "
                       "because every one of them landed."
                       % (len(planned), section, ", ".join(landed),
                          len(planned)),
        })

    return dict(provenance, **{
        "status": "partial" if landed else "error",
        "error_code": None if landed else "RESTORE_FAILED",
        "written": bool(landed),
        "atomic": False,
        "restore": True,
        "rows": rows,
        "row_results": units,
        "restores_completely": False,
        "message": (
            "PARTIAL RESTORE of %r -- do NOT read this as done. %d of %d "
            "planned row(s) landed (%s); %d never attempted (%s); %d "
            "dispatched and UNCONFIRMED (%s) -- an unconfirmed row is neither "
            "done nor undone, so re-read the profile before deciding; %d "
            "refused before the wire (%s); %d landed but moved something else "
            "too (%s); %d skipped and unrestorable (%s). Every row carries its "
            "own `snapshot` where a write was taken. The source snapshot is "
            "untouched: re-run this restore against it once the blocking row "
            "is dealt with."
            % (section, len(landed), len(planned), ", ".join(landed) or "none",
               len(not_attempted), ", ".join(not_attempted) or "none",
               len(unknown), ", ".join(unknown) or "none",
               len(refused), ", ".join(refused) or "none",
               len(with_collateral), ", ".join(with_collateral) or "none",
               len(skipped),
               "; ".join("%s (%s)" % (s["row_id"], s["reason"])
                         for s in skipped) or "none")),
    })


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
