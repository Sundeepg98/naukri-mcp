"""Debug API actions, over TWO DIFFERENT TRANSPORTS that must never be confused.

WHY THIS FILE WAS REWRITTEN (2026-08-26)
----------------------------------------
Every action here used to run a **browser-context** ``fetch()`` inside
``page.evaluate``. That is NOT the transport the server's own 100+ tools use.
The real tools go out over aiohttp (``api_client`` -> ``naukri_server.api``)
carrying ``API_HEADERS`` plus an ``Authorization: Bearer`` token; a page
``fetch()`` carries cookies and NO bearer, and is additionally subject to CORS
and to Akamai's edge rules that the REST path never meets.

Three consequences were measured on the live account:

* ``GET /cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/dashboard``
  answered 200 with a full body, and the SAME path with a single harmless
  ``?x=1`` answered ``{"error": "Failed to fetch"}``. Any endpoint needing
  ``?jobId=`` / ``?days=`` / ``?page=`` was therefore unreachable.
* ``POST /jobapi/v2/search/recom-jobs`` with a valid body answered
  ``{"error": "Failed to fetch"}`` while the page itself calls that exact
  endpoint successfully.
* The notification-count endpoint answered ``"Failed to fetch"`` bare, with no
  query string at all.

And the failure that made all of the above dangerous rather than merely
annoying: the JS ``catch`` block rendered a CORS/network failure as
``{"status": "ok", "fetch_result": {"error": "Failed to fetch"}}``. A transport
that never reached Naukri was reported in the same shape, and under the same
``"status": "ok"``, as a real answer from Naukri. Endpoints that exist read as
endpoints that do not, and every "this endpoint returns nothing" conclusion
drawn through this tool was unsound.

WHAT REPLACED IT
----------------
Two families, named for what they actually do:

* ``do_*_api``          -- REST transport (``api_client``, aiohttp, bearer).
                           The path a real tool takes. If this reaches an
                           endpoint, a real tool can reach it too.
* ``do_*_via_browser``  -- browser transport (``page.evaluate`` + ``fetch``).
                           Kept deliberately: it answers a genuinely different
                           question -- what the LOGGED-IN PAGE can reach, with
                           its cookies and under CORS. Some surfaces are
                           cookie-authenticated and browser-only (see
                           ``config.CCS_PAGE_API``, "requires browser cookies,
                           not JWT"), so this is a capability, not dead weight.

Both families return the SAME envelope, and the envelope's whole job is to make
a transport failure impossible to mistake for an API result. See ``_envelope``.

WHAT EACH TRANSPORT CAN AND CANNOT TELL YOU
-------------------------------------------
=========================  ==================  =====================
                           REST                browser
=========================  ==================  =====================
Authorization bearer       yes                 no (cookies only)
Subject to CORS / edge     no                  yes
Exact 2xx status           NO (see below)      yes
Response headers           no                  yes
Distinguishes CORS block   n/a                 NO (opaque by design)
=========================  ==================  =====================

The REST transport hands back a parsed body, not a response object, so a
SUCCESS carries no exact 2xx code (``http_status`` is None and ``ok`` is True).
Every NON-success does carry its status. That is the honest cost of using the
real transport rather than a look-alike, and it is the right trade: the point of
this tool is to answer "can the server reach this?", and only the real transport
can answer that.
"""

import json
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from naukri_server.api import NaukriAPIError, NaukriBotCheckError
from naukri_server.browser import (
    AuthExpiredError,
    BrowserUnavailableError,
    NotLoggedInError,
)
from naukri_server.config import NAUKRI_BASE
from naukri_server.interfaces import api_client

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

TRANSPORT_REST = "rest"
TRANSPORT_BROWSER = "browser"

#: An answer came back from Naukri and it parsed.
OUTCOME_OK = "ok"
#: Naukri (or its edge) answered, with a non-success status. A REAL API result.
OUTCOME_HTTP_ERROR = "http_error"
#: Akamai Bot Manager classified the caller as automation (403/406).
OUTCOME_BOT_CHECK = "bot_check"
#: No usable credential, or the session is gone. Not a statement about the endpoint.
OUTCOME_AUTH = "auth_required"
#: The request never produced a response. NOT an API result.
OUTCOME_TRANSPORT = "transport_failure"
#: This process refused to send it (circuit breaker). NOT an API result.
OUTCOME_CLIENT_REFUSED = "client_refused"
#: The caller's own arguments did not parse. Nothing was sent.
OUTCOME_BAD_REQUEST = "bad_request"

