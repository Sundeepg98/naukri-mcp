"""The inbox row read the wrong half of its own payload.

`naukri_list_inbox` returned 62 messages whose `preview` was the empty string
and whose `recruiter_name` was null on EVERY row -- so deciding which of them
deserved an answer meant opening all 62 one at a time. That was not a platform
limitation. It was eight fields read from positions the response does not use.

The two inbox endpoints disagree about shape and the server calls both. The
REST GET (`INBOX_REST_API`) is tried first and nests the job payload under
`jobDetails`; the nc-services POST template (`INBOX_API`) is the fallback and
returns a flatter row with a `titleText` the REST shape has no equivalent of.
Every one of the eight fields below was reading the fallback's position only,
so the preferred path silently returned nothing.

MEASURED against the live account 2026-08-23, 20 of 20 rows:

    preview                 ""     <- titleText, absent from the REST shape
    recruiter_name          null   <- vCardInfo.name; the key is `vname`
    experience_min/max      null   <- jobExperience.min; the keys are
                                      `minimum`/`maximum`, as jobCtc beside
                                      it already read correctly
    job_description         null   <- msg.miscJobDetails; it is nested under
                                      jobDetails
    has_screening_questions false  <- same nesting
    salary                  null   <- same nesting

THE FIXTURE IN THIS FILE IS THE REAL SHAPE, transcribed from that capture.
Its SHAPE is the capture's; its VALUES are not. The recruiter's name, the
requisition tag and the four 32-hex message ids were real when transcribed and
were replaced with synthetic values of identical shape on 2026-08-24, before
this repository was made public. Field count, nesting and value shape -- which
is the whole point of the fixture -- are unchanged.
That matters: a fixture invented to match the code under test would pass
whatever the code did. Every test here asserts TWO things -- that the new read
finds the real value, and that the OLD read position is empty on the same
fixture. The second half is what stops these tests certifying nothing; without
it they would pass just as happily against the broken code.

All tests are PURE -- no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# The fixture: one REST-shaped row, transcribed from the live capture.
# ---------------------------------------------------------------------------

JOB_DESC = "<p>We are having Urgent Openings for @ Tech Mahindra.</p>"
# Tag markup, no HTML entities: measured 0 of 20 live rows carry an entity, so
# a fixture full of them would be testing a shape the API does not send.
OPENER = "Hey, I am reaching out regarding the <strong>Fullstack</strong> role"


def _rest_row(**over):
    row = {
        "mailId": "aaaa1111bbbb2222cccc3333dddd4444",
        "messageId": "bbbb2222cccc3333dddd4444eeee5555",
        "conversationId": "cccc3333dddd4444eeee5555ffff6666",
        "dateTime": "2026-08-22T22:22:47+0530",
        "isRead": 1,
        "messageType": "recruiterMail",
        "subject": "Job | Software Engineer-Fullstack in Hyderabad, Bengaluru",
        "folderType": "inbox",
        "uniqueId": "dddd4444eeee5555ffff6666aaaa7777",
        "plainMailId": "10200300",
        "powerNvite": 0,
        "isRelevant": 1,
        "isApplied": False,
        "interested": True,
        "sender": [{"name": "Tech Mahindra", "senderId": "s1", "type": "recruiter"}],
        "receiver": {"name": "jobseeker", "type": "jobseeker"},
        # The recruiter's name is HERE, under `vname`, with an internal
        # requisition tag Naukri appends to the display name.
        "vCardInfo": {
            "vname": "Example Recruiter_TAG_100200",
            "designation": None,
            "location": None,
            "compid": "100007",
            "groupId": "2114",
            "showapplybutton": False,
        },
        "jobDetails": {
            "jobTitle": "Software Engineer-Fullstack",
            "jobExperience": {"minimum": 5, "maximum": 10},
            "jobCtc": {"minimum": 15.0, "maximum": 30.0},
            "jobLocation": "Hyderabad, Bengaluru",
            "workMode": "Hybrid",
            "jobKeySkills": "Node.Js, React.Js, Typescript",
            "questionairreAttached": True,
            "applyUrlData": {"jobId": "220826815714", "type": "NVITE"},
            "miscJobDetails": {
                "jobDesc": JOB_DESC,
                "dynamicContentLine": OPENER,
                "salary": "15.0 lacs - 30.0 lacs",
                "consultant": False,
                "consultantName": "Tech Mahindra",
                "clientName": "",
                "currency": "INR",
            },
        },
        "companyDetails": {
            "companyName": "Tech Mahindra",
            "ambitionBoxRating": 3.3,
            "ambitionBoxReviews": 44858,
            "ambitionBoxUrl": "https://www.ambitionbox.com/reviews/tech-mahindra-reviews",
        },
    }
    row.update(over)
    return row


def _rest_response(rows=None, total=62, unread=11):
    return {
        "inbox": rows if rows is not None else [_rest_row()],
        "totalCount": total,
        "unreadCount": unread,
        "relevantCount": 0,
        "hasPowerNvites": True,
    }


async def _fetch_one(row=None):
    """Run `_fetch_inbox` over the REST path and hand back the single row."""
    from naukri_server.tools.inbox import _fetch_inbox

    with patch("naukri_server.tools.inbox.api_client.get",
               new_callable=AsyncMock) as get:
        get.return_value = _rest_response([row if row is not None else _rest_row()])
        result = await _fetch_inbox(limit=20)

    assert result["messages"], "not vacuous - a row was parsed"
    return result["messages"][0]


# =====================================================================
# 0. The fixture really does reproduce the bug
# =====================================================================
# If these go green against the OLD positions, the fixture has drifted away
# from the shape the live API returns and every test below stops meaning
# anything.

class TestTheFixtureReproducesTheBug:

    def test_the_old_read_positions_are_all_empty_on_the_real_shape(self):
        row = _rest_row()

        assert row.get("titleText") is None, "REST has no titleText"
        assert (row.get("vCardInfo") or {}).get("name") is None
        assert (row["jobDetails"]["jobExperience"]).get("min") is None
        assert (row["jobDetails"]["jobExperience"]).get("max") is None
        assert (row.get("miscJobDetails") or {}).get("jobDesc") is None
        assert row.get("questionairreAttached") is None
        assert row.get("salary") is None

    def test_the_values_really_are_present_somewhere(self):
        """The other half: the payload DOES carry all of it."""
        row = _rest_row()
        misc = row["jobDetails"]["miscJobDetails"]

        assert row["vCardInfo"]["vname"]
        assert row["jobDetails"]["jobExperience"]["minimum"] == 5
        assert misc["jobDesc"] and misc["salary"] and misc["dynamicContentLine"]
        assert row["jobDetails"]["questionairreAttached"] is True


# =====================================================================
# 1. The eight fields, each read from where the value actually lives
# =====================================================================

class TestTheEightFieldPaths:

    async def test_recruiter_name_comes_from_vname(self):
        msg = await _fetch_one()
        assert msg["recruiter_name"] == "Example Recruiter"

    async def test_the_internal_requisition_tag_is_stripped(self):
        """`_TAG_100200` is Naukri's, not part of the person's name."""
        from naukri_server.tools.inbox import _recruiter_name

        assert _recruiter_name({"vCardInfo": {"vname": "Asha R_TAG_1"}}) == "Asha R"
        assert _recruiter_name({"vCardInfo": {"vname": "Asha R"}}) == "Asha R"

    async def test_an_anonymous_recruiter_is_None_not_empty_string(self):
        """"no name" and "we failed to read it" must stay distinguishable."""
        from naukri_server.tools.inbox import _recruiter_name

        assert _recruiter_name({"vCardInfo": {"vname": ""}}) is None
        assert _recruiter_name({"vCardInfo": {}}) is None
        assert _recruiter_name({}) is None
        assert _recruiter_name({"vCardInfo": {"vname": "_TAG_9"}}) is None

    async def test_preview_carries_the_recruiters_own_opening_line(self):
        msg = await _fetch_one()
        assert msg["preview"], "preview is still empty - the bug is back"
        assert "reaching out regarding" in msg["preview"]
        assert "<strong>" not in msg["preview"], "HTML survived into preview"

    async def test_preview_falls_back_to_the_job_description(self):
        row = _rest_row()
        del row["jobDetails"]["miscJobDetails"]["dynamicContentLine"]

        msg = await _fetch_one(row)
        assert "Urgent Openings" in msg["preview"]
        assert "<p>" not in msg["preview"]

    async def test_preview_is_empty_rather_than_invented(self):
        """No opener and no description means no preview -- not a guess."""
        row = _rest_row()
        row["jobDetails"]["miscJobDetails"].pop("dynamicContentLine")
        row["jobDetails"]["miscJobDetails"].pop("jobDesc")

        assert (await _fetch_one(row))["preview"] == ""

    async def test_preview_is_truncated(self):
        from naukri_server.tools.inbox import PREVIEW_CHARS

        row = _rest_row()
        row["jobDetails"]["miscJobDetails"]["dynamicContentLine"] = "x" * 5000

        assert len((await _fetch_one(row))["preview"]) == PREVIEW_CHARS

    async def test_experience_reads_the_long_key_names(self):
        msg = await _fetch_one()
        assert msg["job_details"]["experience_min"] == 5
        assert msg["job_details"]["experience_max"] == 10

    async def test_job_description_comes_from_the_nested_block(self):
        msg = await _fetch_one()
        assert msg["job_description"] == JOB_DESC

    async def test_screening_questions_are_detected(self):
        msg = await _fetch_one()
        assert msg["has_screening_questions"] is True

    async def test_screening_questions_stay_false_when_absent(self):
        """The control for the line above: it must not be true for everyone."""
        row = _rest_row()
        row["jobDetails"]["questionairreAttached"] = False

        assert (await _fetch_one(row))["has_screening_questions"] is False

    async def test_salary_comes_from_the_nested_block(self):
        msg = await _fetch_one()
        assert msg["salary"] == "15.0 lacs - 30.0 lacs"

    async def test_the_ambitionbox_url_is_no_longer_dropped(self):
        msg = await _fetch_one()
        assert msg["company_details"]["ambition_box_url"].endswith(
            "tech-mahindra-reviews")

    async def test_the_end_client_is_surfaced(self):
        """On a consultancy posting the sender is the agency, not the employer."""
        row = _rest_row()
        row["jobDetails"]["miscJobDetails"].update(
            {"consultant": True, "consultantName": "Redbox HR", "clientName": "Acme"})

        jd = (await _fetch_one(row))["job_details"]
        assert jd["consultant"] is True
        assert jd["consultant_name"] == "Redbox HR"
        assert jd["client_name"] == "Acme"

    async def test_designation_stays_null_rather_than_being_backfilled(self):
        """Naukri does not populate it. Inventing one from an adjacent field
        would be the same class of error as reading the wrong key."""
        assert (await _fetch_one())["recruiter_designation"] is None


# =====================================================================
# 2. The fallback shape must keep working
# =====================================================================
# The nc-services POST template is what answers when the REST GET fails, and
# it is the shape the old code was written against. Fixing the REST path at
# its expense would just move the outage.

class TestTheFlatFallbackShapeStillParses:

    async def test_the_flat_template_shape_still_reads(self):
        from naukri_server.tools.inbox import _fetch_inbox

        flat = {
            "mailId": "m1",
            "messageId": "id1",
            "subject": "Job | Backend",
            "titleText": "A recruiter wrote this line",
            "isRead": 0,
            "sender": [{"name": "Acme"}],
            "vCardInfo": {"vname": "Rita K"},
            "miscJobDetails": {"jobDesc": "<p>flat desc</p>"},
            "questionairreAttached": True,
            "salary": "10 lacs",
            "jobDetails": {"jobTitle": "Backend", "jobExperience": {"min": 2, "max": 4}},
        }
        with patch("naukri_server.tools.inbox.api_client.get",
                   new_callable=AsyncMock) as get, \
             patch("naukri_server.tools.inbox.api_client.post",
                   new_callable=AsyncMock) as post:
            get.side_effect = RuntimeError("REST down")
            post.return_value = {"successResponse": {
                "inbox": [flat], "total": 1, "unread": 1}}
            result = await _fetch_inbox(limit=20)

        msg = result["messages"][0]
        assert msg["preview"] == "A recruiter wrote this line"
        assert msg["recruiter_name"] == "Rita K"
        assert msg["job_description"] == "<p>flat desc</p>"
        assert msg["has_screening_questions"] is True
        assert msg["salary"] == "10 lacs"
        # The short experience keys are this shape's, and still read.
        assert msg["job_details"]["experience_min"] == 2
        assert msg["job_details"]["experience_max"] == 4


# =====================================================================
# 3. naukri_triage_inbox
# =====================================================================

class TestTriage:

    async def test_it_ranks_unread_first_then_fit(self):
        from naukri_server.tools.inbox import naukri_triage_inbox

        read_row = _rest_row(isRead=1, mailId="read", subject="Job | read one")
        unread_row = _rest_row(isRead=0, mailId="unread", subject="Job | unread one")

        with patch("naukri_server.tools.inbox.api_client.get",
                   new_callable=AsyncMock) as get:
            get.return_value = _rest_response([read_row, unread_row], total=2, unread=1)
            out = await naukri_triage_inbox(limit=10)

        assert out["status"] == "success"
        assert out["returned"] == 2
        assert out["messages"][0]["mail_id"] == "unread"
        assert out["messages"][0]["unread"] is True

    async def test_it_reports_the_counts_that_drive_a_decision(self):
        from naukri_server.tools.inbox import naukri_triage_inbox

        with patch("naukri_server.tools.inbox.api_client.get",
                   new_callable=AsyncMock) as get:
            get.return_value = _rest_response(total=62, unread=11)
            out = await naukri_triage_inbox(limit=10)

        assert out["total_in_inbox"] == 62
        assert out["unread_in_inbox"] == 11
        assert out["needs_answers"] == 1
        assert out["messages"][0]["recruiter_name"] == "Example Recruiter"
        assert out["messages"][0]["preview"]

    async def test_a_row_carries_no_full_job_description(self):
        """Triage exists to be cheap. 62 full JDs is not a summary."""
        from naukri_server.tools.inbox import naukri_triage_inbox

        with patch("naukri_server.tools.inbox.api_client.get",
                   new_callable=AsyncMock) as get:
            get.return_value = _rest_response()
            out = await naukri_triage_inbox(limit=10)

        assert "job_description" not in out["messages"][0]

    async def test_an_unreadable_profile_degrades_instead_of_failing(self):
        """The honesty branch: no profile means no scores, said out loud --
        not every row showing a null fit as though the jobs were all bad."""
        from naukri_server.tools.inbox import naukri_triage_inbox

        with patch("naukri_server.tools.inbox.api_client.get",
                   new_callable=AsyncMock) as get, \
             patch("naukri_server.services.profile_service.get_cached_profile",
                   new_callable=AsyncMock) as prof:
            get.return_value = _rest_response()
            prof.side_effect = RuntimeError("profile API down")
            out = await naukri_triage_inbox(limit=10)

        assert out["status"] == "success"
        assert out["scored"] is False
        assert "profile API down" in out["scoring_error"]
        assert out["returned"] == 1

    async def test_min_fit_score_drops_unscored_rows(self):
        """An unscored row cannot be said to clear a bar."""
        from naukri_server.tools.inbox import naukri_triage_inbox

        with patch("naukri_server.tools.inbox.api_client.get",
                   new_callable=AsyncMock) as get, \
             patch("naukri_server.services.profile_service.get_cached_profile",
                   new_callable=AsyncMock) as prof:
            get.return_value = _rest_response()
            prof.side_effect = RuntimeError("down")
            out = await naukri_triage_inbox(limit=10, min_fit_score=50)

        assert out["returned"] == 0

    async def test_a_short_page_ends_the_walk(self):
        """Found by this test going red: the pager trusted `has_more`, which is
        arithmetic on the server's `total`. With a total of 62 and pages that
        deliver one row, it kept asking and re-collected the SAME message seven
        times. A page shorter than requested is the end of the data, whatever
        the count says."""
        from naukri_server.tools.inbox import naukri_triage_inbox

        with patch("naukri_server.tools.inbox.api_client.get",
                   new_callable=AsyncMock) as get:
            # has_more stays true throughout: 62 total, one row per page.
            get.return_value = _rest_response(total=62, unread=11)
            out = await naukri_triage_inbox(limit=10)

        assert get.await_count == 1, (
            "the pager asked for %d pages over a one-row inbox"
            % get.await_count)
        assert out["returned"] == 1
        assert len({r["mail_id"] for r in out["messages"]}) == 1

    async def test_it_does_page_when_a_page_comes_back_full(self):
        """The control for the rule above: a FULL page must not end the walk,
        or the fix would have bought correctness by never paginating.

        A full page is 20 rows, not 50. The endpoint caps pageSize at 20 and
        answers HTTP 424 above it, so a 50-row page cannot occur -- the earlier
        fixture tested this rule against a page the server can never send. It
        passed only because the rows-requested limit and the wire pageSize are
        separate numbers. 55 rows is therefore three pages: 20, 20, then a
        short 15 that ends the walk.

        Count INBOX calls, not every call on the mock. `_triage_inbox` also
        reads the profile through the same `api_client.get`, and that read is
        served from `_profile_ttl_cache` when an earlier test in this module
        has warmed it. A bare `await_count` therefore asserted 2 or 3 depending
        purely on test ORDER -- green in a full-file run, red in isolation.
        Filtering by endpoint makes the count mean what the test says it means.
        """
        from naukri_server.tools.inbox import naukri_triage_inbox
        from naukri_server.config import INBOX_REST_API

        first = [_rest_row(mailId="a%d" % i) for i in range(20)]
        second = [_rest_row(mailId="b%d" % i) for i in range(20)]
        tail = [_rest_row(mailId="c%d" % i) for i in range(15)]

        with patch("naukri_server.tools.inbox.api_client.get",
                   new_callable=AsyncMock) as get:
            get.side_effect = [
                _rest_response(first, total=55, unread=0),
                _rest_response(second, total=55, unread=0),
                _rest_response(tail, total=55, unread=0),
            ]
            out = await naukri_triage_inbox(limit=55)

        inbox_calls = [c for c in get.call_args_list
                       if c.args and c.args[0] == INBOX_REST_API]
        assert len(inbox_calls) == 3, (
            "a full page must not end the walk; the pager made %d inbox call(s)"
            % len(inbox_calls))
        assert out["returned"] == 55

    @pytest.mark.parametrize("bad", [-1, 101])
    async def test_it_validates_min_fit_score(self, bad):
        from naukri_server.tools.inbox import naukri_triage_inbox

        out = await naukri_triage_inbox(min_fit_score=bad)
        assert out["error_code"] == "VALIDATION_ERROR"

    async def test_it_validates_limit(self):
        from naukri_server.tools.inbox import naukri_triage_inbox

        out = await naukri_triage_inbox(limit=0)
        assert out["error_code"] == "VALIDATION_ERROR"


# =====================================================================
# 4. It is registered, and it goes through the scrubber
# =====================================================================

class TestItIsRegistered:

    def test_triage_is_registered_and_wrapped(self):
        import naukri_server
        from naukri_server.tools.inbox import naukri_triage_inbox

        assert "naukri_triage_inbox" in naukri_server.mcp._tool_manager._tools
        assert getattr(naukri_triage_inbox, "__wrapped_by_watchdog__", False), (
            "not registered through the wrapper, so the path scrubber never "
            "sees its result"
        )

    def test_the_read_only_inbox_tools_are_all_still_there(self):
        """One going missing should fail loudly, not quietly."""
        import naukri_server

        for name in ("naukri_list_inbox", "naukri_read_message",
                     "naukri_mark_interested", "naukri_triage_inbox"):
            assert name in naukri_server.mcp._tool_manager._tools

    def test_no_reply_or_compose_tool_is_registered(self):
        """Naukri exposes no reply, compose or thread surface to a jobseeker.

        Measured 2026-08-23: GET on `/v1/inbox/users/self/reply`,
        `/v0/.../reply` and `/v1/.../mail/reply` all return 404, against a
        positive control -- `/v0/.../markInterested`, a route that does exist
        -- returning 405 Method Not Allowed on the same verb. `/conversations`
        and `/chat` were already 404. The inbox UI offers exactly Apply and
        Not interested, and opening a message fires zero API calls.

        So a reply tool here would be a tool that cannot work. This test is the
        standing record of that: if somebody adds one, they have to come and
        read this docstring and re-run the probes first.
        """
        import naukri_server

        registered = naukri_server.mcp._tool_manager._tools
        for banned in ("naukri_reply", "naukri_reply_to_recruiter",
                       "naukri_send_message", "naukri_compose_message"):
            assert banned not in registered, (
                "%s is registered, but Naukri exposes no reply surface -- "
                "re-run the 404/405 probes in this docstring before keeping it"
                % banned
            )
