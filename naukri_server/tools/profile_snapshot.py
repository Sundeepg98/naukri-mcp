"""Safety rails for profile-section writes: snapshot before, verify after.

A profile-section write is a ONE-WAY DOOR. There is no undo on naukri.com, and
the six collections listed in ``WHOLE_LIST_SECTIONS`` are sent to the server as
ENTIRE LISTS -- a caller that sends only the row it wants to add DELETES every
other row in that section, and the API returns success while doing it. That is
the single failure this module exists to make impossible to ship unnoticed.

Three rails, in the order a caller uses them:

  1. SNAPSHOT   ``save_snapshot`` -- the full pre-write read, on disk, atomic.
  2. DIFF       ``diff_section``  -- what actually moved, structurally.
  3. VERIFY     ``verify_write``  -- did the intended change land, AND did
                anything ELSE in that section move? The second half is
                ``collateral`` and it is the silent-deletion detector.

TRANSPORT-FREE ON PURPOSE. Nothing here opens a socket, a browser or a page.
Every function takes profile dicts the caller already has and returns plain
data, so the whole layer is testable against fixtures and cannot itself cause
the accident it guards.

THE READ/WRITE KEY ASYMMETRY (measured, not assumed). Five collections come
back from the READ under a SINGULAR key and go out on the WRITE under a PLURAL
one. Evidence: ``_audit/_slices/naukri-profile-read-shape.md`` (21 top-level
sections at expand_level=4, singular spellings) and
``_audit/_slices/naukri-profile-write-contract.md`` (per-section body
envelopes, plural spellings). Every function here resolves the section name
through ``READ_KEY_FOR_WRITE_KEY`` / ``WRITE_KEY_FOR_READ_KEY``, so a caller
may pass either spelling and sections that keep one name pass through
unchanged.

PURE: filesystem plus dict arithmetic. No network, no browser, no database.
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Optional

from naukri_server.config import DATA_DIR, logger
from naukri_server.utils import save_json_atomic

# ---------------------------------------------------------------------------
# Section-name resolution
# ---------------------------------------------------------------------------

#: WRITE key (what the fullprofiles body uses) -> READ key (what the profile
#: GET returns). Measured; see the two _audit/_slices documents named above.
READ_KEY_FOR_WRITE_KEY = {
    "onlineProfiles": "onlineProfile",
    "patents": "patent",
    "presentations": "presentation",
    "publications": "publication",
    "workSamples": "workSample",
}

#: The inverse. READ key -> WRITE key.
WRITE_KEY_FOR_READ_KEY = {read: write for write, read in READ_KEY_FOR_WRITE_KEY.items()}

#: The six sections the front end sends as a COMPLETE list every time. Sending
#: one row drops the rest. Spelled with their WRITE keys, which is how a caller
#: building a payload will name them. `languages` is in this set and is NOT
#: part of the singular/plural asymmetry -- it is spelled the same on both
#: sides, which is exactly why it is easy to miss.
WHOLE_LIST_SECTIONS = frozenset((
    "onlineProfiles",
    "workSamples",
    "presentations",
    "publications",
    "patents",
    "languages",
))

#: Snapshot payload format. Bump when the on-disk shape changes.
SNAPSHOT_VERSION = 1

#: Filenames this module owns. Timestamp first and fixed width, so a plain
#: lexicographic sort IS chronological order.
_SNAPSHOT_GLOB = "profile-*.json"
_SNAPSHOT_STAMP = "%Y%m%dT%H%M%S%fZ"
_UNSAFE_LABEL = re.compile(r"[^A-Za-z0-9_-]+")


def read_key(section: str) -> str:
    """Return the key the profile READ uses for *section*.

    Accepts either spelling. Identity for every section that keeps one name.
    """
    return READ_KEY_FOR_WRITE_KEY.get(section, section)


def write_key(section: str) -> str:
    """Return the key a WRITE body uses for *section*.

    Accepts either spelling. Identity for every section that keeps one name.
    """
    return WRITE_KEY_FOR_READ_KEY.get(section, section)


def resolve_section(profile: dict, section: str):
    """Find *section* in *profile* under whichever spelling is present.

    Returns ``(key_used, value, found)``. ``key_used`` is the spelling actually
    found so a caller can report the real key; when nothing is found it is the
    read spelling, which is what a profile read would have carried.
    """
    if not isinstance(profile, dict):
        return read_key(section), None, False
    for candidate in (section, read_key(section), write_key(section)):
        if candidate in profile:
            return candidate, profile[candidate], True
    return read_key(section), None, False


# ---------------------------------------------------------------------------
# 1. Snapshots
# ---------------------------------------------------------------------------

#: DATA_DIR-relative, exactly like `EXPORTS_DIR = DATA_DIR / "exports"` in
#: config.py -- the repo's one convention for a runtime-state DIRECTORY.
#: DATA_DIR defaults to the repo root (overridable with NAUKRI_DATA_DIR), so
#: `profile_snapshots/` is in .gitignore for the same reason `exports/` is:
#: these files carry live personal data and must never be committed. Module
#: level and underscore-prefixed so a test can redirect it, matching
#: `tools/export.py:_EXPORTS_DIR`.
_SNAPSHOT_DIR = DATA_DIR / "profile_snapshots"

#: Serialises concurrent snapshot writes, as `_sync_state_lock` does for sync
#: state. Filenames are already collision-safe; this protects the prune/list
#: pair from observing a half-written directory.
_snapshot_lock = asyncio.Lock()


def snapshot_dir() -> Path:
    """Where snapshots live. PURE -- calling this creates nothing.

    Directory creation happens in ``save_snapshot`` and nowhere else, so
    importing this module or listing snapshots can never dirty the tree.
    """
    return _SNAPSHOT_DIR


def _slug(label: str) -> str:
    """Filename-safe form of *label*.

    No dots: `save_json_atomic` derives its `.backup` and `.tmp` sidecars with
    `Path.with_suffix`, which replaces the LAST suffix -- a dot inside the
    label would send the temp file somewhere unintended.
    """
    slug = _UNSAFE_LABEL.sub("-", str(label or "")).strip("-")
    return slug[:60] or "unlabelled"


def _snapshot_name(label: str, when: datetime) -> str:
    """Chronologically sortable and collision-safe.

    Fixed-width UTC timestamp to microseconds, then the slug, then 8 random hex
    characters. Two snapshots taken in the same microsecond still get distinct
    names, so a save can never silently overwrite an earlier one.
    """
    return "profile-%s__%s__%s.json" % (
        when.strftime(_SNAPSHOT_STAMP), _slug(label), uuid.uuid4().hex[:8],
    )


async def save_snapshot(profile: dict, label: str) -> dict:
    """Persist the FULL profile read plus metadata. Returns the metadata only.

    Args:
        profile: the complete profile dict as read from the API.
        label: short free-text tag, e.g. the section about to be written.

    Returns:
        ``{status, file, path, taken_at, label, sections, section_count,
        bytes, snapshot_version}``. The profile body is NOT echoed back.
    """
    if not isinstance(profile, dict):
        raise TypeError("save_snapshot needs the profile dict, got %s"
                        % type(profile).__name__)

    taken_at = datetime.now(timezone.utc)
    sections = sorted(str(k) for k in profile.keys())
    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "taken_at": taken_at.isoformat(),
        "label": str(label or ""),
        "sections": sections,
        "profile": profile,
    }

    directory = snapshot_dir()
    name = _snapshot_name(label, taken_at)
    path = directory / name

    async with _snapshot_lock:
        directory.mkdir(parents=True, exist_ok=True)
        save_json_atomic(path, payload, logger)
        size = path.stat().st_size

    logger.info("Profile snapshot saved: %s (%d sections, %d bytes)",
                name, len(sections), size)
    return {
        "status": "success",
        "file": name,
        "path": str(path),
        "taken_at": payload["taken_at"],
        "label": payload["label"],
        "sections": sections,
        "section_count": len(sections),
        "bytes": size,
        "snapshot_version": SNAPSHOT_VERSION,
    }


def list_snapshots() -> list:
    """Every snapshot on disk, NEWEST FIRST. Never returns profile content.

    Ordered by FILENAME descending, not by mtime: the name carries a fixed-width
    UTC timestamp, so filename order is chronological order and does not depend
    on filesystem timestamp resolution or on a file having been copied.

    A snapshot whose JSON will not parse still appears, with null metadata and
    ``readable: False`` -- a corrupt file is exactly when a caller most needs to
    see that it exists.
    """
    directory = snapshot_dir()
    if not directory.exists():
        return []

    out = []
    for path in sorted(directory.glob(_SNAPSHOT_GLOB),
                       key=lambda p: p.name, reverse=True):
        entry = {
            "file": path.name,
            "taken_at": None,
            "label": None,
            "bytes": path.stat().st_size,
            "section_count": 0,
            "readable": False,
        }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Snapshot %s will not parse; listing it as unreadable",
                           path.name)
            out.append(entry)
            continue
        if isinstance(data, dict):
            entry["taken_at"] = data.get("taken_at")
            entry["label"] = data.get("label")
            entry["section_count"] = len(data.get("sections") or [])
            entry["snapshot_version"] = data.get("snapshot_version")
            entry["readable"] = True
        out.append(entry)
    return out


def _safe_snapshot_path(name: str) -> Path:
    """Resolve *name* to a path INSIDE the snapshot directory, or raise.

    Rejects, before touching the filesystem: anything with a path separator,
    any parent reference, any absolute or drive-qualified path. The Windows
    checks run with `PureWindowsPath` on every platform on purpose -- on POSIX,
    ``Path(r"C:\\x").name`` is the whole string, so a plain
    ``name != Path(name).name`` test would let a drive path through on Linux
    and the guard would only work on the box it was written on.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("snapshot name must be a non-empty string")

    if "/" in name or "\\" in name:
        raise ValueError(
            "snapshot name must be a bare filename, not a path: %r" % name)

    win = PureWindowsPath(name)
    if win.drive or win.anchor or win.is_absolute() or os.path.isabs(name):
        raise ValueError(
            "snapshot name must not be absolute or drive-qualified: %r" % name)

    if name in (".", "..") or ".." in win.parts:
        raise ValueError(
            "snapshot name must not contain a parent reference: %r" % name)

    base = snapshot_dir().resolve()
    target = (base / name).resolve()
    if target.parent != base:
        raise ValueError(
            "snapshot name escapes the snapshot directory: %r" % name)
    return target


