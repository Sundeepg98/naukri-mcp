"""Profile SECTION writes -- one orchestrator, one spec table, ten sections.

The transport lives in ``profile_write`` and the rails in ``profile_snapshot``.
This module is the layer between them: it knows what each section is SHAPED
like, and it refuses to send a shape it does not recognise.

Every spec below is traced to Naukri's own shipped editor bundle
``mnj_v320.min.js`` and written up with byte offsets in
``_audit/_slices/naukri-profile-write-contract.md``. Nothing here is inferred
from convention.

THE THREE KINDS, and why the distinction is not cosmetic
--------------------------------------------------------
``SCALAR_BLOCK``  the edit is a set of fields inside the ``profile`` object.
                  Sent as ``{"profile": {...}}``. Merge semantics; an omitted
                  key is untouched.

``SINGLE_ROW``    the edit is ONE row of a collection, sent as a one-element
                  array: ``{"employments": [ {...} ]}``. The server matches on
                  the row's id field and merges. Omitting the id creates a new
                  row. The other rows are NOT sent and are NOT affected.

``WHOLE_LIST``    the editor sends EVERY row of the collection on every save.
                  Sending one row therefore DELETES the rest. For these the
                  orchestrator reads the live list, merges the caller's row
                  into it by id, and sends all of them. This is the single
                  most dangerous distinction in the module: get it wrong on a
                  SINGLE_ROW section and nothing happens; get it wrong on a
                  WHOLE_LIST section and rows are gone.

WHY EVERY WRITE RE-READS
------------------------
A 200 from this route means the request was accepted. It does not mean the
field changed, and it cannot mean the neighbouring rows survived. So every
write here takes a profile read before, a profile read after, and reports
``verified`` off the AFTER read plus ``collateral`` off the comparison. A
write that cannot be confirmed returns an error naming the snapshot to
restore from -- it never reports success it did not measure.
"""

from typing import Any, Optional

from naukri_server.config import PROFILE_API, logger
from naukri_server.interfaces import api_client
from naukri_server.tools.profile_snapshot import (
    read_key, write_key, resolve_section, save_snapshot, verify_write,
    rows_lost, diff_section,
)
from naukri_server.tools.profile_write import fullprofiles_write

SCALAR_BLOCK = "scalar_block"
SINGLE_ROW = "single_row"
WHOLE_LIST = "whole_list"


class SectionSpec:
    """What one writable profile section is shaped like.

    Attributes:
        kind: SCALAR_BLOCK / SINGLE_ROW / WHOLE_LIST.
        envelope: the key the write payload is nested under.
        id_field: the row id used to target an update; None for SCALAR_BLOCK.
            Omitting it on a SINGLE_ROW / WHOLE_LIST write means "create".
        vocab_fields: fields the editor sends as ``{"id": .., "value": ..}``
            objects rather than free text. A bare string in one of these is
            accepted by the API and then quietly ignored, so it is refused
            here instead.
        required_on_create: fields the editor always sends when there is no
            id. Absent on an update, which is a merge.
        origin: bundle offset in mnj_v320.min.js, so a future reader can
            re-derive the spec rather than trust it.
    """

    __slots__ = ("kind", "envelope", "id_field", "read_id_field",
                 "vocab_fields", "required_on_create", "origin")

    def __init__(self, kind: str, envelope: str, id_field: Optional[str] = None,
                 vocab_fields: tuple = (), required_on_create: tuple = (),
                 origin: str = "", read_id_field: Optional[str] = None):
        self.kind = kind
        self.envelope = envelope
        self.id_field = id_field
        # The READ payload does not always spell the row id the way the WRITE
        # payload does. Five whole-list collections come back keyed `id` and
        # must go out keyed `<section>Id`; the editor does that mapping itself
        # (e.g. `a.onlineProfileId = a.id`) and deletes `id` and `profileId`
        # off each row before sending. Defaults to id_field where they agree.
        self.read_id_field = read_id_field or id_field
        self.vocab_fields = vocab_fields
        self.required_on_create = required_on_create
        self.origin = origin


# ---------------------------------------------------------------------------
# The spec table. Each origin is a byte offset into mnj_v320.min.js.
# ---------------------------------------------------------------------------