#: Outcomes that describe the ENDPOINT. Everything else describes the plumbing,
#: and a discovery pass must not read it as evidence about the endpoint.
ENDPOINT_EVIDENCE_OUTCOMES = frozenset({OUTCOME_OK, OUTCOME_HTTP_ERROR})

_MAX_TEXT = 5000


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _absolute(path: str) -> str:
    """Naukri-absolute URL for a path that may already be absolute."""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{NAUKRI_BASE}{path}"


def _split_path_query(raw: str):
    """Split ``raw`` into (path_without_query, query_pairs, path_as_given).

    THE QUERY STRING IS PASSED THROUGH BYTE-FOR-BYTE, never re-encoded. It is
    returned as ``path_as_given`` and that is what goes on the wire.
    ``query_pairs`` is decoded for the REPORT only.

    Re-encoding was the tempting alternative (parse to a dict, hand it to
    ``api_get(params=...)``) and it is lossy in two ways that matter to a
    discovery probe: it collapses a repeated key (``?a=1&a=2``) and it
    normalises percent-encoding. A probe exists to send exactly what it was
    asked to send.
    """
    raw = raw or ""
    parts = urlsplit(raw)
    if not parts.query:
        return raw, [], raw
    bare = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    return bare, pairs, raw


def _split_body(raw: str, default):
    """Split the ``"PATH|JSON_BODY"`` argument form.

    Returns (path_as_given, body, parse_error). ``parse_error`` is a string when
    the JSON half did not parse -- in which case NOTHING is sent, because a
    caller's typo must not be reported as an answer from Naukri.
    """
    parts = (raw or "").split("|", 1)
    path = parts[0]
    if len(parts) < 2:
        return path, default, None
    text = parts[1]
    try:
        return path, json.loads(text), None
    except (ValueError, TypeError) as exc:
        return path, None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------

def _envelope(transport, outcome, method, path_as_given, query, *,
              reached_origin, http_status=None, body=None, error=None,
              request_body=None, detail=None, extra=None) -> dict:
    """Build the one result shape both transports answer in.

    ``reached_origin`` is the field this whole rewrite exists for:

    * ``True``  -- an HTTP response came back from Naukri or its CDN edge, so
                   ``http_status`` (when present) is a fact ABOUT THE ENDPOINT.
    * ``False`` -- no response came back. Either this process refused to send
                   (circuit breaker, no token) or the connection never produced
                   one (connection error / timeout after retries). A False here
                   does NOT prove the origin never received the request -- a
                   timeout is indistinguishable from a slow success -- it proves
                   only that no answer was read.
    * ``None``  -- undeterminable. The browser transport's ``fetch()`` rejection
                   is the standing case: "TypeError: Failed to fetch" is opaque
                   BY SPECIFICATION and cannot separate a CORS-blocked response
                   from a request that never left.

    ``status`` stays "ok" only for a genuine answer. The old code answered
    ``"status": "ok"`` for CORS failures, which is exactly how a broken
    transport passed itself off as an empty endpoint.
    """
    result = {
        "status": "ok" if outcome == OUTCOME_OK else "error",
        "outcome": outcome,
        "transport": transport,
        "reached_origin": reached_origin,
        "http_status": http_status,
        "endpoint_evidence": outcome in ENDPOINT_EVIDENCE_OUTCOMES,
        "request": {
            "method": method,
            "path": path_as_given,
            "url": _absolute(path_as_given),
            "query": [list(pair) for pair in (query or [])],
        },
    }
    if request_body is not None:
        result["request"]["body"] = request_body
    if outcome == OUTCOME_OK:
        result["body"] = body
    else:
        result["error"] = error or {}
    if detail:
        result["detail"] = detail
    if extra:
        result.update(extra)
    return result


def _not_endpoint_evidence(transport: str) -> str:
    return (
        "This is a %s TRANSPORT failure, not an answer from Naukri. Do not "
        "record it as 'the endpoint returns nothing'." % transport
    )


# ---------------------------------------------------------------------------
# REST transport
# ---------------------------------------------------------------------------