def load_snapshot(name: str) -> dict:
    """Read one snapshot back, whole.

    *name* must be a bare filename inside the snapshot directory. Anything that
    could escape it raises ``ValueError`` -- see ``_safe_snapshot_path``.
    """
    path = _safe_snapshot_path(name)
    if not path.exists():
        raise FileNotFoundError("no such snapshot: %s" % name)
    return json.loads(path.read_text(encoding="utf-8"))


def prune_snapshots(keep: int) -> int:
    """Keep the newest *keep* snapshots, delete the rest. Returns how many went.

    Raises ``ValueError`` when ``keep < 1``. Deleting everything is never what
    a retention setting means, and a ``keep=0`` that silently wiped the only
    copies of a profile would defeat the entire point of this module.
    """
    if not isinstance(keep, int) or isinstance(keep, bool):
        raise ValueError("keep must be an int, got %r" % (keep,))
    if keep < 1:
        raise ValueError(
            "keep must be at least 1; refusing to delete every snapshot "
            "(got %d)" % keep)

    directory = snapshot_dir()
    if not directory.exists():
        return 0

    ordered = sorted(directory.glob(_SNAPSHOT_GLOB),
                     key=lambda p: p.name, reverse=True)
    deleted = 0
    for path in ordered[keep:]:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("Could not prune snapshot %s: %s", path.name, exc)
    if deleted:
        logger.info("Pruned %d snapshot(s), kept %d",
                    deleted, min(keep, len(ordered)))
    return deleted


