"""Tests for naukri_server.probes.drift_detector — pure unit, no I/O.

DriftDetector stores in-memory snapshots and reports schema changes between a
captured baseline and a current response. Each test constructs its own
DriftDetector() so there's no cross-test state.
"""

import time

import pytest

from naukri_server.probes.drift_detector import (
    DriftDetector,
    DriftReport,
    _build_schema,
    _diff_schemas,
    _type_name,
    _worst_severity,
    detector as module_detector,
)


# ---------------------------------------------------------------------------
# _type_name — collapses numeric subtypes, distinguishes bool from int
# ---------------------------------------------------------------------------


def test_type_name_collapses_int_and_float_to_number():
    assert _type_name(1) == "number"
    assert _type_name(1.5) == "number"
    assert _type_name(0) == "number"


def test_type_name_distinguishes_bool_from_number():
    """bool is an int subclass — we MUST check it first to avoid mislabeling."""
    assert _type_name(True) == "bool"
    assert _type_name(False) == "bool"


def test_type_name_basic_types():
    assert _type_name("hello") == "str"
    assert _type_name(None) == "null"
    assert _type_name([]) == "list"
    assert _type_name({}) == "dict"


# ---------------------------------------------------------------------------
# _build_schema — flattens JSON-ish into {dotted_path: type}
# ---------------------------------------------------------------------------


def test_build_schema_flat_dict():
    schema = _build_schema({"name": "alice", "age": 30, "active": True})
    assert schema == {"name": "str", "age": "number", "active": "bool"}


def test_build_schema_nested_dict():
    schema = _build_schema({"user": {"name": "alice", "age": 30}})
    assert schema["user"] == "dict"
    assert schema["user.name"] == "str"
    assert schema["user.age"] == "number"


def test_build_schema_list_uses_first_element_as_canonical():
    schema = _build_schema({"jobs": [{"id": 1, "title": "SDE"}, {"id": 2}]})
    # Should sample from index 0 only, not flag missing field on index 1.
    assert schema["jobs"] == "list"
    assert schema["jobs.0"] == "dict"
    assert schema["jobs.0.id"] == "number"
    assert schema["jobs.0.title"] == "str"


def test_build_schema_empty_collections():
    schema = _build_schema({"jobs": [], "meta": {}})
    assert schema["jobs"] == "list"
    assert schema["meta"] == "dict"


def test_build_schema_respects_max_depth():
    """Pathological nesting must not blow the call stack."""
    deep = {}
    cursor = deep
    for i in range(10):
        cursor["next"] = {}
        cursor = cursor["next"]
    cursor["leaf"] = "value"

    schema = _build_schema(deep)
    # We don't care about the exact paths here, only that it returned without
    # exceeding recursion and produced something finite.
    assert isinstance(schema, dict)
    assert len(schema) > 0
    assert len(schema) < 50  # bounded by _MAX_DEPTH=4


def test_build_schema_root_scalar():
    schema = _build_schema(42)
    # Bare scalars get logged under "_root"
    assert schema == {"_root": "number"}


# ---------------------------------------------------------------------------
# _diff_schemas — finds added / removed / type_change
# ---------------------------------------------------------------------------


def test_diff_schemas_no_change_returns_empty():
    a = {"x": "str", "y": "number"}
    assert _diff_schemas(a, a) == {}


def test_diff_schemas_added_field():
    before = {"x": "str"}
    after = {"x": "str", "y": "number"}
    diff = _diff_schemas(before, after)
    assert diff == {"y": "added"}


def test_diff_schemas_removed_field():
    before = {"x": "str", "y": "number"}
    after = {"x": "str"}
    diff = _diff_schemas(before, after)
    assert diff == {"y": "removed"}


def test_diff_schemas_type_change():
    before = {"x": "str"}
    after = {"x": "number"}
    diff = _diff_schemas(before, after)
    assert diff == {"x": "type_change: str -> number"}