def _classify_rest_exception(exc: BaseException) -> dict:
    """Map an exception out of ``api_client`` onto the envelope's vocabulary.

    Ordering is load-bearing: ``NaukriBotCheckError`` subclasses
    ``NaukriAPIError``, so it must be tested first or a bot-check would be
    filed as an ordinary HTTP error.
    """
    if isinstance(exc, NaukriBotCheckError):
        return {
            "outcome": OUTCOME_BOT_CHECK,
            "reached_origin": True,
            "http_status": exc.status,
            "error": {
                "kind": type(exc).__name__,
                "message": exc.message,
                "code": exc.code,
                "block_kind": exc.block_kind,
            },
            "detail": (
                "Akamai Bot Manager rejected the REST caller. The endpoint may "
                "exist and be perfectly reachable from the logged-in page -- "
                "retry the same path with the matching *_via_browser action."
            ),
        }
    if isinstance(exc, NaukriAPIError):
        # status 0 is api.py's sentinel for "no response after retries".
        if exc.status == 0:
            return {
                "outcome": OUTCOME_TRANSPORT,
                "reached_origin": False,
                "http_status": None,
                "error": {"kind": type(exc).__name__, "message": exc.message,
                          "code": exc.code},
                "detail": _not_endpoint_evidence("REST") + " No HTTP response "
                          "was received (connection error or timeout after retries).",
            }
        # api.py raises a 503 locally when its circuit breaker is open. That
        # request never left this process, so it is not a 503 FROM Naukri.
        if exc.status == 503 and "Circuit breaker open" in (exc.message or ""):
            return {
                "outcome": OUTCOME_CLIENT_REFUSED,
                "reached_origin": False,
                "http_status": None,
                "error": {"kind": type(exc).__name__, "message": exc.message,
                          "code": exc.code},
                "detail": _not_endpoint_evidence("REST") + " api.py's circuit "
                          "breaker refused to send after repeated failures; wait "
                          "for it to reset, then re-probe.",
            }
        if exc.status == 401:
            return {
                "outcome": OUTCOME_AUTH,
                "reached_origin": True,
                "http_status": 401,
                "error": {"kind": type(exc).__name__, "message": exc.message,
                          "code": exc.code, "block_kind": exc.block_kind},
                "detail": "Session rejected. Call naukri_login, then re-probe.",
            }
        return {
            "outcome": OUTCOME_HTTP_ERROR,
            "reached_origin": True,
            "http_status": exc.status,
            "error": {"kind": type(exc).__name__, "message": exc.message,
                      "code": exc.code, "block_kind": exc.block_kind},
            "detail": "Naukri answered HTTP %s. This IS evidence about the "
                      "endpoint." % exc.status,
        }
    if isinstance(exc, (NotLoggedInError, AuthExpiredError)):
        return {
            "outcome": OUTCOME_AUTH,
            "reached_origin": False,
            "http_status": None,
            "error": {"kind": type(exc).__name__, "message": str(exc)},
            "detail": _not_endpoint_evidence("REST") + " No usable credential "
                      "existed, so nothing was sent. Call naukri_login.",
        }
    if isinstance(exc, BrowserUnavailableError):
        return {
            "outcome": OUTCOME_TRANSPORT,
            "reached_origin": False,
            "http_status": None,
            "error": {"kind": type(exc).__name__, "message": str(exc)},
            "detail": _not_endpoint_evidence("REST") + " The browser needed to "
                      "mint a token was unavailable, so nothing was sent.",
        }
    # Unclassified. reached_origin stays None ON PURPOSE: guessing False here
    # would be the same lie the old code told, one layer up.
    return {
        "outcome": OUTCOME_TRANSPORT,
        "reached_origin": None,
        "http_status": None,
        "error": {"kind": type(exc).__name__, "message": str(exc)},
        "detail": _not_endpoint_evidence("REST") + " The failure was not one "
                  "api.py classifies, so whether the request reached Naukri is "
                  "UNKNOWN.",
    }


