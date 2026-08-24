"""The AmbitionBox REST bridge must send gateway auth headers, and must not
report a dead bridge as an unqualified success.

MEASURED 2026-08-24, two independent instruments.

1. The gateway itself, via a direct read-only GET to the SAME url
   ``ab_get_work_culture`` builds (review distribution, company id 1):

       no headers                     -> HTTP 400, 29 bytes,
                                         body "AppId or SystemId not present"
       appid + systemid headers       -> HTTP 200, 687 bytes, valid JSON
                                         carrying reviewsCount /
                                         workMonitorLabels / workMonitorSeries
                                         -- the exact fields the helper reads.

   ``_ab_rest_get`` sent NO headers at all, and no AmbitionBox appid constant
   existed anywhere in the repo. So every one of the seven REST helpers was
   structurally incapable of a 200.

2. End to end, one read-only ``naukri_company_intel(intel_type="interviews")``
   call on a public company:

       status                 -> "success"
       ab_rest                -> ABSENT
       _enrichment_errors     -> all three helpers, each
                                 "ClientResponseError: 403, Forbidden"

   Note 403, not the 400 the header-free curl drew. 403 IS inside the existing
   cookie-refresh retry branch, so the bridge burns a browser navigation and a
   second request before failing -- but it still fails, ``ab_rest`` is still
   absent, and the tool still called itself "success". Live AmbitionBox
   probing STOPPED at that 403 under this wave's rate rule, so whether the
   headers alone clear the 403 path is NOT verified here; what is verified is
   that the headers are required and were missing.

The loudness fix follows the shape used for the other defects in this wave --
``counted`` beside ``count``, ``listed`` beside ``not_listed`` -- rather than
demoting ``status``. Demoting it would make ``interview_prep._unpack`` discard
the browser-scraped company rating and sample questions whenever a documented
"supplementary, best-effort" enrichment failed, trading one silently-wrong
answer for another. ``ab_rest_status`` cannot be misread as a measurement.
"""

import aiohttp
import pytest
from unittest.mock import AsyncMock, patch

from naukri_server.tools import ambitionbox_rest


# ---------------------------------------------------------------------------
# A fake aiohttp transport, so these tests reach ZERO live AmbitionBox traffic.
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                None, (), status=self.status, message="Forbidden")

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class _FakeSession:
    """Captures every outgoing request instead of sending it."""

    captured = []

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, params=None, headers=None, timeout=None):
        _FakeSession.captured.append({
            "url": url,
            "params": params,
            "headers": headers,
            "cookies": self.init_kwargs.get("cookies"),
        })
        return _FakeResponse(*_FakeSession.responses.pop(0))

    responses = []


def _missing_auth_headers(request: dict) -> bool:
    """The instrument: does this captured request lack gateway auth?

    Written as a plain function so it can be shown BOTH detecting and staying
    silent -- a check that cannot fail certifies nothing.
    """
    headers = request.get("headers") or {}
    lowered = {str(k).lower() for k in headers}
    return not ({"appid", "systemid"} <= lowered)


async def _run_get(responses, url="https://ab.example/servicegateway-ambitionbox/x"):
    _FakeSession.captured = []
    _FakeSession.responses = list(responses)
    with patch.object(ambitionbox_rest, "_get_ab_cookies",
                      new=AsyncMock(return_value={})), \
         patch.object(ambitionbox_rest.aiohttp, "ClientSession", _FakeSession):
        return await ambitionbox_rest._ab_rest_get(url)


class TestTheInstrument:

    def test_CONTROL_it_flags_a_header_free_request(self):
        assert _missing_auth_headers({"headers": None}) is True
        assert _missing_auth_headers({"headers": {}}) is True

    def test_CONTROL_it_stays_silent_on_an_authenticated_request(self):
        assert _missing_auth_headers(
            {"headers": {"appid": "613", "systemid": "ambitionbox"}}) is False

    def test_CONTROL_it_flags_a_half_authenticated_request(self):
        assert _missing_auth_headers({"headers": {"appid": "613"}}) is True


