"""Tests for Interface Segregation validation helper (models.validate_action_params)."""

import pytest

from naukri_server.models import validate_action_params


# ---------------------------------------------------------------------------
# Sample valid_per_action map (mirrors real _VALID_PARAMS_PER_ACTION)
# ---------------------------------------------------------------------------
_SAMPLE_MAP = {
    "list": {"status", "limit", "page"},
    "detail": {"job_id"},
    "purge": {"before_date", "dry_run"},
    "no_params": set(),
}


class TestValidateActionParamsDetectsUnused:
    """validate_action_params should return names of irrelevant non-None params."""

    def test_single_unused_param(self):
        provided = {"job_id": "123", "status": None}
        unused = validate_action_params("detail", provided, _SAMPLE_MAP)
        assert unused == []  # job_id is valid for detail

        # Now add an irrelevant param
        provided["status"] = "applied"
        unused = validate_action_params("detail", provided, _SAMPLE_MAP)
        assert "status" in unused
        assert "job_id" not in unused

    def test_multiple_unused_params(self):
        provided = {"job_id": "123", "status": "applied", "limit": 10, "before_date": "2026-01-01"}
        unused = validate_action_params("detail", provided, _SAMPLE_MAP)
        assert set(unused) == {"status", "limit", "before_date"}

    def test_action_field_excluded(self):
        """The 'action' key itself should never appear in unused list."""
        provided = {"action": "detail", "job_id": "123"}
        unused = validate_action_params("detail", provided, _SAMPLE_MAP)
        assert "action" not in unused

    def test_none_values_ignored(self):
        """Params with None value are not considered 'provided'."""
        provided = {"job_id": "123", "status": None, "limit": None, "before_date": None}
        unused = validate_action_params("detail", provided, _SAMPLE_MAP)
        assert unused == []

    def test_empty_valid_set_flags_all_non_none(self):
        """Actions with empty valid set should flag all non-None params."""
        provided = {"status": "applied", "limit": 10}
        unused = validate_action_params("no_params", provided, _SAMPLE_MAP)
        assert set(unused) == {"status", "limit"}

    def test_unknown_action_returns_empty(self):
        """If action is not in the map, return empty list (no validation possible)."""
        provided = {"job_id": "123", "status": "applied"}
        unused = validate_action_params("unknown_action", provided, _SAMPLE_MAP)
        assert unused == []


class TestValidateActionParamsReturnsEmpty:
    """validate_action_params should return [] when all provided params are valid."""

    def test_all_valid_params_for_list(self):
        provided = {"status": "applied", "limit": 10, "page": 2}
        unused = validate_action_params("list", provided, _SAMPLE_MAP)
        assert unused == []

    def test_all_valid_params_for_purge(self):
        provided = {"before_date": "2026-01-01", "dry_run": True}
        unused = validate_action_params("purge", provided, _SAMPLE_MAP)
        assert unused == []

    def test_no_params_provided(self):
        provided = {}
        unused = validate_action_params("list", provided, _SAMPLE_MAP)
        assert unused == []

    def test_all_none_params(self):
        provided = {"status": None, "job_id": None, "limit": None}
        unused = validate_action_params("list", provided, _SAMPLE_MAP)
        assert unused == []

    def test_subset_of_valid_params(self):
        provided = {"status": "applied"}
        unused = validate_action_params("list", provided, _SAMPLE_MAP)
        assert unused == []