SECTION_SPECS = {
    "summary": SectionSpec(
        SCALAR_BLOCK, "profile", origin="35970 (ea)",
    ),
    "resumeHeadline": SectionSpec(
        SCALAR_BLOCK, "profile", origin="35852 (da)",
    ),
    "keySkills": SectionSpec(
        SCALAR_BLOCK, "profile", origin="36311 (ha)",
    ),
    "careerPreferences": SectionSpec(
        SCALAR_BLOCK, "profile", origin="36863 (la)",
        vocab_fields=("entityDepartment", "entityRoleCategory", "entityRole",
                      "entityIndustryTypeId"),
    ),
    "employments": SectionSpec(
        SINGLE_ROW, "employments", id_field="employmentId",
        origin="568934 (rq)",
        required_on_create=("organization", "startDate", "employmentType"),
    ),
    "educations": SectionSpec(
        SINGLE_ROW, "educations", id_field="educationId",
        vocab_fields=("entityInstitute",),
        origin="38155 (oa)",
        required_on_create=("course", "yearOfCompletion"),
    ),
    "schools": SectionSpec(
        SINGLE_ROW, "schools", id_field="schoolId", origin="38913 (pa)",
        required_on_create=("schoolLevel",),
    ),
    "itskills": SectionSpec(
        SINGLE_ROW, "itskills", id_field="sid", origin="37812 (ma)",
        required_on_create=("entitySkill",),
    ),
    "projects": SectionSpec(
        SINGLE_ROW, "projects", id_field="projectId", origin="755823 (As)",
        required_on_create=("projectTitle",),
    ),
    "certifications": SectionSpec(
        SINGLE_ROW, "certifications", id_field="certificationsId",
        origin="718277 (fs)",
    ),
    # -- whole-list collections: sending one row drops the others ------------
    "languages": SectionSpec(
        WHOLE_LIST, "languages", id_field="languageId", origin="790786 (at)",
    ),
    "onlineProfiles": SectionSpec(
        WHOLE_LIST, "onlineProfiles", id_field="onlineProfileId",
        read_id_field="id", origin="640662 (er)",
    ),
    "workSamples": SectionSpec(
        WHOLE_LIST, "workSamples", id_field="workSampleId",
        read_id_field="id", origin="656875 (pr)",
    ),
    "presentations": SectionSpec(
        WHOLE_LIST, "presentations", id_field="presentationId",
        read_id_field="id", origin="668250 (Ar)",
    ),
    "publications": SectionSpec(
        WHOLE_LIST, "publications", id_field="publicationId",
        read_id_field="id", origin="681299 (Lr)",
    ),
    "patents": SectionSpec(
        WHOLE_LIST, "patents", id_field="patentId", read_id_field="id",
        origin="696760 (Wr)",
    ),
}

# The SCALAR_BLOCK sections all write into the SAME ``profile`` envelope, so
# the section name alone does not constrain what gets set: without this,
# write_section("summary", {"resumeHeadline": ...}) would cheerfully rewrite
# the headline. Each scalar section declares exactly which profile keys it
# owns, and a key outside its own set is refused.
SCALAR_ALLOWED_FIELDS = {
    "summary": {"summary"},
    "resumeHeadline": {"resumeHeadline"},
    "keySkills": {"keySkills"},
    "careerPreferences": {
        "entityIndustryTypeId", "entityDepartment", "entityRoleCategory",
        "entityRole", "desiredJobType", "desiredEmploymentType",
        "newLocationPrefId", "desiredRoleTypeId", "shiftPrefTime",
        "expectedCtcCurrency", "absoluteExpectedCtc", "joinDate",
    },
}


def _spec(section: str) -> SectionSpec:
    spec = SECTION_SPECS.get(section)
    if spec is None:
        raise ValueError(
            "Unknown profile section %r. Writable sections: %s"
            % (section, ", ".join(sorted(SECTION_SPECS)))
        )
    return spec


def _check_vocab(spec: SectionSpec, fields: dict) -> None:
    """Refuse a controlled-vocabulary field given as free text.

    The editor sends these as ``{"id": .., "value": ..}``. The API accepts a
    bare string and then does not apply it -- a write that reports 200 and
    changes nothing. Refusing here turns a silent no-op into a clear error.
    """
    for name in spec.vocab_fields:
        if name in fields and not isinstance(fields[name], dict):
            raise ValueError(
                "%r is a controlled-vocabulary field: it must be an object "
                "like {'id': <id>, 'value': <label>}, not %s. A bare value is "
                "accepted by the API and silently ignored."
                % (name, type(fields[name]).__name__)
            )


def _check_scalar_scope(section: str, spec: SectionSpec, fields: dict) -> None:
    """Keep a scalar section from writing a field belonging to another one.

    All four scalar sections share the ``profile`` envelope, so nothing in the
    payload itself distinguishes them. Without this check, asking to write the
    summary and passing ``resumeHeadline`` would rewrite the headline instead,
    snapshot it under the wrong label, and verify clean.
    """
    if spec.kind != SCALAR_BLOCK:
        return
    allowed = SCALAR_ALLOWED_FIELDS.get(section)
    if allowed is None:
        return
    stray = sorted(set(fields) - allowed)
    if stray:
        raise ValueError(
            "Section %r does not own %s. It writes only: %s. Every scalar "
            "section shares the 'profile' envelope, so the section name is "
            "the only thing scoping the write."
            % (section, ", ".join(stray), ", ".join(sorted(allowed)))
        )