# ---------------------------------------------------------------------------
# 2. Structural diff
# ---------------------------------------------------------------------------

def _leaf_paths(value: Any, prefix: str = "") -> dict:
    """``{path: leaf}`` for every scalar under *value*.

    Empty containers are recorded as leaves themselves, so "the list became
    empty" is a visible change rather than the silent absence of any path.
    """
    out = {}
    if isinstance(value, dict):
        if not value:
            out[prefix] = {}
        for key, sub in value.items():
            child = ("%s.%s" % (prefix, key)) if prefix else str(key)
            out.update(_leaf_paths(sub, child))
    elif isinstance(value, list):
        if not value:
            out[prefix] = []
        for index, sub in enumerate(value):
            out.update(_leaf_paths(sub, "%s[%d]" % (prefix, index)))
    else:
        out[prefix] = value
    return out


def detect_id_field(before_rows: list, after_rows: list) -> Optional[str]:
    """Guess the row-id key of a collection, generically. No field names.

    The rule is structural: a key present on EVERY row, whose value is a
    non-empty scalar, and whose values are DISTINCT within each list. That last
    clause is what disqualifies ``profileId`` -- the read payload stamps the
    same profileId on every row of every section, so it looks like an id and
    identifies nothing.

    Returns ``None`` unless one of the two lists has at least two rows. With a
    single row, uniqueness cannot discriminate between a real row id and a
    profile-level anchor, and positional matching is equivalent anyway -- so
    the honest answer is "cannot tell", not a coin flip.

    Ties (two keys both unique on every row, e.g. ``languageId`` and
    ``entityLanguageId``) resolve lexicographically. Either key matches rows
    correctly; the choice only has to be deterministic.
    """
    before_rows = [r for r in (before_rows or []) if isinstance(r, dict)]
    after_rows = [r for r in (after_rows or []) if isinstance(r, dict)]
    if max(len(before_rows), len(after_rows)) < 2:
        return None

    populated = [rows for rows in (before_rows, after_rows) if rows]
    common = set(populated[0][0])
    for rows in populated:
        for row in rows:
            common &= set(row)

    for key in sorted(common):
        lowered = key.lower()
        if lowered != "id" and not lowered.endswith("id"):
            continue
        ok = True
        for rows in populated:
            values = [row.get(key) for row in rows]
            if any(v is None or v == "" or isinstance(v, (dict, list))
                   for v in values):
                ok = False
                break
            if len({str(v) for v in values}) != len(values):
                ok = False
                break
        if ok:
            return key
    return None