class TestTheRequestCarriesGatewayAuth:

    @pytest.mark.asyncio
    async def test_appid_and_systemid_are_sent(self):
        await _run_get([(200, {"data": {}})])
        assert len(_FakeSession.captured) == 1
        assert not _missing_auth_headers(_FakeSession.captured[0])

    @pytest.mark.asyncio
    async def test_the_header_values_are_non_empty(self):
        await _run_get([(200, {"data": {}})])
        headers = _FakeSession.captured[0]["headers"]
        assert headers["appid"].strip()
        assert headers["systemid"].strip()

    def test_the_header_constant_is_exported_for_reuse(self):
        headers = ambitionbox_rest.AB_REST_HEADERS
        assert set(k.lower() for k in headers) >= {"appid", "systemid"}

    @pytest.mark.asyncio
    async def test_cookies_are_still_sent_alongside(self):
        """The headers are additive -- the cookie path must survive."""
        _FakeSession.captured = []
        _FakeSession.responses = [(200, {"data": {}})]
        with patch.object(ambitionbox_rest, "_get_ab_cookies",
                          new=AsyncMock(return_value={"ab": "1"})), \
             patch.object(ambitionbox_rest.aiohttp, "ClientSession", _FakeSession):
            await ambitionbox_rest._ab_rest_get("https://ab.example/x")
        assert _FakeSession.captured[0]["cookies"] == {"ab": "1"}


class TestAGatewayRejectionIsNotACookieProblem:

    @pytest.mark.asyncio
    async def test_a_400_does_not_burn_a_cookie_refresh(self):
        """"AppId or SystemId not present" cannot be fixed by new cookies, and
        refreshing them costs a real browser navigation."""
        _FakeSession.captured = []
        _FakeSession.responses = [(400, None, "AppId or SystemId not present")]
        cookie_fetch = AsyncMock(return_value={})
        with patch.object(ambitionbox_rest, "_get_ab_cookies", new=cookie_fetch), \
             patch.object(ambitionbox_rest.aiohttp, "ClientSession", _FakeSession):
            with pytest.raises(ambitionbox_rest.AbRestGatewayError):
                await ambitionbox_rest._ab_rest_get("https://ab.example/x")
        assert cookie_fetch.await_count == 1
        assert len(_FakeSession.captured) == 1

    @pytest.mark.asyncio
    async def test_the_400_error_names_the_gateway_message(self):
        _FakeSession.captured = []
        _FakeSession.responses = [(400, None, "AppId or SystemId not present")]
        with patch.object(ambitionbox_rest, "_get_ab_cookies",
                          new=AsyncMock(return_value={})), \
             patch.object(ambitionbox_rest.aiohttp, "ClientSession", _FakeSession):
            with pytest.raises(ambitionbox_rest.AbRestGatewayError) as exc:
                await ambitionbox_rest._ab_rest_get("https://ab.example/x")
        assert "400" in str(exc.value)
        assert "AppId or SystemId not present" in str(exc.value)

    @pytest.mark.asyncio
    async def test_CONTROL_a_403_still_retries_with_fresh_cookies(self):
        """403 IS a cookie-shaped failure -- that branch must survive."""
        _FakeSession.captured = []
        _FakeSession.responses = [(403, None, ""), (200, {"data": {"ok": 1}})]
        cookie_fetch = AsyncMock(return_value={})
        with patch.object(ambitionbox_rest, "_get_ab_cookies", new=cookie_fetch), \
             patch.object(ambitionbox_rest.aiohttp, "ClientSession", _FakeSession):
            data = await ambitionbox_rest._ab_rest_get("https://ab.example/x")
        assert data == {"data": {"ok": 1}}
        assert cookie_fetch.await_count == 2