async def _read_profile() -> dict:
    """Read the full profile. expand_level=4 or the endpoint 400s."""
    return await api_client.get(PROFILE_API, params={"expand_level": "4"})


def _to_write_row(read_row: dict, spec: SectionSpec) -> dict:
    """Turn a row as READ into a row as the editor SENDS it.

    Five whole-list collections come back keyed ``id`` and go out keyed
    ``<section>Id``. The editor does exactly this itself -- ``er`` at bundle
    offset 640662 reads ``a.onlineProfileId = a.id`` and then deletes ``id``
    and ``profileId`` off the row. Reproducing it matters twice over: without
    the rename no live row matches the caller's id, so an update is impossible;
    and without the strip every row goes back carrying keys the editor removes.

    CONFINED ON PURPOSE. Only the five collections whose read and write
    spellings actually differ are transformed. ``languages`` reads and writes
    ``languageId`` alike and the bundle documents no strip for it, so its rows
    pass through untouched -- generalising the strip to every collection would
    be inventing a contract the evidence does not carry.
    """
    if spec.read_id_field == spec.id_field:
        return dict(read_row)
    row = dict(read_row)
    if spec.read_id_field in row:
        row[spec.id_field] = row[spec.read_id_field]
    row.pop("id", None)
    row.pop("profileId", None)
    return row


def _merge_whole_list(current_rows: list, row: dict, spec: SectionSpec,
                      section: str = "this collection") -> list:
    """Merge one row into the live list, preserving every other row.

    This is what stops a whole-list write from deleting the rest of the
    collection. A row carrying an id REPLACES the matching row in place; a row
    without one is APPENDED. Every live row is first converted to the shape
    the editor sends, so matching happens in one vocabulary rather than across
    the read/write spelling gap.
    """
    rows = [_to_write_row(r, spec) for r in current_rows if isinstance(r, dict)]
    target = row.get(spec.id_field)
    if target is None:
        return rows + [dict(row)]
    for i, existing in enumerate(rows):
        if existing.get(spec.id_field) == target:
            merged = dict(existing)
            merged.update(row)
            rows[i] = merged
            return rows
    raise ValueError(
        "No row in %r carries %s == %r. Pass the id of a row that exists, or "
        "omit it to create a new one. (The read payload spells this id %r, "
        "and it is mapped for you.)"
        % (section, spec.id_field, target, spec.read_id_field)
    )


def _to_expected(fields: dict, spec: SectionSpec) -> dict:
    """Restate the caller's fields in the READ spelling, for verification.

    ``verify_write`` checks containment against the AFTER READ, so anything it
    is asked to look for has to be spelled the way the read spells it. The
    caller writes ``onlineProfileId``; the read returns ``id``. Handing the
    write spelling to the verifier makes a check that CAN NEVER PASS -- every
    update to the five id-mapped collections would report NOT_PERSISTED even
    when it landed perfectly, which is worse than no check at all: it teaches
    the reader to distrust a write that worked.

    ``rows_lost`` already takes the read spelling. This is the mirror site.
    """
    expected = dict(fields)
    if (spec.read_id_field != spec.id_field
            and spec.id_field in expected):
        expected[spec.read_id_field] = expected.pop(spec.id_field)
    return expected


def _build_body(spec: SectionSpec, section: str, fields: dict,
                profile: dict) -> tuple:
    """Return ``(section_body, expected)`` for this write.

    ``section_body`` is in the WRITE spelling, because it goes on the wire.
    ``expected`` is in the READ spelling, because ``verify_write`` compares it
    against a fresh read. Those are not the same vocabulary -- see
    ``_to_expected``.
    """
    if spec.kind == SCALAR_BLOCK:
        return {spec.envelope: dict(fields)}, dict(fields)

    if spec.kind == SINGLE_ROW:
        return {spec.envelope: [dict(fields)]}, _to_expected(fields, spec)

    _, current, found = resolve_section(profile, spec.envelope)
    if not found or current is None:
        current = []
    if not isinstance(current, list):
        raise ValueError(
            "Section %r read back as %s, not a list -- refusing to rebuild a "
            "whole-list payload from it." % (section, type(current).__name__)
        )
    merged = _merge_whole_list(current, dict(fields), spec, section)
    return {spec.envelope: merged}, _to_expected(fields, spec)