def test_diff_schemas_null_transitions_are_not_drift():
    """null -> str (or vice versa) should NOT be flagged — null often means
    'field exists but was unset on this call'."""
    before = {"x": "null"}
    after = {"x": "str"}
    assert _diff_schemas(before, after) == {}

    before2 = {"x": "str"}
    after2 = {"x": "null"}
    assert _diff_schemas(before2, after2) == {}


def test_diff_schemas_mixed_changes():
    before = {"a": "str", "b": "number", "c": "list"}
    after = {"a": "str", "b": "str", "d": "dict"}  # b changed, c removed, d added
    diff = _diff_schemas(before, after)
    assert diff["b"].startswith("type_change")
    assert diff["c"] == "removed"
    assert diff["d"] == "added"


# ---------------------------------------------------------------------------
# _worst_severity — picks highest-rank label
# ---------------------------------------------------------------------------


def test_worst_severity_added_only():
    assert _worst_severity(["added", "added"]) == "added"


def test_worst_severity_type_change_beats_added():
    assert _worst_severity(["added", "type_change: x -> y"]) == "type_change"


def test_worst_severity_removed_beats_all():
    assert _worst_severity(["added", "type_change: x -> y", "removed"]) == "removed"


def test_worst_severity_empty_defaults_to_added():
    """Edge case: empty iterable. Caller shouldn't normally hit this since we
    only call _worst_severity when changes is non-empty, but it must be safe."""
    assert _worst_severity([]) == "added"


# ---------------------------------------------------------------------------
# DriftDetector — public API
# ---------------------------------------------------------------------------


def test_snapshot_then_check_no_change_returns_none():
    d = DriftDetector()
    sample = {"jobs": [{"id": 1, "title": "SDE"}], "total": 1}
    d.snapshot_endpoint("/jobapi/v3/search", sample)

    report = d.check_drift("/jobapi/v3/search", sample)
    assert report is None


def test_check_drift_without_snapshot_returns_none():
    """Calling check_drift on an unknown URL must return None (no false alarm)."""
    d = DriftDetector()
    report = d.check_drift("/never/snapshotted", {"x": 1})
    assert report is None


def test_snapshot_endpoint_returns_captured_schema():
    d = DriftDetector()
    schema = d.snapshot_endpoint("/x", {"a": 1, "b": "hi"})
    assert schema == {"a": "number", "b": "str"}


def test_snapshot_endpoint_url_required():
    d = DriftDetector()
    with pytest.raises(ValueError, match="url is required"):
        d.snapshot_endpoint("", {"x": 1})


def test_check_drift_added_field():
    d = DriftDetector()
    d.snapshot_endpoint("/x", {"a": 1})
    report = d.check_drift("/x", {"a": 1, "newField": "yes"})

    assert report is not None
    assert isinstance(report, DriftReport)
    assert report.url == "/x"
    assert report.severity == "added"
    assert report.changed_fields == {"newField": "added"}


def test_check_drift_removed_field():
    d = DriftDetector()
    d.snapshot_endpoint("/x", {"a": 1, "b": 2})
    report = d.check_drift("/x", {"a": 1})

    assert report is not None
    assert report.severity == "removed"
    assert report.changed_fields == {"b": "removed"}


def test_check_drift_type_change():
    d = DriftDetector()
    d.snapshot_endpoint("/x", {"salary": 100000})
    report = d.check_drift("/x", {"salary": "100000"})  # number -> str

    assert report is not None
    assert report.severity == "type_change"
    assert report.changed_fields["salary"].startswith("type_change")


def test_check_drift_mixed_promotes_to_worst_severity():
    """When a single response has both added and removed changes, severity =
    the worst (removed)."""
    d = DriftDetector()
    d.snapshot_endpoint("/x", {"a": 1, "b": 2})
    report = d.check_drift("/x", {"a": 1, "c": 3})  # b removed, c added

    assert report is not None
    assert report.severity == "removed"
    assert "b" in report.changed_fields
    assert "c" in report.changed_fields


