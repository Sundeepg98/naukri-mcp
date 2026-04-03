"""Tests for naukri_server.models — domain objects and helpers."""

import pytest

from naukri_server.models import ApplicationStatus, paginate


# ---------------------------------------------------------------------------
# ApplicationStatus.from_string
# ---------------------------------------------------------------------------

class TestApplicationStatusFromString:
    def test_parses_known_statuses(self):
        """All canonical status strings round-trip through from_string."""
        for member in ApplicationStatus:
            assert ApplicationStatus.from_string(member.value) == member

    def test_parses_with_whitespace_and_dashes(self):
        assert ApplicationStatus.from_string("  needs-input  ") == ApplicationStatus.NEEDS_INPUT
        assert ApplicationStatus.from_string("ALREADY_APPLIED") == ApplicationStatus.ALREADY_APPLIED
        assert ApplicationStatus.from_string("Viewed By Recruiter") == ApplicationStatus.VIEWED_BY_RECRUITER

    def test_returns_unknown_for_garbage(self):
        assert ApplicationStatus.from_string("not_a_real_status") == ApplicationStatus.UNKNOWN
        assert ApplicationStatus.from_string("xyz123") == ApplicationStatus.UNKNOWN

    def test_returns_unknown_for_empty(self):
        assert ApplicationStatus.from_string("") == ApplicationStatus.UNKNOWN


# ---------------------------------------------------------------------------
# paginate
# ---------------------------------------------------------------------------

class TestPaginate:
    def test_returns_correct_has_more(self):
        items = list(range(10))
        pagination, page_items = paginate(items, page=1, limit=5)
        assert pagination["has_more"] is True
        assert pagination["total"] == 10
        assert pagination["count"] == 5
        assert pagination["page"] == 1
        assert page_items == [0, 1, 2, 3, 4]

        pagination2, page_items2 = paginate(items, page=2, limit=5)
        assert pagination2["has_more"] is False
        assert pagination2["count"] == 5
        assert page_items2 == [5, 6, 7, 8, 9]

    def test_handles_empty_list(self):
        pagination, page_items = paginate([], page=1, limit=50)
        assert pagination["total"] == 0
        assert pagination["count"] == 0
        assert pagination["page"] == 1
        assert pagination["has_more"] is False
        assert page_items == []

    def test_page_beyond_items(self):
        items = [1, 2, 3]
        pagination, page_items = paginate(items, page=5, limit=10)
        assert pagination["total"] == 3
        assert pagination["count"] == 0
        assert pagination["has_more"] is False
        assert page_items == []