async def write_section(section: str, fields: dict, confirm: bool = False,
                        label: Optional[str] = None) -> dict:
    """Write one profile section, then prove it landed.

    Args:
        section: a key of ``SECTION_SPECS``.
        fields: the fields to set. For a row collection, include the spec's
            ``id_field`` to UPDATE an existing row; omit it to CREATE one.
        confirm: False (the default) performs NO write. It returns the exact
            body that would be sent, so the payload can be read before it is
            fired. Only ``confirm=True`` writes.
        label: optional tag for the pre-write snapshot.

    Returns:
        On preview: ``{status: "preview", would_send, ...}``.
        On success: ``{status: "updated", verified: True, diff, snapshot}``.
        On an unconfirmed or damaging write: ``{status: "error", ...}`` naming
        the snapshot to restore from. It never reports a success it did not
        measure in the re-read.
    """
    try:
        spec = _spec(section)
    except ValueError as e:
        # Structured, not raised: every other bad input on this path returns a
        # VALIDATION_ERROR dict, and an MCP caller with a typo'd section name
        # deserves the same answer as one with a typo'd field.
        return {"status": "error", "error_code": "VALIDATION_ERROR",
                "message": str(e)}
    if not isinstance(fields, dict) or not fields:
        return {"status": "error", "error_code": "VALIDATION_ERROR",
                "message": "fields must be a non-empty dict."}
    try:
        _check_vocab(spec, fields)
        _check_scalar_scope(section, spec, fields)
    except ValueError as e:
        return {"status": "error", "error_code": "VALIDATION_ERROR",
                "message": str(e)}

    if spec.kind != SCALAR_BLOCK and spec.id_field not in fields:
        missing = [f for f in spec.required_on_create if f not in fields]
        if missing:
            return {
                "status": "error", "error_code": "VALIDATION_ERROR",
                "message": "Creating a %s row needs %s. Pass %s to update an "
                           "existing row instead."
                           % (section, ", ".join(missing), spec.id_field),
            }

    try:
        before = await _read_profile()
        body, expected = _build_body(spec, section, fields, before)
    except ValueError as e:
        return {"status": "error", "error_code": "VALIDATION_ERROR",
                "message": str(e)}

    is_create = spec.kind != SCALAR_BLOCK and spec.id_field not in fields
    rows_sent = len(body[spec.envelope]) if spec.kind != SCALAR_BLOCK else None

    if not confirm:
        return {
            "status": "preview", "section": section, "kind": spec.kind,
            "action": "create" if is_create else "update",
            "would_send": body, "rows_in_payload": rows_sent,
            "message": "Nothing was written. Re-run with confirm=True to "
                       "send exactly this body.",
        }

    snapshot = await save_snapshot(before, label or ("pre-%s" % section))

    try:
        response = await fullprofiles_write(body)
    except Exception as e:
        return {
            "status": "error", "error_code": "API_ERROR",
            "message": "Write failed: %s: %s" % (type(e).__name__, e),
            "snapshot": snapshot.get("file"), "written": False,
        }

    after = await _read_profile()
    result = verify_write(before, after, spec.envelope, expected)
    # rows_lost compares two READ payloads, so it must use the read spelling
    # of the row id -- five whole-list collections spell it `id` on the way in
    # and `<section>Id` on the way out.
    lost = (rows_lost(before, after, spec.envelope, spec.read_id_field)
            if spec.read_id_field else [])

    payload = {
        "section": section, "kind": spec.kind,
        "action": "create" if is_create else "update",
        "verified": result["verified"], "reason": result["reason"],
        "diff": result["diff"], "collateral": result["collateral"],
        "rows_lost": lost, "snapshot": snapshot.get("file"),
        "response": response,
    }

    if not result["verified"]:
        payload.update({
            "status": "error", "error_code": "NOT_PERSISTED",
            "message": "The write was accepted but the re-read does not show "
                       "it. Nothing here confirms the change landed. Restore "
                       "from snapshot %r if the section looks wrong."
                       % snapshot.get("file"),
        })
        return payload

    if lost or result["collateral"]:
        payload.update({
            "status": "error", "error_code": "COLLATERAL_CHANGE",
            "message": "The intended change landed, but %d row(s) disappeared "
                       "and %d unrelated change(s) were detected in %r. "
                       "Restore from snapshot %r."
                       % (len(lost), len(result["collateral"]), section,
                          snapshot.get("file")),
        })
        return payload

    try:
        from naukri_server.tools.profile import (
            _profile_ttl_cache, _dashboard_ttl_cache,
        )
        _profile_ttl_cache.invalidate()
        _dashboard_ttl_cache.invalidate()
    except Exception:
        logger.debug("Profile cache invalidation skipped", exc_info=True)

    try:
        from naukri_server.events import event_bus, ProfileUpdated
        await event_bus.emit(ProfileUpdated(
            fields=", ".join(sorted(fields)), method="rest_v1",
        ))
    except Exception:
        logger.debug("ProfileUpdated emit skipped", exc_info=True)

    payload.update({
        "status": "updated",
        "message": "Wrote %s and confirmed it in a fresh read. No rows lost, "
                   "no collateral changes." % section,
    })
    return payload