def _is_row_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) for item in value)


def _diff_values(before: Any, after: Any, prefix: str) -> dict:
    """Leaf-path diff of two arbitrary values."""
    left = _leaf_paths(before, prefix)
    right = _leaf_paths(after, prefix)
    added = [{"path": p, "value": right[p]} for p in right if p not in left]
    removed = [{"path": p, "value": left[p]} for p in left if p not in right]
    changed = [{"path": p, "from": left[p], "to": right[p]}
               for p in left if p in right and left[p] != right[p]]
    unchanged = sum(1 for p in left if p in right and left[p] == right[p])
    return {"added": sorted(added, key=lambda e: e["path"]),
            "removed": sorted(removed, key=lambda e: e["path"]),
            "changed": sorted(changed, key=lambda e: e["path"]),
            "unchanged_count": unchanged}


def _row_diff(before_rows: list, after_rows: list, prefix: str,
              id_field: Optional[str]) -> dict:
    """Diff two arrays-of-object, matching rows by id when one can be found."""
    if not id_field:
        sub = _diff_values(before_rows, after_rows, prefix)
        sub["id_field"] = None
        return sub

    added, removed, changed = [], [], []
    unchanged = 0
    before_map = {str(r.get(id_field)): r for r in before_rows}
    after_map = {str(r.get(id_field)): r for r in after_rows}

    for row_id, row in before_map.items():
        if row_id not in after_map:
            removed.append({"path": "%s[%s=%s]" % (prefix, id_field, row_id),
                            "value": row})
    for row_id, row in after_map.items():
        if row_id not in before_map:
            added.append({"path": "%s[%s=%s]" % (prefix, id_field, row_id),
                          "value": row})
    for row_id, row in before_map.items():
        if row_id in after_map:
            sub = _diff_values(row, after_map[row_id],
                               "%s[%s=%s]" % (prefix, id_field, row_id))
            added.extend(sub["added"])
            removed.extend(sub["removed"])
            changed.extend(sub["changed"])
            unchanged += sub["unchanged_count"]

    return {"added": sorted(added, key=lambda e: e["path"]),
            "removed": sorted(removed, key=lambda e: e["path"]),
            "changed": sorted(changed, key=lambda e: e["path"]),
            "unchanged_count": unchanged,
            "id_field": id_field}