async def _rest_call(method: str, path_as_given: str, query, *, body=None,
                     extra_headers=None, request_body=None) -> dict:
    """Run one REST call and wrap it in the envelope. Never raises."""
    verb = {
        "GET": lambda: api_client.get(path_as_given, extra_headers=extra_headers),
        "POST": lambda: api_client.post(path_as_given, body=body,
                                        extra_headers=extra_headers),
        "PUT": lambda: api_client.put(path_as_given, body=body,
                                      extra_headers=extra_headers),
        "DELETE": lambda: api_client.delete(path_as_given, body=body,
                                            extra_headers=extra_headers),
    }[method]
    try:
        data = await verb()
    except BaseException as exc:  # noqa: BLE001 - a probe reports, never raises
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        parts = _classify_rest_exception(exc)
        return _envelope(TRANSPORT_REST, parts["outcome"], method, path_as_given,
                         query, reached_origin=parts["reached_origin"],
                         http_status=parts["http_status"], error=parts["error"],
                         request_body=request_body, detail=parts.get("detail"))
    return _envelope(
        TRANSPORT_REST, OUTCOME_OK, method, path_as_given, query,
        reached_origin=True, http_status=None, body=data,
        request_body=request_body,
        detail="REST successes carry no exact 2xx code -- api_client returns the "
               "parsed body, not the response. Non-success statuses ARE reported.",
        extra={"ok": True},
    )


def _bad_request(transport, method, path, query, message, request_text) -> dict:
    return _envelope(
        transport, OUTCOME_BAD_REQUEST, method, path, query,
        reached_origin=False, http_status=None,
        error={"kind": "ArgumentError", "message": message,
               "given": request_text},
        detail="Nothing was sent. The url argument did not parse, so this says "
               "nothing at all about the endpoint.",
    )


async def do_fetch_api(url: str) -> dict:
    """REST transport GET. Query strings are supported and passed through verbatim.

    ``url`` is a Naukri API path, with or without a query string:
    ``/jobapi/v1/job/123`` or ``/cloudgateway-apply/v0/x?jobId=5&applyType=normal``.
    """
    path, query, given = _split_path_query(url)
    return await _rest_call("GET", given, query)


async def do_post_api(url: str) -> dict:
    """REST transport POST. ``url`` is ``"PATH|JSON_BODY"``; PATH may carry a query."""
    raw_path, body, err = _split_body(url, {})
    path, query, given = _split_path_query(raw_path)
    if err:
        return _bad_request(TRANSPORT_REST, "POST", given, query,
                            "POST body is not valid JSON: %s" % err, url)
    return await _rest_call("POST", given, query, body=body, request_body=body)


async def do_put_api(url: str) -> dict:
    """REST transport PUT. ``url`` is ``"PATH|JSON_BODY"``. WRITES upstream."""
    raw_path, body, err = _split_body(url, {})
    path, query, given = _split_path_query(raw_path)
    if err:
        return _bad_request(TRANSPORT_REST, "PUT", given, query,
                            "PUT body is not valid JSON: %s" % err, url)
    return await _rest_call("PUT", given, query, body=body, request_body=body)


async def do_delete_api(url: str) -> dict:
    """REST transport DELETE. ``url`` is ``"PATH"`` or ``"PATH|JSON_BODY"``. WRITES upstream."""
    raw_path, body, err = _split_body(url, None)
    path, query, given = _split_path_query(raw_path)
    if err:
        return _bad_request(TRANSPORT_REST, "DELETE", given, query,
                            "DELETE body is not valid JSON: %s" % err, url)
    return await _rest_call("DELETE", given, query, body=body, request_body=body)


#: Widget surfaces answer to a different app id than the main desktop app.
WIDGET_HEADERS = {"appid": "109", "systemid": "109"}


async def do_fetch_widget(url: str) -> dict:
    """REST transport GET with widget headers (appid 109, systemid 109)."""
    path, query, given = _split_path_query(url)
    result = await _rest_call("GET", given, query, extra_headers=WIDGET_HEADERS)
    result["request"]["extra_headers"] = dict(WIDGET_HEADERS)
    return result


SETTINGS_PATH = "/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self"


