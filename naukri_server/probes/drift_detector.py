"""Schema drift detection for upstream API responses.

This is the DETECTION layer only. No auto-fix, no remediation. Any future
healing tier (markdown report / notify-only / auto-patch) consumes the
DriftReport objects this module emits.

Snapshots are stored in-memory by URL. The first call to snapshot_endpoint(url)
records the schema; subsequent check_drift(url) calls compare current vs
snapshot and return a DriftReport if any added/removed/type-changed fields are
found.

Schema representation:
  Each schema is a flat dict {dotted_path: type_name}. We walk the response
  to a maximum depth to keep the snapshot bounded; lists are represented by
  their element type at index [0] (assumed homogeneous, which is the Naukri
  convention).

Severity ranking (highest first):
  - "removed"      — a field present at snapshot time is gone
  - "type_change"  — a field still exists but its type changed
  - "added"        — a new field appeared (often a backwards-compat addition)

Mixed reports are reported with the worst severity wins.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# Maximum depth we walk into nested objects when building a schema. Naukri
# responses are usually flat-ish; we stop at 4 to avoid pathological recursion.
_MAX_DEPTH = 4

# Severity ordering — used to "promote" a mixed report to its worst severity.
_SEVERITY_RANK = {"added": 0, "type_change": 1, "removed": 2}


@dataclass
class DriftReport:
    """Result of a single drift check.

    Attributes:
        url: The endpoint URL the report is for.
        severity: The worst severity found ("added" | "removed" | "type_change").
        changed_fields: Mapping of dotted_path -> change description, e.g.
            "jobDetails.0.salary" -> "removed", or
            "jobDetails.0.minSal" -> "type_change: int -> str".
        snapshot_age_days: Float days since the original snapshot was taken.
    """

    url: str
    severity: str
    changed_fields: dict[str, str] = field(default_factory=dict)
    snapshot_age_days: float = 0.0


def _type_name(value: Any) -> str:
    """Return a stable, comparable type label for a schema value.

    We collapse all numeric types except bool to 'number' (Naukri sometimes
    returns "5" vs 5 for the same field across versions, and we don't want to
    flag that as drift). We also collapse None to a literal 'null' so the
    presence of a null doesn't masquerade as a type change.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"  # check before int — bool is an int subclass in Python
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _walk(obj: Any, prefix: str, depth: int, schema: dict[str, str]) -> None:
    """Recursively flatten a JSON-like value into {dotted_path: type_name}."""
    if depth >= _MAX_DEPTH:
        # Record only the type at the cutoff, don't descend further.
        schema[prefix] = _type_name(obj)
        return

    if isinstance(obj, dict):
        if not obj:
            schema[prefix or "_root"] = "dict"  # empty dict — record presence
            return
        for k, v in obj.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            schema[child] = _type_name(v)
            if isinstance(v, (dict, list)):
                _walk(v, child, depth + 1, schema)
    elif isinstance(obj, list):
        if not obj:
            schema[prefix or "_root"] = "list"
            return
        # Use the first element as the canonical sample. Naukri lists are
        # homogeneous in practice — each row is the same shape.
        first = obj[0]
        child = f"{prefix}.0" if prefix else "0"
        schema[child] = _type_name(first)
        if isinstance(first, (dict, list)):
            _walk(first, child, depth + 1, schema)
    else:
        schema[prefix or "_root"] = _type_name(obj)


def _build_schema(response: Any) -> dict[str, str]:
    """Build a flat {dotted_path: type_name} schema from a JSON-like value."""
    schema: dict[str, str] = {}
    _walk(response, prefix="", depth=0, schema=schema)
    return schema


def _diff_schemas(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    """Return {dotted_path: change_description} for every drift between two schemas."""
    changes: dict[str, str] = {}
    before_keys = set(before.keys())
    after_keys = set(after.keys())

    for missing in before_keys - after_keys:
        changes[missing] = "removed"
    for new in after_keys - before_keys:
        changes[new] = "added"
    for shared in before_keys & after_keys:
        old_type = before[shared]
        new_type = after[shared]
        if old_type != new_type:
            # Don't flag a transition to/from null as a type change — null often
            # means "field exists but no value yet" and shouldn't be surfaced.
            if old_type == "null" or new_type == "null":
                continue
            changes[shared] = f"type_change: {old_type} -> {new_type}"
    return changes


def _worst_severity(changes: Iterable[str]) -> str:
    """Pick the highest-rank severity from a set of change descriptions."""
    worst = "added"
    worst_rank = -1
    for desc in changes:
        if desc.startswith("type_change"):
            kind = "type_change"
        elif desc == "removed":
            kind = "removed"
        else:
            kind = "added"
        rank = _SEVERITY_RANK[kind]
        if rank > worst_rank:
            worst_rank = rank
            worst = kind
    return worst


@dataclass
class _Snapshot:
    """Internal record of a captured schema at a point in time."""

    schema: dict[str, str]
    taken_at: float  # monotonic seconds (time.time())


class DriftDetector:
    """In-memory store of endpoint schemas with drift comparison.

    Usage:
        detector = DriftDetector()
        detector.snapshot_endpoint(url, sample_response)
        # ... later ...
        report = detector.check_drift(url, fresh_response)
        if report:
            log.warning("Drift in %s: %s", report.url, report.changed_fields)

    This class is intentionally simple — it does NOT persist snapshots, does
    NOT trigger remediation, and does NOT emit events. A future healing layer
    can subscribe to drift reports via whatever wiring it chooses.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, _Snapshot] = {}

    # -- Public API --------------------------------------------------------

    def snapshot_endpoint(self, url: str, sample_response: Any) -> dict[str, str]:
        """Record (or overwrite) the current schema for `url`.

        Returns the captured schema so callers can log / persist it externally.
        """
        if not url:
            raise ValueError("url is required")
        schema = _build_schema(sample_response)
        self._snapshots[url] = _Snapshot(schema=schema, taken_at=time.time())
        return dict(schema)  # defensive copy

    def has_snapshot(self, url: str) -> bool:
        """Return True if a snapshot exists for `url`."""
        return url in self._snapshots

    def get_snapshot(self, url: str) -> dict[str, str] | None:
        """Return a copy of the schema snapshot for `url`, or None."""
        snap = self._snapshots.get(url)
        if snap is None:
            return None
        return dict(snap.schema)

    def clear(self, url: str | None = None) -> None:
        """Remove a single snapshot, or all snapshots if `url` is None."""
        if url is None:
            self._snapshots.clear()
        else:
            self._snapshots.pop(url, None)

    def check_drift(self, url: str, current_response: Any) -> DriftReport | None:
        """Compare a fresh response against the stored snapshot for `url`.

        Returns:
            None if no snapshot exists yet, or if the schemas match exactly.
            A DriftReport otherwise.
        """
        snap = self._snapshots.get(url)
        if snap is None:
            return None

        current_schema = _build_schema(current_response)
        changes = _diff_schemas(snap.schema, current_schema)
        if not changes:
            return None

        age_seconds = max(0.0, time.time() - snap.taken_at)
        return DriftReport(
            url=url,
            severity=_worst_severity(changes.values()),
            changed_fields=changes,
            snapshot_age_days=age_seconds / 86400.0,
        )


# Module-level singleton for callers that want one shared detector. Tests
# should construct their own DriftDetector() to avoid cross-test bleed.
detector = DriftDetector()