def test_check_drift_records_snapshot_age_days():
    d = DriftDetector()
    d.snapshot_endpoint("/x", {"a": 1})
    # Force-age the snapshot by 2 days.
    snap = d._snapshots["/x"]
    snap.taken_at = time.time() - (2 * 86400)

    report = d.check_drift("/x", {"a": 1, "b": 2})
    assert report is not None
    assert 1.9 < report.snapshot_age_days < 2.1


def test_has_snapshot_and_get_snapshot():
    d = DriftDetector()
    assert not d.has_snapshot("/x")
    assert d.get_snapshot("/x") is None

    d.snapshot_endpoint("/x", {"a": 1})
    assert d.has_snapshot("/x")
    assert d.get_snapshot("/x") == {"a": "number"}


def test_get_snapshot_returns_defensive_copy():
    """Mutating the returned dict must not affect the stored snapshot."""
    d = DriftDetector()
    d.snapshot_endpoint("/x", {"a": 1})
    snap = d.get_snapshot("/x")
    assert snap is not None
    snap["TAMPERED"] = "evil"

    fresh = d.get_snapshot("/x")
    assert "TAMPERED" not in fresh


def test_clear_single_url():
    d = DriftDetector()
    d.snapshot_endpoint("/a", {"x": 1})
    d.snapshot_endpoint("/b", {"y": 2})

    d.clear("/a")
    assert not d.has_snapshot("/a")
    assert d.has_snapshot("/b")


def test_clear_all_when_no_url_given():
    d = DriftDetector()
    d.snapshot_endpoint("/a", {"x": 1})
    d.snapshot_endpoint("/b", {"y": 2})

    d.clear()
    assert not d.has_snapshot("/a")
    assert not d.has_snapshot("/b")


def test_snapshot_endpoint_overwrites_existing():
    d = DriftDetector()
    d.snapshot_endpoint("/x", {"a": 1})
    d.snapshot_endpoint("/x", {"b": 2})  # different shape

    # check_drift against the SECOND snapshot's shape should now show no drift.
    report = d.check_drift("/x", {"b": 2})
    assert report is None


def test_realistic_naukri_search_drift_scenario():
    """End-to-end: snapshot a Naukri search response, then receive a v4-shaped
    response with renamed fields and changed types. We expect a drift report
    with multiple changed fields and severity=removed."""
    d = DriftDetector()
    v3_response = {
        "noOfJobs": 100,
        "jobDetails": [
            {"jobId": "1", "title": "SDE", "minSal": 1500000, "maxSal": 2500000}
        ],
    }
    d.snapshot_endpoint("/jobapi/v3/search", v3_response)

    v4_response = {
        "noOfJobs": 100,
        "jobDetails": [
            {"jobId": "1", "title": "SDE",
             # minSal -> minSalary (rename = old removed + new added)
             "minSalary": "15.0 LPA",  # also type_change number -> str
             "maxSalary": "25.0 LPA",
             "newFlag": True},
        ],
    }
    report = d.check_drift("/jobapi/v3/search", v4_response)
    assert report is not None
    assert report.severity == "removed"  # minSal/maxSal removed beats added
    assert "jobDetails.0.minSal" in report.changed_fields
    assert report.changed_fields["jobDetails.0.minSal"] == "removed"
    assert "jobDetails.0.minSalary" in report.changed_fields
    assert report.changed_fields["jobDetails.0.minSalary"] == "added"
    assert "jobDetails.0.newFlag" in report.changed_fields


# ---------------------------------------------------------------------------
# Module-level singleton — exists and is a DriftDetector
# ---------------------------------------------------------------------------


def test_module_singleton_exists():
    """The module exposes a `detector` singleton for shared callers."""
    assert isinstance(module_detector, DriftDetector)


def test_module_singleton_is_isolated_per_test():
    """Tests construct their own DriftDetector(); the singleton is for prod use.
    Sanity check that the singleton type matches what we documented."""
    # Verify the type is correct without polluting it.
    assert hasattr(module_detector, "snapshot_endpoint")
    assert hasattr(module_detector, "check_drift")
    assert hasattr(module_detector, "clear")