async def do_settings_api(url: str = "") -> dict:
    """REST transport GET of the account settings blob. ``url`` is ignored.

    Reshapes the payload into the named sections a settings audit wants. A
    failure is reported through the SAME envelope as every other action -- it
    used to be folded into the payload as ``settings_api.error`` underneath
    ``"status": "ok"``, which is the same silent-failure shape this file was
    rewritten to remove.
    """
    path, query, given = _split_path_query(SETTINGS_PATH + "?expand_level=4")
    result = await _rest_call("GET", given, query)
    if result["outcome"] != OUTCOME_OK:
        return result
    data = result.get("body") or {}
    user = data.get("user", {}) if isinstance(data, dict) else {}
    result["settings_api"] = {
        "user": user,
        "communication_settings": user.get("communicationSettings"),
        "visibility": user.get("resdexVisibility"),
        "additional_details": data.get("additionalDetails") if isinstance(data, dict) else None,
        "extended_profile": data.get("extendedProfile") if isinstance(data, dict) else None,
        "profile_additional": data.get("profileAdditional") if isinstance(data, dict) else None,
        "user_properties": user.get("userProperties"),
        "raw_keys": list(data.keys()) if isinstance(data, dict) else [],
    }
    return result


# ---------------------------------------------------------------------------
# Browser transport
# ---------------------------------------------------------------------------

#: One JS body for every verb. Four near-identical copies used to drift apart
#: (the GET truncated non-JSON at 2000 chars while the others used 5000).
#: ``transport_error`` is a DISTINCT key from anything an API body can hold, so
#: a rejected fetch can never be mistaken for a payload with an "error" field.
_BROWSER_FETCH_JS = """async ([apiUrl, method, headers, bodyText, maxText]) => {
    try {
        const opts = {method: method, credentials: 'include', headers: headers};
        if (bodyText !== null && bodyText !== undefined) { opts.body = bodyText; }
        const resp = await fetch(apiUrl, opts);
        const text = await resp.text();
        let body = null;
        let parsed = true;
        try { body = JSON.parse(text); } catch (e) { parsed = false; body = text.slice(0, maxText); }
        return {
            status: resp.status,
            url: resp.url,
            ok: resp.ok,
            type: resp.type,
            redirected: resp.redirected,
            json: parsed,
            headers: Object.fromEntries(resp.headers.entries()),
            body: body,
        };
    } catch (e) {
        const name = (e && e.name) ? e.name : 'Error';
        const msg = (e && e.message) ? e.message : String(e);
        return {transport_error: name + ': ' + msg};
    }
}"""

BROWSER_HEADERS = {
    "Accept": "application/json",
    "appid": "121",
    "clientid": "d3skt0p",
    "content-type": "application/json",
    "systemid": "Naukri",
    "x-requested-with": "XMLHttpRequest",
}

BROWSER_WIDGET_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "appid": "109",
    "systemid": "109",
    "clientid": "d3skt0p",
    "x-requested-with": "XMLHttpRequest",
}