class TestADeadBridgeSaysSo:
    """``naukri_company_intel`` reported ``status: "success"`` with ``ab_rest``
    simply absent and the reason parked under an underscore-prefixed key."""

    async def _enrich(self, culture, benefits, competitors):
        from naukri_server.tools.ambitionbox import _enrich_with_rest
        with patch("naukri_server.tools.ambitionbox.ab_get_work_culture",
                   new=AsyncMock(**culture)), \
             patch("naukri_server.tools.ambitionbox.ab_get_benefits",
                   new=AsyncMock(**benefits)), \
             patch("naukri_server.tools.ambitionbox.ab_get_competitors",
                   new=AsyncMock(**competitors)):
            return await _enrich_with_rest(
                {"status": "success", "company": "X", "_ab_company_id": "42"})

    OK = {"return_value": {"status": "success", "count": 1}}
    DEAD = {"side_effect": RuntimeError("403, message='Forbidden'")}

    @pytest.mark.asyncio
    async def test_total_failure_is_reported_as_failed(self):
        enriched = await self._enrich(self.DEAD, self.DEAD, self.DEAD)
        assert enriched["ab_rest_status"] == "failed"
        assert "ab_rest" not in enriched
        assert len(enriched["ab_rest_errors"]) == 3

    @pytest.mark.asyncio
    async def test_the_reason_is_readable_without_an_underscore_key(self):
        enriched = await self._enrich(self.DEAD, self.DEAD, self.DEAD)
        assert any("Forbidden" in e for e in enriched["ab_rest_errors"])

    @pytest.mark.asyncio
    async def test_partial_failure_is_reported_as_degraded(self):
        enriched = await self._enrich(self.OK, self.DEAD, self.OK)
        assert enriched["ab_rest_status"] == "degraded"
        assert "work_culture" in enriched["ab_rest"]
        assert "benefits" not in enriched["ab_rest"]

    @pytest.mark.asyncio
    async def test_CONTROL_a_healthy_bridge_reports_ok(self):
        enriched = await self._enrich(self.OK, self.OK, self.OK)
        assert enriched["ab_rest_status"] == "ok"
        assert "ab_rest_errors" not in enriched

    @pytest.mark.asyncio
    async def test_no_company_id_is_skipped_not_failed(self):
        """Never having tried is a different claim from having failed."""
        from naukri_server.tools.ambitionbox import _enrich_with_rest
        enriched = await _enrich_with_rest({"status": "success", "company": "X"})
        assert enriched.get("ab_rest_status") in (None, "skipped")
        assert "ab_rest_errors" not in enriched

    @pytest.mark.asyncio
    async def test_a_dead_bridge_is_logged_at_warning_not_debug(self, caplog):
        """It was `logger.debug`, so seven dead helpers left no log line at
        the level anybody reads."""
        import logging
        with caplog.at_level(logging.WARNING):
            await self._enrich(self.DEAD, self.DEAD, self.DEAD)
        assert any("ab_rest" in r.getMessage().lower()
                   or "ambitionbox" in r.getMessage().lower()
                   for r in caplog.records)


#: The two claims the pre-fix docstrings actually made. Deliberately the
#: PHRASES and not the bare word "silent": a substring check on "silent" also
#: fires on prose that DESCRIBES the defect being cured, which would make the
#: instrument unusable in exactly the file it guards.
BANNED_SILENCE_CLAIMS = ("failures are silently swallowed", "failures are silent")


def _promises_silence(doc: str) -> bool:
    lowered = (doc or "").lower()
    return any(claim in lowered for claim in BANNED_SILENCE_CLAIMS)


class TestTheDocstringsNoLongerPromiseSilence:
    """Two docstrings advertised the swallowing as a feature."""

    def test_CONTROL_the_scanner_catches_the_original_wording(self):
        assert _promises_silence(
            "Failures are silently swallowed -- enrichment is best-effort.")
        assert _promises_silence("REST enrichment is best-effort, failures are silent.")

    def test_CONTROL_the_scanner_allows_prose_about_the_defect(self):
        assert not _promises_silence(
            "one silently-wrong answer traded for another")

    def test_enrich_docstring_no_longer_promises_silence(self):
        from naukri_server.tools.ambitionbox import _enrich_with_rest
        assert not _promises_silence(_enrich_with_rest.__doc__)

    def test_company_intel_docstring_no_longer_promises_silence(self):
        from naukri_server.tools.ambitionbox import naukri_company_intel
        doc = getattr(naukri_company_intel, "__doc__", "") or ""
        if not doc:
            doc = getattr(getattr(naukri_company_intel, "fn", None), "__doc__", "") or ""
        assert doc, "could not reach the tool's docstring; the check would be vacuous"
        assert not _promises_silence(doc)