def diff_section(before: dict, after: dict, section: str) -> dict:
    """Structural diff of ONE top-level section between two profile reads.

    Generic: it walks shape and values, and knows no field names. Arrays of
    objects are matched by a detected row id where one exists (see
    ``detect_id_field``) so that deleting a row from the middle reports one
    removed ROW rather than a cascade of shifted indices; everything else is
    matched by path.

    *section* may be given in either the read or the write spelling.

    Returns:
        ``{added: [{path, value}], removed: [{path, value}],
        changed: [{path, from, to}], unchanged_count: int, section: str,
        id_field: str|None, present_before: bool, present_after: bool}``
    """
    before_key, before_value, before_found = resolve_section(before, section)
    after_key, after_value, after_found = resolve_section(after, section)
    prefix = after_key if after_found else before_key

    if _is_row_list(before_value) or _is_row_list(after_value):
        rows_before = before_value if isinstance(before_value, list) else []
        rows_after = after_value if isinstance(after_value, list) else []
        if all(isinstance(r, dict) for r in list(rows_before) + list(rows_after)):
            result = _row_diff(rows_before, rows_after, prefix,
                               detect_id_field(rows_before, rows_after))
            result["section"] = prefix
            result["present_before"] = before_found
            result["present_after"] = after_found
            return result

    result = _diff_values(before_value, after_value, prefix)
    result["id_field"] = None
    result["section"] = prefix
    result["present_before"] = before_found
    result["present_after"] = after_found
    return result


# ---------------------------------------------------------------------------
# 3. Verify after write
# ---------------------------------------------------------------------------