async def _browser_call(page, method: str, path_as_given: str, query, *,
                        headers=None, body_text=None, request_body=None) -> dict:
    """Run one browser-context fetch and wrap it in the envelope. Never raises."""
    headers = headers or BROWSER_HEADERS
    try:
        raw = await page.evaluate(
            _BROWSER_FETCH_JS,
            [_absolute(path_as_given), method, dict(headers), body_text, _MAX_TEXT],
        )
    except BaseException as exc:  # noqa: BLE001 - a probe reports, never raises
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return _envelope(
            TRANSPORT_BROWSER, OUTCOME_TRANSPORT, method, path_as_given, query,
            reached_origin=False, http_status=None,
            error={"kind": type(exc).__name__, "message": str(exc),
                   "stage": "browser_driver"},
            request_body=request_body,
            detail=_not_endpoint_evidence("browser") + " The page never ran the "
                   "fetch (tab closed, navigation, or evaluate timeout).",
        )

    if not isinstance(raw, dict):
        return _envelope(
            TRANSPORT_BROWSER, OUTCOME_TRANSPORT, method, path_as_given, query,
            reached_origin=None, http_status=None,
            error={"kind": "MalformedEvaluateResult", "message": repr(raw)[:500],
                   "stage": "browser_driver"},
            request_body=request_body,
            detail=_not_endpoint_evidence("browser"),
        )

    if "transport_error" in raw:
        return _envelope(
            TRANSPORT_BROWSER, OUTCOME_TRANSPORT, method, path_as_given, query,
            reached_origin=None, http_status=None,
            error={"kind": "FetchRejected", "message": raw["transport_error"],
                   "stage": "page_fetch"},
            request_body=request_body,
            detail=_not_endpoint_evidence("browser") + " A page fetch() rejection "
                   "is opaque by specification: CORS block, edge rule, and network "
                   "failure are indistinguishable, and a bearer-authenticated "
                   "endpoint will land here. Re-probe the same path with the REST "
                   "sibling action before concluding anything about the endpoint.",
            extra={"response_type": raw.get("type")},
        )

    http_status = raw.get("status")
    ok = bool(raw.get("ok"))
    # status 0 with no exception means an opaque response -- readable neither
    # as success nor as a status. Treat it as transport, never as an answer.
    if not http_status:
        return _envelope(
            TRANSPORT_BROWSER, OUTCOME_TRANSPORT, method, path_as_given, query,
            reached_origin=None, http_status=None,
            error={"kind": "OpaqueResponse",
                   "message": "fetch returned status 0 (type=%s)" % raw.get("type"),
                   "stage": "page_fetch"},
            request_body=request_body,
            detail=_not_endpoint_evidence("browser") + " The response was opaque, "
                   "so its status could not be read.",
            extra={"response_type": raw.get("type")},
        )

    extra = {
        "ok": ok,
        "response_headers": raw.get("headers") or {},
        "response_url": raw.get("url"),
        "response_type": raw.get("type"),
        "redirected": raw.get("redirected"),
        "json": raw.get("json"),
    }
    if ok:
        return _envelope(TRANSPORT_BROWSER, OUTCOME_OK, method, path_as_given,
                         query, reached_origin=True, http_status=http_status,
                         body=raw.get("body"), request_body=request_body,
                         extra=extra)
    return _envelope(
        TRANSPORT_BROWSER, OUTCOME_HTTP_ERROR, method, path_as_given, query,
        reached_origin=True, http_status=http_status,
        error={"kind": "HttpError", "message": "HTTP %s" % http_status,
               "body": raw.get("body")},
        request_body=request_body,
        detail="The page received HTTP %s. This IS evidence about the endpoint "
               "as the browser sees it." % http_status,
        extra=extra,
    )


async def do_fetch_api_via_browser(page, url: str) -> dict:
    """BROWSER transport GET -- cookies, no bearer, subject to CORS."""
    path, query, given = _split_path_query(url)
    return await _browser_call(page, "GET", given, query)


async def do_post_api_via_browser(page, url: str) -> dict:
    """BROWSER transport POST. ``url`` is ``"PATH|JSON_BODY"``."""
    raw_path, body, err = _split_body(url, {})
    path, query, given = _split_path_query(raw_path)
    if err:
        return _bad_request(TRANSPORT_BROWSER, "POST", given, query,
                            "POST body is not valid JSON: %s" % err, url)
    return await _browser_call(page, "POST", given, query,
                               body_text=json.dumps(body), request_body=body)


async def do_put_api_via_browser(page, url: str) -> dict:
    """BROWSER transport PUT. ``url`` is ``"PATH|JSON_BODY"``. WRITES upstream."""
    raw_path, body, err = _split_body(url, {})
    path, query, given = _split_path_query(raw_path)
    if err:
        return _bad_request(TRANSPORT_BROWSER, "PUT", given, query,
                            "PUT body is not valid JSON: %s" % err, url)
    return await _browser_call(page, "PUT", given, query,
                               body_text=json.dumps(body), request_body=body)


async def do_delete_api_via_browser(page, url: str) -> dict:
    """BROWSER transport DELETE. ``url`` is ``"PATH"`` or ``"PATH|JSON_BODY"``. WRITES upstream."""
    raw_path, body, err = _split_body(url, None)
    path, query, given = _split_path_query(raw_path)
    if err:
        return _bad_request(TRANSPORT_BROWSER, "DELETE", given, query,
                            "DELETE body is not valid JSON: %s" % err, url)
    body_text = None if body is None else json.dumps(body)
    return await _browser_call(page, "DELETE", given, query,
                               body_text=body_text, request_body=body)


async def do_fetch_widget_via_browser(page, url: str) -> dict:
    """BROWSER transport GET with widget headers (appid 109, systemid 109)."""
    path, query, given = _split_path_query(url)
    result = await _browser_call(page, "GET", given, query,
                                 headers=BROWSER_WIDGET_HEADERS)
    result["request"]["extra_headers"] = dict(BROWSER_WIDGET_HEADERS)
    return result
