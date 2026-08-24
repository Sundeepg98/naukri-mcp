"""The daily brief must publish the AUTHORITATIVE unread total.

MEASURED on the live account 2026-08-24, twice, minutes apart:

    naukri_list_inbox(limit=5, unread_only=True)
        -> total=66  count=0  unread=11  messages=[]
    naukri_list_inbox(limit=5, unread_only=False)
        -> total=66  count=5  unread=11  messages=[5 rows, every is_read=true]

The inbox REST endpoint takes pageSize/pageNo/mailType and has NO unread
filter, so ``_fetch_inbox`` applies ``unread_only`` client-side to the page it
already fetched. When the newest rows happen to be read ones, that page yields
zero rows while the SAME response carries the authoritative ``unread: 11``.

The fetch SUCCEEDS, so the "a failed section is None, never a zero" guard in
``naukri_daily_brief`` never engages, and ``errors[]`` stays silent. The brief
then published ``unread_messages.count = 0`` -- a hard zero that reads as a
measurement of nothing while eleven recruiters wait.

The fix follows ``_triage_inbox``, which already defends against exactly this
hazard by publishing ``unread_in_inbox`` (authoritative) separately from
``returned`` (rows actually in hand). The brief now does the same, and says so
when it cannot list what it counted.
"""

import pytest

from naukri_server.services.daily_brief_service import build_recommended_actions
from naukri_server.tools.daily_brief import _unread_messages_section
from tests.test_daily_brief_deep import (
    _all_good_results,
    _run_brief_with_mocked_results,
)

#: The live 2026-08-24 shape, field for field. No message rows are reproduced
#: here -- the defect lives entirely in the scalars.
LIVE_FILTERED_PAGE = {
    "status": "success",
    "total": 66,
    "count": 0,
    "page": 1,
    "has_more": True,
    "unread": 11,
    "messages": [],
}


class TestBriefPublishesTheAuthoritativeUnreadTotal:

    @pytest.mark.asyncio
    async def test_brief_reports_eleven_not_the_filtered_pages_zero(self):
        results = _all_good_results()
        results[0] = dict(LIVE_FILTERED_PAGE)
        result = await _run_brief_with_mocked_results(results)
        assert result["unread_messages"]["count"] == 11

    @pytest.mark.asyncio
    async def test_brief_admits_it_could_not_list_them(self):
        """A count with no rows must SAY so, or the empty ``messages`` list
        silently contradicts the number beside it."""
        results = _all_good_results()
        results[0] = dict(LIVE_FILTERED_PAGE)
        result = await _run_brief_with_mocked_results(results)
        section = result["unread_messages"]
        assert section["listed"] == 0
        assert section["not_listed"] == 11
        assert "could not list" in section["note"]

    @pytest.mark.asyncio
    async def test_failed_inbox_is_still_none_never_a_zero(self):
        """The 2026-08-22 guard must survive this change."""
        results = _all_good_results()
        results[0] = Exception("inbox error")
        result = await _run_brief_with_mocked_results(results)
        assert result["unread_messages"] is None


class TestTheSectionBuilder:

    def test_rows_in_hand_are_a_floor_on_the_count(self):
        """``unread`` drifting BELOW the rows actually delivered must not make
        the brief report fewer unread than it is holding."""
        section = _unread_messages_section(
            {"count": 4, "unread": 0, "messages": [{}, {}, {}, {}]})
        assert section["count"] == 4
        assert section["not_listed"] == 0

    def test_page_without_an_unread_key_falls_back_to_its_own_count(self):
        """Not every caller's payload carries the authoritative figure; the
        page's own count is the honest fallback, not a zero."""
        section = _unread_messages_section({"count": 5, "messages": []})
        assert section["count"] == 5
        assert section["note"] is not None

    def test_a_fully_listed_page_carries_no_note(self):
        section = _unread_messages_section(
            {"count": 3, "unread": 3, "messages": [{}, {}, {}]})
        assert section["count"] == 3
        assert section["listed"] == 3
        assert section["not_listed"] == 0
        assert section["note"] is None

    def test_a_genuinely_empty_inbox_is_zero_with_no_note(self):
        section = _unread_messages_section({"count": 0, "unread": 0, "messages": []})
        assert section["count"] == 0
        assert section["note"] is None


class TestTheRecommendedAction:

    def test_action_names_the_unread_total_and_the_listing_gap(self):
        brief = {"unread_messages": _unread_messages_section(dict(LIVE_FILTERED_PAGE))}
        actions = build_recommended_actions(brief)
        inbox_actions = [a for a in actions if "unread recruiter" in a["action"]]
        assert len(inbox_actions) == 1
        action = inbox_actions[0]
        assert "11" in action["action"]
        assert "could not list" in action["action"]
        # A tool that cannot list them must not be the one recommended.
        assert "unread_only=True" not in action["tool"]

    def test_CONTROL_a_fully_listed_page_keeps_the_plain_action(self):
        """The gap wording must appear ONLY on a gap -- a check that fires
        every time certifies nothing."""
        brief = {"unread_messages": _unread_messages_section(
            {"count": 3, "unread": 3, "messages": [{}, {}, {}]})}
        actions = build_recommended_actions(brief)
        inbox_actions = [a for a in actions if "unread recruiter" in a["action"]]
        assert len(inbox_actions) == 1
        assert "could not list" not in inbox_actions[0]["action"]
        assert inbox_actions[0]["tool"] == "naukri_list_inbox(unread_only=True)"

    def test_CONTROL_no_unread_produces_no_inbox_action(self):
        brief = {"unread_messages": _unread_messages_section(
            {"count": 0, "unread": 0, "messages": []})}
        actions = build_recommended_actions(brief)
        assert not [a for a in actions if "unread recruiter" in a["action"]]
