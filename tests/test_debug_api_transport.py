"""`naukri_debug`'s api_* actions must use the REAL transport, and must never
render a transport failure as an answer from Naukri.

Every test is PURE: no network, no browser, no file I/O.

WHAT WAS WRONG (measured on the live account, 2026-08-25/26)
-----------------------------------------------------------
Every api_* action ran a browser-context ``fetch()`` inside ``page.evaluate``.
That is not the transport any real tool uses -- the real path is aiohttp with
``API_HEADERS`` and an ``Authorization: Bearer`` token. The page fetch carries
cookies and no bearer, and meets CORS and Akamai edge rules the REST path never
meets. Three measurements:

* ``.../v1/users/self/dashboard`` -> 200 with a full body; the SAME path with
  ``?x=1`` -> ``{"error": "Failed to fetch"}``. One harmless parameter.
* ``POST /jobapi/v2/search/recom-jobs`` with a valid body -> "Failed to fetch",
  while the page itself calls that endpoint successfully.
* the notification-count endpoint -> "Failed to fetch" bare, no query at all.

And the defect that made those dangerous rather than merely annoying: the JS
``catch`` returned ``{"status": "ok", "fetch_result": {"error": ...}}``. A
request that never reached Naukri was reported under ``"status": "ok"``, in the
same shape as a real answer. Endpoints that exist read as endpoints that do not.

WHAT THESE TESTS PIN
--------------------
1. a GET WITH a query string succeeds, and the query reaches the transport
   byte-for-byte;
2. a POST succeeds;
3. a transport failure is reported AS a transport failure -- never as an empty
   or absent result;
4. a real 404 is reported as a 404. This is the negative control: a classifier
   that called everything a transport error would be just as useless as the one
   that called everything an answer.
"""

import pytest
from unittest.mock import patch

from naukri_server.api import NaukriAPIError, NaukriBotCheckError
from naukri_server.browser import NotLoggedInError
from naukri_server.tools.debug import api_actions
from naukri_server.tools.debug.api_actions import (
    OUTCOME_AUTH,
    OUTCOME_BAD_REQUEST,
    OUTCOME_BOT_CHECK,
    OUTCOME_CLIENT_REFUSED,
    OUTCOME_HTTP_ERROR,
    OUTCOME_OK,
    OUTCOME_TRANSPORT,
    TRANSPORT_BROWSER,
    TRANSPORT_REST,
    do_delete_api,
    do_fetch_api,
    do_fetch_api_via_browser,
    do_fetch_widget,
    do_post_api,
    do_post_api_via_browser,
    do_settings_api,
)

DASHBOARD = "/cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/dashboard"
RECOM_JOBS = "/jobapi/v2/search/recom-jobs"
JOB_V1 = "/jobapi/v1/job/210826815108"


# ---------------------------------------------------------------------------
# Doubles. They RECORD; they never assert -- `_rest_call` catches broad
# exceptions on purpose, so an assertion raised inside a double would be
# swallowed into an envelope instead of failing the test.
# ---------------------------------------------------------------------------

class _RecordingApiClient:
    """Stands in for `interfaces.api_client`. Returns a payload or raises."""

    def __init__(self, result=None, raises=None):
        self.result = result if result is not None else {"ok": True}
        self.raises = raises
        self.calls = []

    async def _run(self, method, path, params, extra_headers, body):
        self.calls.append({
            "method": method, "path": path, "params": params,
            "extra_headers": extra_headers, "body": body,
        })
        if self.raises is not None:
            raise self.raises
        return self.result

    async def get(self, path, params=None, extra_headers=None):
        return await self._run("GET", path, params, extra_headers, None)

    async def post(self, path, body=None, extra_headers=None):
        return await self._run("POST", path, None, extra_headers, body)

    async def put(self, path, body=None, extra_headers=None):
        return await self._run("PUT", path, None, extra_headers, body)

    async def delete(self, path, body=None, extra_headers=None):
        return await self._run("DELETE", path, None, extra_headers, body)