def _contains(actual: Any, expected: Any) -> bool:
    """True when every key/value in *expected* is present in *actual*."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(k in actual and _contains(actual[k], v)
                   for k, v in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        return all(any(_contains(item, want) for item in actual)
                   for want in expected)
    return actual == expected


def _section_contains(section_value: Any, expected: dict) -> bool:
    """Containment against a section that may be a dict OR an array of rows."""
    if isinstance(section_value, list):
        return any(_contains(row, expected) for row in section_value)
    return _contains(section_value, expected)


def _is_accounted(kind: str, entry: dict, expected: dict,
                  expected_leaves: dict) -> bool:
    """Is this diff entry explained by the write the caller said it made?

    A REMOVED path is never explained. The write contract expresses deletion of
    a scalar as an explicit empty string, never as omission (write-contract
    5.4), so a path that vanished is a loss the caller did not ask for. That is
    the whole point of collateral.

    KNOWN LIMITATION, stated rather than hidden: matching is by leaf-path
    suffix plus value, so if the SAME field changed to the SAME value on two
    different rows, both count as accounted-for. Row deletion -- the hazard
    this module exists for -- is unaffected, because removals never account.
    """
    if kind == "removed":
        return False
    value = entry["to"] if kind == "changed" else entry["value"]
    path = entry["path"]
    if kind == "added" and expected and _contains(value, expected):
        return True
    for rel, want in expected_leaves.items():
        if not rel:
            continue
        if (path == rel or path.endswith("." + rel)) and value == want:
            return True
    return False


def verify_write(before: dict, after: dict, section: str, expected: dict) -> dict:
    """Did the intended write land, and did anything ELSE in the section move?

    Args:
        before: the profile read taken BEFORE the write.
        after: the profile read taken AFTER it.
        section: top-level section name, either spelling.
        expected: the key/values the write was supposed to put there.

    Returns:
        ``{verified, reason, diff, collateral, section, expected_keys}``.

        ``verified`` is True only when every key/value in *expected* is present
        in *after*'s section. An EMPTY *expected* is False, not vacuously True:
        a gate that passes when asked to check nothing is not a gate.

        ``collateral`` lists every change in that section NOT accounted for by
        *expected*, each tagged with its ``kind``. A row that vanished from a
        collection appears here. THIS IS THE SILENT-DELETION DETECTOR: a
        whole-list section (see ``WHOLE_LIST_SECTIONS``) sent with one row
        deletes the others and the API still reports success, so the response
        cannot tell you and only this comparison can.
    """
    section_key, after_value, after_found = resolve_section(after, section)
    diff = diff_section(before, after, section)
    expected = expected if isinstance(expected, dict) else {}
    expected_leaves = _leaf_paths(expected, "") if expected else {}

    collateral = []
    for kind in ("added", "removed", "changed"):
        for entry in diff[kind]:
            if not _is_accounted(kind, entry, expected, expected_leaves):
                item = dict(entry)
                item["kind"] = kind
                collateral.append(item)

    checked = sorted(expected.keys())
    if not expected:
        verified = False
        verdict = ("nothing to check: `expected` was empty, so this call "
                   "cannot certify the write")
    elif not after_found:
        verified = False
        verdict = ("section %r is ABSENT from the post-write read, so none of "
                   "the %d expected key(s) %s can be present"
                   % (section_key, len(checked), checked))
    else:
        verified = _section_contains(after_value, expected)
        verdict = ("checked %d expected key(s) %s against section %r of the "
                   "post-write read: %s"
                   % (len(checked), checked, section_key,
                      "all present" if verified else "NOT all present"))

    reason = (
        "%s. Diff over that section: %d added, %d removed, %d changed, "
        "%d unchanged. %d change(s) not accounted for by the expected write "
        "(collateral)."
        % (verdict, len(diff["added"]), len(diff["removed"]),
           len(diff["changed"]), diff["unchanged_count"], len(collateral))
    )
    if write_key(section_key) in WHOLE_LIST_SECTIONS:
        reason += (" NOTE: %r is sent to Naukri as a WHOLE LIST -- a payload "
                   "carrying one row deletes every other row in it."
                   % write_key(section_key))

    return {
        "verified": verified,
        "reason": reason,
        "diff": diff,
        "collateral": collateral,
        "section": section_key,
        "expected_keys": checked,
    }


def rows_lost(before: dict, after: dict, section: str, id_field: str) -> list:
    """Row ids present in *before*'s section and gone from *after*'s.

    The one function to call after writing any of ``WHOLE_LIST_SECTIONS``.
    Those six are sent as complete lists, so a payload carrying a single row
    silently drops the rest; this returns exactly which ids went, in their
    original order.

    Rows with no value under *id_field* are skipped -- an unidentified row
    cannot be tracked across the two reads. Raises ``ValueError`` if the
    section is present but is not a list: asking which ROWS were lost from a
    non-collection is a caller bug, and a rail must say so rather than return
    a reassuring empty list.
    """
    if not id_field:
        raise ValueError("rows_lost needs an id_field")

    _, before_value, before_found = resolve_section(before, section)
    _, after_value, after_found = resolve_section(after, section)

    for value, found, which in ((before_value, before_found, "before"),
                                (after_value, after_found, "after")):
        if found and value is not None and not isinstance(value, list):
            raise ValueError(
                "section %r is %s in the %s profile, not an array of rows"
                % (section, type(value).__name__, which))

    if not isinstance(before_value, list):
        return []
    after_rows = after_value if isinstance(after_value, list) else []
    after_ids = {str(r.get(id_field)) for r in after_rows
                 if isinstance(r, dict) and r.get(id_field) not in (None, "")}

    lost, seen = [], set()
    for row in before_value:
        if not isinstance(row, dict):
            continue
        raw = row.get(id_field)
        if raw in (None, ""):
            continue
        key = str(raw)
        if key in after_ids or key in seen:
            continue
        seen.add(key)
        lost.append(raw)
    return lost
