"""recruiter_history counts must describe the portfolio, not the page.

Context (2026-08-22 live sweep): on the real account -- 162 applications across
115 distinct companies -- naukri_recruiter_history returned

    {"total_companies": 20, "responsive_count": 0, "unresponsive_count": 20}

Twenty was not a total. ``database.get_recruiter_history`` ended in ``LIMIT 20``
and the service published ``len(rows)`` as ``total_companies``, so the cap
became the answer. "How many companies have I applied to?" was off by 95, and
responsive/unresponsive described only the twenty highest-volume companies -- a
number that looks like a portfolio statistic and is not one.

These tests use 25 companies so the default limit of 20 actually bites; with 3
rows (the pre-existing fixture size) the bug is invisible, which is why it
survived a green suite.
"""

from unittest.mock import AsyncMock, patch

import pytest


def _rows(n, responsive_every=5):
    """n companies, most-applied first, every Nth one responsive."""
    return [
        {
            "company": "Company %02d" % i,
            "applications": n - i,
            "first_applied": "2026-01-01T00:00:00+00:00",
            "last_applied": "2026-02-01T00:00:00+00:00",
            "statuses": "applied,viewed" if i % responsive_every == 0 else "applied",
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_total_companies_is_the_portfolio_not_the_page():
    """THE regression: 25 companies, default limit 20 -> total must be 25."""
    from naukri_server.tools.tracking import _recruiter_history

    with patch("naukri_server.database.get_recruiter_history",
               new_callable=AsyncMock, return_value=_rows(25)):
        result = await _recruiter_history()

    assert result["total_companies"] == 25, "the cap must not become the total"
    assert len(result["companies"]) == 20, "the page is still 20 rows"
    assert result["returned"] == 20
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_response_counts_cover_every_company_not_just_the_page():
    """With a page of 5 out of 25, the counts must still describe all 25.

    Falsifiable on purpose: pre-fix there was no `limit` parameter at all, so
    this call raised TypeError. Computing the counts off `companies` after a
    slice would give 5 here instead of 25.
    """
    from naukri_server.tools.tracking import _recruiter_history

    rows = _rows(25)
    with patch("naukri_server.database.get_recruiter_history",
               new_callable=AsyncMock, return_value=rows):
        result = await _recruiter_history(limit=5)

    expected_responsive = sum(1 for r in rows if "viewed" in r["statuses"])
    assert len(result["companies"]) == 5, "the page really is 5 rows"
    assert result["responsive_count"] == expected_responsive
    assert result["unresponsive_count"] == 25 - expected_responsive
    assert result["responsive_count"] + result["unresponsive_count"] == 25


@pytest.mark.asyncio
async def test_limit_none_of_the_counts_change_when_the_page_shrinks():
    """Shrinking the page must move `returned`, never the *_count fields."""
    from naukri_server.tools.tracking import _recruiter_history

    with patch("naukri_server.database.get_recruiter_history",
               new_callable=AsyncMock, return_value=_rows(25)):
        wide = await _recruiter_history(limit=25)
        narrow = await _recruiter_history(limit=3)

    for field in ("total_companies", "responsive_count", "unresponsive_count"):
        assert wide[field] == narrow[field] == 25 if field == "total_companies" \
            else wide[field] == narrow[field]
    assert narrow["returned"] == 3 and narrow["has_more"] is True
    assert wide["returned"] == 25 and wide["has_more"] is False


@pytest.mark.asyncio
async def test_db_helper_no_longer_caps_at_twenty():
    """The SQL must not carry its own LIMIT -- paging is the caller's job.

    Reads the executed statement rather than the source text: the docstring
    names ``LIMIT 20`` to explain the bug, and a substring check over the whole
    function body matches that prose and passes for the wrong reason.
    """
    from unittest.mock import MagicMock

    from naukri_server import database

    executed = []

    class _FakeCursor:
        async def fetchall(self):
            return []

    class _FakeDb:
        async def execute(self, sql, *a):
            executed.append(sql)
            return _FakeCursor()

        async def close(self):
            return None

    with patch.object(database, "get_db", new_callable=AsyncMock,
                      return_value=_FakeDb()):
        await database.get_recruiter_history()

    assert executed, "the helper must actually run a query"
    sql = " ".join(executed[0].upper().split())
    assert "GROUP BY COMPANY" in sql, sql
    assert "LIMIT" not in sql, "the SQL still caps the result: %s" % sql
    _ = MagicMock  # keep the import honest if the fake is ever swapped out