class _NaukriLikePage:
    """A page double that reproduces the MEASURED live browser-fetch behaviour.

    A cross-origin-ish GET with a query string, and any POST, are rejected the
    way the live account rejected them: the promise rejects and the JS `catch`
    reports "TypeError: Failed to fetch". Everything else answers 200.
    """

    def __init__(self):
        self.evaluated = []

    async def evaluate(self, js, arg):
        url, method, headers, body_text, _max_text = arg
        self.evaluated.append({"url": url, "method": method,
                               "headers": headers, "body": body_text})
        if method != "GET" or "?" in url:
            return {"transport_error": "TypeError: Failed to fetch"}
        return {
            "status": 200, "url": url, "ok": True, "type": "basic",
            "redirected": False, "json": True,
            "headers": {"content-type": "application/json"},
            "body": {"dashboard": {"profileViews": 12}},
        }


class _ExplodingPagePool:
    """Any use of the browser at all is a failure for the REST actions."""

    def acquire(self):
        raise AssertionError("a REST action must not check out a browser tab")


# ---------------------------------------------------------------------------
# PROOF 1 -- a GET WITH a query string succeeds
# ---------------------------------------------------------------------------

class TestAGetWithAQueryStringSucceeds:

    @pytest.mark.asyncio
    async def test_the_dashboard_with_a_query_parameter_now_answers(self):
        """The exact differential that exposed the bug: same path, plus "?x=1"."""
        client = _RecordingApiClient(result={"dashboard": {"profileViews": 12}})
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api(DASHBOARD + "?x=1")

        assert result["outcome"] == OUTCOME_OK
        assert result["status"] == "ok"
        assert result["transport"] == TRANSPORT_REST
        assert result["reached_origin"] is True
        assert result["endpoint_evidence"] is True
        assert result["body"] == {"dashboard": {"profileViews": 12}}

    @pytest.mark.asyncio
    async def test_the_query_string_reaches_the_transport_byte_for_byte(self):
        """The parameters must be ON THE WIRE, not merely echoed in the report."""
        client = _RecordingApiClient()
        target = "/cloudgateway-apply/v0/x?jobId=210826815108&applyType=normal"
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api(target)

        assert len(client.calls) == 1
        assert client.calls[0]["path"] == target, (
            "the query string was dropped or rewritten before the request: %r"
            % client.calls[0]["path"]
        )
        assert result["request"]["query"] == [
            ["jobId", "210826815108"], ["applyType", "normal"],
        ]

    @pytest.mark.asyncio
    async def test_a_repeated_key_survives(self):
        """`params=dict` would have collapsed this. Passthrough does not."""
        client = _RecordingApiClient()
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api("/x?a=1&a=2")

        assert client.calls[0]["path"] == "/x?a=1&a=2"
        assert client.calls[0]["params"] is None
        assert result["request"]["query"] == [["a", "1"], ["a", "2"]]

    @pytest.mark.asyncio
    async def test_a_path_with_no_query_is_unchanged(self):
        client = _RecordingApiClient()
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api(JOB_V1)

        assert client.calls[0]["path"] == JOB_V1
        assert result["request"]["query"] == []

    @pytest.mark.asyncio
    async def test_the_old_transport_still_fails_on_the_same_query(self):
        """The differential, side by side, in one test.

        The browser transport was KEPT, so the original failure is still
        reproducible through it -- and is now NAMED instead of being dressed up
        as an answer.
        """
        page = _NaukriLikePage()
        via_browser = await do_fetch_api_via_browser(page, DASHBOARD + "?x=1")

        assert via_browser["outcome"] == OUTCOME_TRANSPORT
        assert via_browser["endpoint_evidence"] is False

        client = _RecordingApiClient(result={"dashboard": {}})
        with patch.object(api_actions, "api_client", client):
            over_rest = await do_fetch_api(DASHBOARD + "?x=1")

        assert over_rest["outcome"] == OUTCOME_OK, (
            "the REST transport must reach what the browser transport cannot"
        )


# ---------------------------------------------------------------------------
# PROOF 2 -- a POST succeeds
# ---------------------------------------------------------------------------

