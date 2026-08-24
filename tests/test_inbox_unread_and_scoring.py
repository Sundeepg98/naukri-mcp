"""The inbox reported on the CALL where the caller was reading the RESULT.

Four defects, three of them the same disease. All four were MEASURED against
the live account 2026-08-24 before a line was changed:

    naukri_list_inbox(unread_only=True, limit=20)
        -> total 66, count 3, unread 11, has_more true
    naukri_triage_inbox(unread_only=True, limit=20)
        -> unread_in_inbox 11, returned 3, scored true, scoring_error null,
           fit_score null on ALL 3 rows
    naukri_list_inbox(unread_only=True, limit=25)
        -> HTTP 424 "pageSize should be in between 1-20"

1. THE PAGE WAS FILTERED CLIENT-SIDE. The REST GET builds pageSize / pageNo /
   mailType and no read-state parameter, because the endpoint HAS none --
   probed 2026-08-24 from a pageSize=20&pageNo=1 baseline of 20 rows / 3
   unread / unreadCount 11 / total 66: `isUnread`, `unreadOnly`, `unread`,
   `readStatus` and `isRead` each answered 200 with the byte-same mixed page,
   and `isUnRead` answered 404. So `unread_only` drops read rows AFTER the
   fetch, which on a newest-first inbox empties the page. Every caller then
   reported 3 of 11 and none of them said so.

2. pageSize AND limit ARE TWO DIFFERENT MEASUREMENTS. `validate_limit` clamps
   to 50, the endpoint caps a page at 20, and the caller's number went
   straight to the wire -- so limit=25 came back as a raw HTTP 424 rather than
   as 25 messages.

3. THE SILENT NULL. `FitScore.to_dict()` has keys bonuses / experience_match /
   overall_score / reasons / recommendation / skill_match. It has never had a
   `fit_score`. Reading `.get("fit_score")` therefore returned None on every
   row, raised nothing, never reached the `except` beside it, and logged no
   warning. A missing key is not an error to `.get`, which is the whole reason
   this survived: nothing anywhere said it was wrong.

4. `scored` REPORTED ON THE PROFILE READ. It was set True before any row was
   scored and cleared only when the profile could not be read -- so a run
   whose profile loaded fine and whose rows then all failed shipped
   `scored: true` beside a column of nulls.

Every test here is PURE: no network, no browser, no file I/O. The scoring
control deliberately does NOT mock `to_dict()`; it calls the real engine.
Defect 3 got in because a fixture agreed with the code and both were wrong.

No personal data appears in this file: every id, name, company and line of
message text is synthetic.
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.api import NaukriAPIError
from naukri_server.tools.inbox import (
    INBOX_MAX_PAGE_SIZE,
    INBOX_MAX_PAGES_WALKED,
    _fetch_inbox,
    naukri_triage_inbox,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures. REST-shaped rows, synthetic content.
# ---------------------------------------------------------------------------

JOB_KEY_SKILLS = "Node.Js, React.Js, Typescript, PostgreSQL"
JOB_SALARY = "15.0 lacs - 30.0 lacs"

PROFILE = {
    "key_skills": "Node.js, React.js, TypeScript, MongoDB, Express.js",
    "total_experience": "6 years 0 months",
    "current_location": "Bengaluru",
    "expected_ctc": "25 lacs",
}


def _row(index: int, unread: bool) -> dict:
    """One REST-shaped inbox row. Everything in it is invented."""
    return {
        "mailId": "mail-%04d" % index,
        "messageId": "msg-%04d" % index,
        "conversationId": "conv-%04d" % index,
        "uniqueId": "uid-%04d" % index,
        "dateTime": "2026-08-%02dT10:00:00+0530" % (1 + index % 28),
        "isRead": 0 if unread else 1,
        "messageType": "recruiterMail",
        "subject": "Job | Role %04d" % index,
        "sender": [{"name": "Employer", "senderId": "sender-%04d" % index}],
        "vCardInfo": {"vname": "Recruiter", "designation": None},
        "jobDetails": {
            "jobTitle": "Backend Engineer",
            "jobExperience": {"minimum": 4, "maximum": 8},
            "jobCtc": {"minimum": 15.0, "maximum": 30.0},
            "jobLocation": "Bengaluru",
            "workMode": "Hybrid",
            "jobKeySkills": JOB_KEY_SKILLS,
            "questionairreAttached": False,
            "applyUrlData": {"jobId": "job-%04d" % index},
            "miscJobDetails": {
                "jobDesc": "<p>A synthetic role description.</p>",
                "dynamicContentLine": "A synthetic opening line.",
                "salary": JOB_SALARY,
                "consultant": False,
                "consultantName": "",
                "clientName": "",
            },
        },
        "companyDetails": {
            "companyName": "Employer",
            "ambitionBoxRating": 3.5,
            "ambitionBoxReviews": 100,
            "ambitionBoxUrl": "",
        },
    }


def _build_pages(layout) -> list:
    """`layout` is [(rows_on_page, unread_on_page), ...] -> list of row lists.

    The unread rows sit at the END of each page, which is the shape that
    breaks a client-side filter reading a truncated page.
    """
    pages, index = [], 0
    for size, unread in layout:
        page = []
        for slot in range(size):
            index += 1
            page.append(_row(index, unread=slot >= size - unread))
        pages.append(page)
    return pages


def _paged_endpoint(pages, total, unread, max_page_size=INBOX_MAX_PAGE_SIZE):
    """An `api_client.get` stand-in that PAGES and enforces the real bound.

    It answers HTTP 424 for a pageSize above `max_page_size`, exactly as the
    live endpoint did on 2026-08-24 -- so a test that passes here could not
    have produced that 424.
    """
    calls = []

    async def _get(path, params=None, extra_headers=None):
        params = dict(params or {})
        calls.append(params)
        size = int(params.get("pageSize", 0))
        if size > max_page_size or size < 1:
            raise NaukriAPIError(424, "pageSize should be in between 1-20")
        number = int(params.get("pageNo", 1))
        rows = pages[number - 1] if 1 <= number <= len(pages) else []
        return {
            "inbox": rows,
            "totalCount": total,
            "unreadCount": unread,
            "relevantCount": 0,
            "hasPowerNvites": False,
        }

    return _get, calls


def _patch_get(endpoint):
    return patch("naukri_server.tools.inbox.api_client.get", new=endpoint)


def _patch_post_bounded(max_page_size=INBOX_MAX_PAGE_SIZE):
    """The POST fallback, enforcing the SAME bound.

    Both endpoints answered 424 above 20 on 2026-08-24 -- the tool catches any
    GET failure and retries on POST, so the measured 424 could only have come
    back to the caller if POST rejected it too. Patching it here also keeps
    this test hermetic: nothing can reach a real session.
    """
    async def _post(path, body=None, **kw):
        body = body or {}
        if int(body.get("pageSize", 0)) > max_page_size:
            raise NaukriAPIError(424, "pageSize should be in between 1-20")
        return {"successResponse": {"inbox": [], "total": 66, "unread": 11}}

    return patch("naukri_server.tools.inbox.api_client.post", new=_post)


def _patch_profile(value=None, error=None):
    mock = AsyncMock()
    if error is not None:
        mock.side_effect = error
    else:
        mock.return_value = dict(value if value is not None else PROFILE)
    return patch("naukri_server.services.profile_service.get_cached_profile", new=mock)


#: The live distribution: 66 mails over pages of 20/20/20/6, 11 of them unread,
#: only 3 of those on page one. This is the fixture the headline defect needs.
LIVE_LAYOUT = [(20, 3), (20, 4), (20, 4), (6, 0)]


# =====================================================================
# CONTROL 1 -- the headline
# =====================================================================

class TestUnreadOnlyListsEveryUnreadItCounts:

    async def test_it_lists_all_eleven_not_page_ones_three(self):
        """The measured failure: 11 unread, 3 on page 1, and the tool said 3."""
        get, calls = _paged_endpoint(_build_pages(LIVE_LAYOUT), total=66, unread=11)
        with _patch_get(get):
            out = await _fetch_inbox(limit=20, unread_only=True)

        assert out["count"] == 11, (
            "unread_only returned %d of the 11 unread the same response "
            "counted; page 1 holds only 3 of them" % out["count"])
        assert out["listed"] == 11
        assert all(not m["is_read"] for m in out["messages"])
        assert len({m["mail_id"] for m in out["messages"]}) == 11

    async def test_a_partial_answer_carries_a_not_listed_count(self):
        """Fewer rows than unread is survivable. SILENCE about it is not."""
        get, calls = _paged_endpoint(_build_pages(LIVE_LAYOUT), total=66, unread=11)
        with _patch_get(get):
            out = await _fetch_inbox(limit=20, unread_only=True)

        assert out["not_listed"] == 0, "a complete answer must claim no gap"
        assert out["unread"] == 11
        assert out["has_more"] is False

    async def test_triage_returns_eleven_of_eleven(self):
        """The tool that exists FOR triage returned 3 of 11."""
        get, calls = _paged_endpoint(_build_pages(LIVE_LAYOUT), total=66, unread=11)
        with _patch_get(get), _patch_profile():
            out = await naukri_triage_inbox(limit=20, unread_only=True)

        assert out["status"] == "success"
        assert out["unread_in_inbox"] == 11
        assert out["returned"] == 11
        assert out["not_listed"] == 0
        assert all(r["unread"] for r in out["messages"])


# =====================================================================
# CONTROL 2 -- the walk stops when the count is satisfied
# =====================================================================

class TestTheWalkStopsWhenSatisfied:

    async def test_it_does_not_fetch_a_page_it_does_not_need(self):
        """All 11 unread live on pages 1-3. Page 4 must never be asked for."""
        get, calls = _paged_endpoint(_build_pages(LIVE_LAYOUT), total=66, unread=11)
        with _patch_get(get):
            out = await _fetch_inbox(limit=20, unread_only=True)

        assert [c["pageNo"] for c in calls] == ["1", "2", "3"], (
            "the walk asked for pages %s; the 11th unread is on page 3"
            % [c["pageNo"] for c in calls])
        assert out["pages_walked"] == 3

    async def test_the_callers_limit_also_stops_it(self):
        """A caller wanting 5 must not pay for the walk to 11."""
        get, calls = _paged_endpoint(_build_pages(LIVE_LAYOUT), total=66, unread=11)
        with _patch_get(get):
            out = await _fetch_inbox(limit=5, unread_only=True)

        assert out["count"] == 5
        assert len(calls) == 2, "5 unread are reached on page 2 (3 + 4)"
        # It stopped early by CHOICE, and says the rest exist.
        assert out["not_listed"] == 6
        assert out["has_more"] is True

    async def test_CONTROL_a_mixed_call_still_fetches_exactly_one_page(self):
        """The walk must be the unread path's, not everybody's -- a pager that
        runs on every call would buy this fix with three extra round trips on
        every ordinary list."""
        get, calls = _paged_endpoint(_build_pages(LIVE_LAYOUT), total=66, unread=11)
        with _patch_get(get):
            out = await _fetch_inbox(limit=20, unread_only=False)

        assert len(calls) == 1
        assert out["count"] == 20
        assert out["pages_walked"] == 1


# =====================================================================
# CONTROL 3 -- exhaustion is reported, not hidden
# =====================================================================

class TestExhaustionIsReported:

    async def test_pages_run_out_and_the_remainder_is_named(self):
        """The server counts 11 unread; the pages only ever carry 7. The walk
        must end at the data and SAY it came up four short."""
        layout = [(20, 3), (20, 4), (5, 0)]  # page 3 is short: end of data
        get, calls = _paged_endpoint(_build_pages(layout), total=66, unread=11)
        with _patch_get(get):
            out = await _fetch_inbox(limit=20, unread_only=True)

        assert out["count"] == 7
        assert out["unread"] == 11
        assert out["not_listed"] == 4, (
            "7 rows against a count of 11 must report a gap of 4, not silence")
        assert out["has_more"] is True
        assert len(calls) == 3

    async def test_the_walk_is_bounded_when_pages_never_run_short(self):
        """A server that keeps answering full pages of nothing useful must not
        spin. The bound is INBOX_MAX_PAGES_WALKED round trips."""
        layout = [(20, 0)] * 40  # 40 full pages, not one unread row on them
        get, calls = _paged_endpoint(_build_pages(layout), total=100000, unread=11)
        with _patch_get(get):
            out = await _fetch_inbox(limit=20, unread_only=True)

        assert len(calls) == INBOX_MAX_PAGES_WALKED
        assert out["pages_walked"] == INBOX_MAX_PAGES_WALKED
        assert out["count"] == 0
        assert out["not_listed"] == 11

    async def test_triage_reports_the_same_gap(self):
        layout = [(20, 3), (20, 4), (5, 0)]
        get, calls = _paged_endpoint(_build_pages(layout), total=66, unread=11)
        with _patch_get(get), _patch_profile():
            out = await naukri_triage_inbox(limit=20, unread_only=True)

        assert out["returned"] == 7
        assert out["unread_in_inbox"] == 11
        assert out["not_listed"] == 4


# =====================================================================
# CONTROL 4 -- the page size is the endpoint's, not the caller's
# =====================================================================

class TestPageSizeIsCappedAtTheEndpointsBound:

    async def test_limit_25_is_clamped_and_never_424s(self):
        """The measured failure: limit=25 reached the wire and came back as
        HTTP 424 "pageSize should be in between 1-20".

        BOTH endpoints are bounded here. The GET failure is caught and retried
        on POST, so an over-sized request only reaches the caller as a 424 if
        the fallback rejects it too -- which is what the live call showed.
        """
        get, calls = _paged_endpoint(_build_pages(LIVE_LAYOUT), total=66, unread=11)
        with _patch_get(get), _patch_post_bounded():
            out = await _fetch_inbox(limit=25, unread_only=False)

        assert out["status"] == "success"
        assert calls, "no request was made at all"
        assert all(int(c["pageSize"]) <= INBOX_MAX_PAGE_SIZE for c in calls), (
            "asked the endpoint for pageSize %s against a bound of %d"
            % ([c["pageSize"] for c in calls], INBOX_MAX_PAGE_SIZE))
        assert out["page_size"] <= INBOX_MAX_PAGE_SIZE
        assert out["count"] == 20

    async def test_the_maximum_limit_is_clamped_too(self):
        """validate_limit's ceiling is 50; this endpoint's is 20."""
        get, calls = _paged_endpoint(_build_pages(LIVE_LAYOUT), total=66, unread=11)
        with _patch_get(get):
            out = await _fetch_inbox(limit=50, unread_only=False)

        assert int(calls[0]["pageSize"]) == INBOX_MAX_PAGE_SIZE
        assert out["status"] == "success"

    async def test_the_post_fallback_is_capped_as_well(self):
        """Both endpoints answered 424 above 20, so the fallback needs the
        cap too -- otherwise the fix only moves the failure one path over."""
        async def _get(path, params=None, extra_headers=None):
            raise NaukriAPIError(424, "pageSize should be in between 1-20")

        with _patch_get(_get), \
             patch("naukri_server.tools.inbox.api_client.post",
                   new_callable=AsyncMock) as post:
            post.return_value = {"successResponse": {
                "inbox": [], "total": 66, "unread": 11}}
            await _fetch_inbox(limit=25, unread_only=False)

        body = post.await_args.kwargs.get("body") or post.await_args.args[1]
        assert body["pageSize"] <= INBOX_MAX_PAGE_SIZE

    async def test_CONTROL_a_small_limit_is_left_alone(self):
        """The cap must be a ceiling, not a floor -- clamping every request up
        to 20 would fetch four times the rows a five-row caller asked for."""
        get, calls = _paged_endpoint(_build_pages(LIVE_LAYOUT), total=66, unread=11)
        with _patch_get(get):
            out = await _fetch_inbox(limit=5, unread_only=False)

        assert int(calls[0]["pageSize"]) == 5
        assert out["count"] == 5


# =====================================================================
# CONTROL 5 -- the silent null, pinned against the REAL engine
# =====================================================================

class TestFitScoreIsTheEnginesNumber:
    """No mocking of `to_dict()` anywhere in this class, on purpose.

    The defect was a fixture agreeing with the code while both were wrong. So
    the expected value here is computed by CALLING the shipped scorer, and a
    rename of its key breaks this test at the lookup.
    """

    @staticmethod
    def _engine_score():
        from naukri_server.scoring import parse_skills, score_job

        fit = score_job(
            parse_skills(JOB_KEY_SKILLS),
            parse_skills(PROFILE["key_skills"]),
            "",
            PROFILE["total_experience"],
            job_location="Bengaluru",
            profile_location=PROFILE["current_location"],
            job_work_mode="Hybrid",
            job_salary=JOB_SALARY,
            profile_expected_ctc=PROFILE["expected_ctc"],
            experience_min=4,
            experience_max=8,
        )
        # A KeyError here is the point: if `overall_score` is ever renamed,
        # this test must go red rather than quietly agree with a None.
        return fit.to_dict()["overall_score"]

    async def test_the_row_carries_the_engines_overall_score(self):
        expected = self._engine_score()
        get, calls = _paged_endpoint(_build_pages([(1, 1)]), total=1, unread=1)
        with _patch_get(get), _patch_profile():
            out = await naukri_triage_inbox(limit=10, unread_only=True)

        assert out["returned"] == 1
        got = out["messages"][0]["fit_score"]
        assert got is not None, (
            "fit_score is null on a row the engine scored -- the read is "
            "against a key the result does not have")
        assert got == expected, "tool said %r, engine said %r" % (got, expected)

    async def test_the_defects_own_signature_is_still_true_of_the_engine(self):
        """`to_dict()` has no `fit_score` key, which is why `.get("fit_score")`
        returned None and raised nothing. If this ever fails, the engine grew
        the key and the tool's read should be re-examined -- not this test
        quietly relaxed."""
        from naukri_server.scoring import parse_skills, score_job

        keys = set(score_job(parse_skills("Node.js"), parse_skills("Node.js"),
                             "", "5 years").to_dict())
        assert "fit_score" not in keys, sorted(keys)
        assert "overall_score" in keys, sorted(keys)

    async def test_scored_and_scored_rows_agree_with_the_rows(self):
        get, calls = _paged_endpoint(_build_pages([(3, 3)]), total=3, unread=3)
        with _patch_get(get), _patch_profile():
            out = await naukri_triage_inbox(limit=10, unread_only=True)

        assert out["scored"] is True
        assert out["scored_rows"] == 3
        assert out["scoring_attempted"] == 3
        assert all(r["fit_score"] is not None for r in out["messages"])


# =====================================================================
# CONTROL 6 -- `scored` reports on the rows, not on the profile read
# =====================================================================

def _scorer_that_always_raises():
    def _boom(*a, **kw):
        raise RuntimeError("scorer exploded")
    return patch("naukri_server.scoring.score_job", new=_boom)


class TestScoredReportsOnTheResult:

    async def test_scored_is_false_when_no_row_could_be_scored(self):
        """The profile read SUCCEEDS here and every row then fails. The old
        flag was set True before the first row and only ever cleared by a
        profile failure, so this shipped `scored: true`."""
        get, calls = _paged_endpoint(_build_pages([(3, 3)]), total=3, unread=3)
        with _patch_get(get), _patch_profile(), _scorer_that_always_raises():
            out = await naukri_triage_inbox(limit=10, unread_only=True)

        assert out["returned"] == 3
        assert out["scored"] is False, (
            "scored: true beside %d rows, none of which carries a score"
            % out["returned"])
        assert out["scored_rows"] == 0
        assert out["scoring_attempted"] == 3

    async def test_all_null_scores_beside_scored_true_is_unrepresentable(self):
        """The exact combination the live call shipped: three rows, three null
        fit scores, `scored: true`. It must not be constructible."""
        get, calls = _paged_endpoint(_build_pages([(3, 3)]), total=3, unread=3)
        with _patch_get(get), _patch_profile(), _scorer_that_always_raises():
            out = await naukri_triage_inbox(limit=10, unread_only=True)

        rows = out["messages"]
        assert rows, "no rows means this proves nothing"
        if all(r["fit_score"] is None for r in rows):
            assert out["scored"] is False

    async def test_a_partial_failure_reports_the_partial_number(self):
        """Some rows scored, some did not: the pair must say which."""
        from naukri_server import scoring as scoring_module

        real = scoring_module.score_job
        state = {"n": 0}

        def _every_other(*a, **kw):
            state["n"] += 1
            if state["n"] % 2 == 0:
                raise RuntimeError("scorer exploded")
            return real(*a, **kw)

        get, calls = _paged_endpoint(_build_pages([(4, 4)]), total=4, unread=4)
        with _patch_get(get), _patch_profile(), \
             patch("naukri_server.scoring.score_job", new=_every_other):
            out = await naukri_triage_inbox(limit=10, unread_only=True)

        assert out["scoring_attempted"] == 4
        assert out["scored_rows"] == 2
        assert out["scored"] is True
        assert sum(1 for r in out["messages"] if r["fit_score"] is not None) == 2


# =====================================================================
# CONTROL 7 -- the two causes stay distinguishable
# =====================================================================

class TestScoringErrorStillNamesTheProfileRead:

    async def test_a_failed_profile_read_sets_scoring_error(self):
        get, calls = _paged_endpoint(_build_pages([(3, 3)]), total=3, unread=3)
        with _patch_get(get), _patch_profile(error=RuntimeError("profile API down")):
            out = await naukri_triage_inbox(limit=10, unread_only=True)

        assert out["scored"] is False
        assert out["scoring_error"] is not None
        assert "profile API down" in out["scoring_error"]
        assert out["scoring_attempted"] == 0, (
            "nothing can be attempted without a profile; a non-zero attempt "
            "count here would make the two causes look alike")
        assert out["returned"] == 3

    async def test_failed_rows_leave_scoring_error_alone(self):
        """The mirror case. Both give `scored: false`, and `scoring_error`
        plus `scoring_attempted` are what tell them apart -- one needs the
        profile fixed, the other needs the scorer fixed."""
        get, calls = _paged_endpoint(_build_pages([(3, 3)]), total=3, unread=3)
        with _patch_get(get), _patch_profile(), _scorer_that_always_raises():
            out = await naukri_triage_inbox(limit=10, unread_only=True)

        assert out["scored"] is False
        assert out["scoring_error"] is None, (
            "a row-level failure must not be reported as a profile failure")
        assert out["scoring_attempted"] == 3

    async def test_the_two_causes_do_not_produce_the_same_envelope(self):
        get, calls = _paged_endpoint(_build_pages([(3, 3)]), total=3, unread=3)
        with _patch_get(get), _patch_profile(error=RuntimeError("profile API down")):
            profile_failure = await naukri_triage_inbox(limit=10, unread_only=True)

        get, calls = _paged_endpoint(_build_pages([(3, 3)]), total=3, unread=3)
        with _patch_get(get), _patch_profile(), _scorer_that_always_raises():
            row_failure = await naukri_triage_inbox(limit=10, unread_only=True)

        assert profile_failure["scored"] == row_failure["scored"] is False
        signature = lambda r: (r["scoring_error"] is None, r["scoring_attempted"])
        assert signature(profile_failure) != signature(row_failure)