class TestAPostSucceeds:

    @pytest.mark.asyncio
    async def test_recom_jobs_post_answers_and_forwards_the_parsed_body(self):
        payload = {"jobDetails": [{"jobId": "1"}], "noOfResults": 63}
        client = _RecordingApiClient(result=payload)
        with patch.object(api_actions, "api_client", client):
            result = await do_post_api(RECOM_JOBS + '|{"sid": "abc", "size": 20}')

        assert result["outcome"] == OUTCOME_OK
        assert result["body"] == payload
        assert result["transport"] == TRANSPORT_REST
        assert client.calls[0]["method"] == "POST"
        assert client.calls[0]["path"] == RECOM_JOBS
        assert client.calls[0]["body"] == {"sid": "abc", "size": 20}, (
            "the JSON half of PATH|BODY must be parsed and forwarded as a body"
        )

    @pytest.mark.asyncio
    async def test_a_post_with_no_body_half_sends_an_empty_object(self):
        client = _RecordingApiClient()
        with patch.object(api_actions, "api_client", client):
            await do_post_api(RECOM_JOBS)
        assert client.calls[0]["body"] == {}

    @pytest.mark.asyncio
    async def test_the_old_transport_still_fails_the_same_post(self):
        page = _NaukriLikePage()
        result = await do_post_api_via_browser(page, RECOM_JOBS + '|{"sid": "abc"}')
        assert result["outcome"] == OUTCOME_TRANSPORT
        assert result["endpoint_evidence"] is False

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_sends_nothing_at_all(self):
        """A caller's typo is not evidence about the endpoint."""
        client = _RecordingApiClient()
        with patch.object(api_actions, "api_client", client):
            result = await do_post_api(RECOM_JOBS + "|{not json}")

        assert result["outcome"] == OUTCOME_BAD_REQUEST
        assert result["status"] == "error"
        assert result["endpoint_evidence"] is False
        assert result["reached_origin"] is False
        assert client.calls == [], "nothing may be sent when the body did not parse"


# ---------------------------------------------------------------------------
# PROOF 3 -- a transport failure is reported AS a transport failure
# ---------------------------------------------------------------------------

class TestATransportFailureIsNamedAsOne:

    @pytest.mark.asyncio
    async def test_a_connection_failure_is_not_an_empty_endpoint(self):
        client = _RecordingApiClient(raises=NaukriAPIError(
            0, "Connection failed after 3 retries: ClientConnectorError: nope"))
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api(JOB_V1)

        assert result["outcome"] == OUTCOME_TRANSPORT
        assert result["status"] == "error"
        assert result["http_status"] is None
        assert result["reached_origin"] is False
        assert result["endpoint_evidence"] is False
        assert "body" not in result, "a failed transport must not present a body"

    @pytest.mark.asyncio
    async def test_a_browser_fetch_rejection_is_a_transport_failure(self):
        page = _NaukriLikePage()
        result = await do_fetch_api_via_browser(page, DASHBOARD + "?x=1")

        assert result["outcome"] == OUTCOME_TRANSPORT
        assert result["status"] == "error"
        assert result["transport"] == TRANSPORT_BROWSER
        assert result["error"]["kind"] == "FetchRejected"
        assert "Failed to fetch" in result["error"]["message"]
        assert result["endpoint_evidence"] is False

    @pytest.mark.asyncio
    async def test_a_browser_fetch_rejection_admits_it_cannot_tell_you_more(self):
        """`reached_origin` is None, not False.

        "TypeError: Failed to fetch" is opaque by specification. Claiming False
        would be a second, quieter version of the same lie.
        """
        page = _NaukriLikePage()
        result = await do_fetch_api_via_browser(page, DASHBOARD + "?x=1")
        assert result["reached_origin"] is None

    @pytest.mark.asyncio
    async def test_the_circuit_breaker_is_not_reported_as_an_origin_503(self):
        """api.py raises a LOCAL 503 when its breaker is open. Nothing was sent."""
        client = _RecordingApiClient(raises=NaukriAPIError(
            503, "Circuit breaker open - API unavailable (resets in 42s)"))
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api(JOB_V1)

        assert result["outcome"] == OUTCOME_CLIENT_REFUSED
        assert result["http_status"] is None, (
            "a breaker-open 503 is not a status Naukri returned"
        )
        assert result["reached_origin"] is False
        assert result["endpoint_evidence"] is False

    def test_api_py_really_does_raise_that_message(self):
        """Pin the string the classifier keys on, at its source.

        A substring match is only as good as the string. This exercises the real
        breaker in `naukri_server.api` -- it rejects before any I/O, so the test
        stays pure -- and restores its state afterwards.
        """
        import asyncio

        from naukri_server import api as api_module

        breaker = api_module._api_circuit
        saved = (breaker._failures, breaker._state, breaker._last_failure)
        try:
            for _ in range(breaker._threshold):
                breaker.record_failure()
            with pytest.raises(NaukriAPIError) as caught:
                asyncio.run(api_module.api_get("/anything"))
            assert caught.value.status == 503
            assert caught.value.message.startswith("Circuit breaker open"), (
                "the classifier keys on this prefix: %r" % caught.value.message
            )
        finally:
            breaker._failures, breaker._state, breaker._last_failure = saved

    @pytest.mark.asyncio
    async def test_a_missing_credential_is_auth_not_a_dead_endpoint(self):
        client = _RecordingApiClient(raises=NotLoggedInError("Not logged in"))
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api(JOB_V1)

        assert result["outcome"] == OUTCOME_AUTH
        assert result["reached_origin"] is False
        assert result["endpoint_evidence"] is False

    @pytest.mark.asyncio
    async def test_a_bot_check_is_its_own_outcome_not_an_http_error(self):
        """NaukriBotCheckError subclasses NaukriAPIError -- order is load-bearing."""
        client = _RecordingApiClient(raises=NaukriBotCheckError(
            406, "Bot-check (HTTP 406) persisted after re-auth on /jobapi/v3/job/1"))
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api("/jobapi/v3/job/1")

        assert result["outcome"] == OUTCOME_BOT_CHECK
        assert result["http_status"] == 406
        assert result["reached_origin"] is True
        assert result["endpoint_evidence"] is False, (
            "the edge rejected the caller; that is not a verdict on the endpoint"
        )
        assert "via_browser" in result["detail"]

    @pytest.mark.asyncio
    async def test_the_page_evaluate_itself_failing_is_a_transport_failure(self):
        class _DeadPage:
            async def evaluate(self, js, arg):
                raise RuntimeError("Target page, context or browser has been closed")

        result = await do_fetch_api_via_browser(_DeadPage(), DASHBOARD)
        assert result["outcome"] == OUTCOME_TRANSPORT
        assert result["error"]["stage"] == "browser_driver"
        assert result["reached_origin"] is False


class TestTheOldShapeCouldNotHaveSaidAnyOfThis:
    """The control that shows the new checks are not vacuous.

    This is the literal value the old `do_fetch_api` returned for every CORS
    failure. It is reproduced here rather than referenced because the code that
    produced it is gone -- and the point is that it carried NO channel through
    which a caller could have learned the request never left.
    """

    OLD_RESULT = {"status": "ok", "fetch_result": {"error": "Failed to fetch"}}

    def test_the_old_result_called_a_transport_failure_ok(self):
        assert self.OLD_RESULT["status"] == "ok"

    def test_the_old_result_had_nowhere_to_put_the_truth(self):
        for field in ("outcome", "transport", "reached_origin", "http_status",
                      "endpoint_evidence"):
            assert field not in self.OLD_RESULT, (
                "if the old shape had carried %r there would have been no bug" % field
            )

    @pytest.mark.asyncio
    async def test_and_the_new_result_for_the_same_event_does_not(self):
        page = _NaukriLikePage()
        new = await do_fetch_api_via_browser(page, DASHBOARD + "?x=1")
        assert new["status"] == "error"
        for field in ("outcome", "transport", "reached_origin", "http_status",
                      "endpoint_evidence"):
            assert field in new


# ---------------------------------------------------------------------------
# PROOF 4 -- a real 404 is reported as a 404 (the negative control)
# ---------------------------------------------------------------------------

class TestARealFourOhFourIsReportedAsFourOhFour:

    @pytest.mark.asyncio
    async def test_a_404_keeps_its_status_and_counts_as_endpoint_evidence(self):
        client = _RecordingApiClient(raises=NaukriAPIError(
            404, "Not Found", code="NOT_FOUND"))
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api("/jobapi/v1/job/000000000000")

        assert result["outcome"] == OUTCOME_HTTP_ERROR
        assert result["http_status"] == 404
        assert result["reached_origin"] is True
        assert result["endpoint_evidence"] is True, (
            "Naukri answered. A classifier that filed this as a transport error "
            "would be exactly as useless as the one it replaced."
        )
        assert result["error"]["message"] == "Not Found"
        assert result["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_405_and_500_are_endpoint_evidence_too(self):
        for status in (405, 410, 422, 500):
            client = _RecordingApiClient(raises=NaukriAPIError(status, "nope"))
            with patch.object(api_actions, "api_client", client):
                result = await do_fetch_api("/x")
            assert result["outcome"] == OUTCOME_HTTP_ERROR, status
            assert result["http_status"] == status
            assert result["endpoint_evidence"] is True, status

    @pytest.mark.asyncio
    async def test_a_browser_404_is_also_a_404(self):
        class _NotFoundPage:
            async def evaluate(self, js, arg):
                return {"status": 404, "url": arg[0], "ok": False, "type": "basic",
                        "redirected": False, "json": False, "headers": {},
                        "body": "Not Found"}

        result = await do_fetch_api_via_browser(_NotFoundPage(), "/nope")
        assert result["outcome"] == OUTCOME_HTTP_ERROR
        assert result["http_status"] == 404
        assert result["reached_origin"] is True
        assert result["endpoint_evidence"] is True

    @pytest.mark.asyncio
    async def test_the_classifier_is_not_a_constant(self):
        """Four different inputs, four different outcomes. Shown, not asserted."""
        cases = [
            (None, OUTCOME_OK),
            (NaukriAPIError(404, "Not Found"), OUTCOME_HTTP_ERROR),
            (NaukriAPIError(0, "Connection failed after 3 retries: x"), OUTCOME_TRANSPORT),
            (NaukriBotCheckError(406, "bot"), OUTCOME_BOT_CHECK),
            (NotLoggedInError("no token"), OUTCOME_AUTH),
        ]
        seen = []
        for raises, expected in cases:
            client = _RecordingApiClient(raises=raises)
            with patch.object(api_actions, "api_client", client):
                result = await do_fetch_api("/x")
            assert result["outcome"] == expected, (raises, result["outcome"])
            seen.append(result["outcome"])
        assert len(set(seen)) == len(cases)


# ---------------------------------------------------------------------------
# The standing invariant + the rest of the surface
# ---------------------------------------------------------------------------

class TestTheStandingInvariant:

    @pytest.mark.asyncio
    async def test_no_result_ever_says_ok_unless_naukri_answered(self):
        raisers = [
            NaukriAPIError(0, "Connection failed after 3 retries: x"),
            NaukriAPIError(503, "Circuit breaker open - API unavailable"),
            NaukriAPIError(404, "Not Found"),
            NaukriBotCheckError(406, "bot"),
            NotLoggedInError("no token"),
            RuntimeError("something nobody classified"),
        ]
        for raises in raisers:
            client = _RecordingApiClient(raises=raises)
            with patch.object(api_actions, "api_client", client):
                result = await do_fetch_api("/x")
            assert result["status"] == "error", raises
            assert result["outcome"] != OUTCOME_OK, raises
            assert "body" not in result, raises
            assert result["detail"], "every failure must say what it means"

    @pytest.mark.asyncio
    async def test_an_unclassified_failure_admits_it_is_unclassified(self):
        client = _RecordingApiClient(raises=RuntimeError("who knows"))
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_api("/x")
        assert result["outcome"] == OUTCOME_TRANSPORT
        assert result["reached_origin"] is None, (
            "guessing False here would be the same lie one layer up"
        )
        assert "UNKNOWN" in result["detail"]


class TestTheRestOfTheApiSurface:

    @pytest.mark.asyncio
    async def test_widget_headers_go_out_on_the_rest_transport(self):
        client = _RecordingApiClient()
        with patch.object(api_actions, "api_client", client):
            result = await do_fetch_widget("/x?y=1")
        assert client.calls[0]["extra_headers"] == {"appid": "109", "systemid": "109"}
        assert result["request"]["extra_headers"] == {"appid": "109", "systemid": "109"}

    @pytest.mark.asyncio
    async def test_delete_without_a_body_half_sends_no_body(self):
        client = _RecordingApiClient()
        with patch.object(api_actions, "api_client", client):
            await do_delete_api("/x/1")
        assert client.calls[0]["body"] is None

    @pytest.mark.asyncio
    async def test_settings_reshapes_the_payload_on_success(self):
        client = _RecordingApiClient(result={
            "user": {"communicationSettings": {"email": True},
                     "resdexVisibility": "ALL",
                     "userProperties": {"a": 1}},
            "additionalDetails": {"b": 2},
        })
        with patch.object(api_actions, "api_client", client):
            result = await do_settings_api("")

        assert result["outcome"] == OUTCOME_OK
        assert result["settings_api"]["communication_settings"] == {"email": True}
        assert result["settings_api"]["visibility"] == "ALL"
        assert set(result["settings_api"]["raw_keys"]) == {"user", "additionalDetails"}

    @pytest.mark.asyncio
    async def test_settings_failure_is_an_envelope_not_a_payload_field(self):
        """It used to answer `{"status": "ok", "settings_api": {"error": ...}}`."""
        client = _RecordingApiClient(raises=NaukriAPIError(401, "expired"))
        with patch.object(api_actions, "api_client", client):
            result = await do_settings_api("")

        assert result["status"] == "error"
        assert result["outcome"] == OUTCOME_AUTH
        assert "settings_api" not in result


class TestRestActionsNeedNoBrowser:
    """A pure aiohttp call must not fail because the browser is down."""

    @pytest.mark.asyncio
    async def test_api_fetch_dispatches_without_checking_out_a_tab(self):
        from naukri_server.tools.debug import naukri_debug
        from naukri_server.browser import browser as browser_singleton

        client = _RecordingApiClient(result={"reached": True})
        with patch.object(browser_singleton, "page_pool", _ExplodingPagePool()), \
                patch.object(api_actions, "api_client", client):
            result = await naukri_debug(action="api_fetch", url=DASHBOARD + "?x=1")

        assert result["outcome"] == OUTCOME_OK
        assert result["body"] == {"reached": True}

    @pytest.mark.asyncio
    async def test_every_rest_action_is_dispatched_the_same_way(self):
        from naukri_server.tools.debug import _HANDLERS, _REST_ACTIONS
        for action in _REST_ACTIONS:
            assert action in _HANDLERS
        assert not (_REST_ACTIONS & {
            "api_fetch_via_browser", "api_post_via_browser",
            "api_put_via_browser", "api_delete_via_browser",
            "api_fetch_widget_via_browser",
        }), "a *_via_browser action needs a page and must never be REST-dispatched"


class TestTheDocstringNamesTheTransports:
    """The docstring is the only spec an AI caller sees (see
    test_debug_docstring_agrees_with_parser.py for the same argument)."""

    def test_it_says_which_transport_each_group_uses(self):
        from naukri_server.tools.debug import naukri_debug
        doc = naukri_debug.__doc__ or ""
        assert "REST transport" in doc
        assert "BROWSER transport" in doc or "browser transport" in doc

    def test_it_documents_that_query_strings_work(self):
        from naukri_server.tools.debug import naukri_debug
        doc = naukri_debug.__doc__ or ""
        assert "Query strings ARE supported" in doc

    def test_it_documents_the_outcome_vocabulary(self):
        from naukri_server.tools.debug import naukri_debug
        from naukri_server.tools.debug import api_actions as mod

        doc = naukri_debug.__doc__ or ""
        for outcome in (mod.OUTCOME_OK, mod.OUTCOME_HTTP_ERROR, mod.OUTCOME_BOT_CHECK,
                        mod.OUTCOME_AUTH, mod.OUTCOME_TRANSPORT,
                        mod.OUTCOME_CLIENT_REFUSED, mod.OUTCOME_BAD_REQUEST):
            assert outcome in doc, "undocumented outcome: %s" % outcome
