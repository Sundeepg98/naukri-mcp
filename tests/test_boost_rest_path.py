"""The boost REST path goes through the fullprofiles transport chokepoint.

Every test is PURE: no network, no browser, no file I/O. `api_client` is a
recorder and `fullprofiles_write` is replaced wholesale, so nothing here can
reach naukri.com even if a route regressed.

BACKGROUND. `_boost_visibility`'s "Strategy 1: REST API" used to loop over
("v0", "v2"), build the endpoint off `FULLPROFILES_API`, and send no
`X-HTTP-Method-Override` header. Neither version is a fullprofiles WRITE route
for the profile editor, so that loop could never have succeeded: it always
fell through to the browser path. The record agrees -- `event_log` spanned
2026-08-20 onward with 7624 rows and held ZERO `ProfileBoosted` events.

WHAT EACH CONTROL PINS:
  1. the boost calls `fullprofiles_write`, so the route (v1), the
     `X-HTTP-Method-Override: PUT` header and the top-level `profileId` are
     INHERITED from the chokepoint instead of being re-typed at this call site;
  2. the payload is `{"profile": {"resumeHeadline": <the headline just read>}}`
     -- a boost is a no-op RE-SAVE, so the headline goes back unchanged;
  3. no outgoing call carries a v0 or v2 fullprofiles path, and
     `profile_update` no longer holds a reference to `FULLPROFILES_API` at all
     (that reference is how the bug gets re-introduced, so it is a tripwire:
     a legitimate future need for the v0 read constant here must retire this
     assertion deliberately, not by accident);
  4. a REST failure still falls back to the browser path -- that fallback is
     the only route that has ever had a chance of working, so the fix must not
     remove it;
  5. `ProfileBoosted` is emitted on the REST success path carrying a method
     string honest about the new route, and the return shape is unchanged;
  6. an empty or missing headline sends NO write.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_server.config import FULLPROFILES_WRITE_API
from naukri_server.events import event_bus
from naukri_server.tools import profile_update
from naukri_server.tools.profile_update import _boost_visibility


# A stand-in headline. Never the real one -- these tests carry no personal data.
FAKE_HEADLINE = "Fake Headline For Test Only"

EXPECTED_METHOD = "rest_fullprofiles_v1"

# The five keys `_boost_visibility`'s REST branch has always returned.
REST_RETURN_KEYS = {"status", "action", "method", "headline_length", "message"}


def _profile_payload(headline=FAKE_HEADLINE):
    """Shape of the PROFILE_API read the boost path consumes."""
    return {"profile": [{"resumeHeadline": headline}]}


class RecordingApi:
    """Stands in for `api_client`, recording every outgoing call.

    Records (verb, path, payload) so a test can assert on the PATHS actually
    reached -- which is how control 3 catches a v0/v2 fullprofiles regression.
    """

    def __init__(self, profile_payload):
        self._profile = profile_payload
        self.calls = []

    async def get(self, path, params=None, extra_headers=None):
        self.calls.append(("GET", path, params))
        return self._profile

    async def post(self, path, body=None, extra_headers=None):
        self.calls.append(("POST", path, body))
        return {"status": "ok"}

    async def put(self, path, body=None, extra_headers=None):
        self.calls.append(("PUT", path, body))
        return {"status": "ok"}

    async def delete(self, path, body=None, extra_headers=None):
        self.calls.append(("DELETE", path, body))
        return {"status": "ok"}

    @property
    def paths(self):
        return [path for _verb, path, _payload in self.calls]


class RecordingTransport:
    """Stands in for `fullprofiles_write`, the single write chokepoint."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def __call__(self, section_body, profile_id=None):
        self.calls.append((section_body, profile_id))
        if self.fail:
            raise RuntimeError("simulated write-route failure")
        return {"status": "ok"}


def _fake_page():
    """A Playwright Page stand-in for the browser fallback.

    `evaluate` is awaited twice by the browser path: once for the edit-button
    JS, once for `_SAVE_MODAL_JS`. `on` / `remove_listener` are SYNC on a real
    Page, so they are MagicMocks, not AsyncMocks.
    """
    page = MagicMock()
    page.url = "https://www.naukri.com/mnjuser/profile"
    page.evaluate = AsyncMock(side_effect=["headline_edit", "structural"])
    page.on = MagicMock()
    page.remove_listener = MagicMock()
    return page


def _fake_browser(page):
    mock_browser = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=page)
    ctx.__aexit__ = AsyncMock(return_value=False)
    mock_browser.page_pool.acquire = MagicMock(return_value=ctx)
    return mock_browser


class _BoostHarness:
    """Wires every seam `_boost_visibility` touches. Nothing escapes it."""

    def __init__(self, headline=FAKE_HEADLINE, transport_fails=False):
        self.api = RecordingApi(_profile_payload(headline))
        self.transport = RecordingTransport(fail=transport_fails)
        self.page = _fake_page()
        self.emitted = []
        self._patches = []

    async def _emit(self, event):
        self.emitted.append(event)

    def __enter__(self):
        self._patches = [
            patch.object(profile_update, "api_client", self.api),
            patch.object(profile_update, "fullprofiles_write", self.transport),
            patch.object(profile_update, "browser", _fake_browser(self.page)),
            patch.object(profile_update, "page_goto", new_callable=AsyncMock),
            patch.object(profile_update.asyncio, "sleep", new_callable=AsyncMock),
            patch.object(event_bus, "emit", side_effect=self._emit),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False

    def events_named(self, name):
        return [e for e in self.emitted if type(e).__name__ == name]


# =====================================================================
# Control 1 - the route is INHERITED from the chokepoint, not re-typed
# =====================================================================

class TestBoostUsesTheWriteChokepoint:

    @pytest.mark.asyncio
    async def test_boost_calls_fullprofiles_write_exactly_once(self):
        with _BoostHarness() as h:
            result = await _boost_visibility()

        assert len(h.transport.calls) == 1, (
            "the boost must reach the wire through fullprofiles_write -- it is "
            "the only place that owns the v1 route and the "
            "X-HTTP-Method-Override: PUT header. Transport calls: %r"
            % (h.transport.calls,)
        )
        assert result["status"] == "success"
        assert set(result) == REST_RETURN_KEYS, (
            "the REST branch's return shape changed: %r" % (sorted(result),)
        )

    def test_the_chokepoint_owns_the_v1_route(self):
        """The route this call site inherits is the v1 write path."""
        assert "/v1/" in FULLPROFILES_WRITE_API
        assert FULLPROFILES_WRITE_API.endswith("/users/self/fullprofiles")


# =====================================================================
# Control 2 - a boost is a no-op RE-SAVE of the headline just read
# =====================================================================

class TestBoostPayload:

    @pytest.mark.asyncio
    async def test_payload_is_the_headline_resent_unchanged(self):
        with _BoostHarness() as h:
            await _boost_visibility()

        section_body, profile_id = h.transport.calls[0]
        assert section_body == {"profile": {"resumeHeadline": FAKE_HEADLINE}}, (
            "a boost re-sends the headline it just read, unchanged, inside the "
            "`profile` envelope. Got: %r" % (section_body,)
        )
        assert profile_id is None, (
            "the call site must NOT fabricate a profileId -- fullprofiles_write "
            "resolves and validates it. Got: %r" % (profile_id,)
        )


# =====================================================================
# Control 3 - the ("v0", "v2") loop is gone
# =====================================================================

class TestNoLegacyFullprofilesVersions:

    @pytest.mark.asyncio
    async def test_no_outgoing_call_carries_a_v0_or_v2_fullprofiles_path(self):
        with _BoostHarness() as h:
            await _boost_visibility()

        offenders = [
            p for p in h.api.paths
            if "fullprofiles" in p and ("/v0/" in p or "/v2/" in p)
        ]
        assert offenders == [], (
            "the boost reached a v0/v2 fullprofiles path. Neither is a write "
            "route for the profile editor; that loop is why zero boosts ever "
            "landed. Offending paths: %r" % (offenders,)
        )

    def test_profile_update_no_longer_references_the_v0_read_constant(self):
        assert not hasattr(profile_update, "FULLPROFILES_API"), (
            "profile_update imported FULLPROFILES_API again -- that constant is "
            "the v0 READ path and re-importing it here is how the v0/v2 write "
            "loop gets re-introduced. Writes go through profile_write."
        )


# =====================================================================
# Control 4 - the browser fallback survives
# =====================================================================

class TestBrowserFallbackSurvives:

    @pytest.mark.asyncio
    async def test_rest_failure_still_falls_back_to_the_browser(self):
        with _BoostHarness(transport_fails=True) as h:
            result = await _boost_visibility()

        assert len(h.transport.calls) == 1, "the REST attempt should still happen first"
        assert result["status"] == "success", (
            "a REST failure must fall back to the browser, not surface an "
            "error. Got: %r" % (result,)
        )
        assert result["method"] == "browser_headline_edit", (
            "the browser fallback is the only route that has ever had a chance "
            "of working -- it must not be lost. Got method %r" % (result.get("method"),)
        )
        assert h.page.evaluate.await_count == 2, (
            "the browser path opens the headline editor and clicks Save"
        )


# =====================================================================
# Control 5 - ProfileBoosted, honest about the new route
# =====================================================================

class TestProfileBoostedEvent:

    @pytest.mark.asyncio
    async def test_rest_success_emits_profileboosted_with_the_new_method(self):
        with _BoostHarness() as h:
            result = await _boost_visibility()

        boosted = h.events_named("ProfileBoosted")
        assert len(boosted) == 1, (
            "exactly one ProfileBoosted per successful boost. Got: %r"
            % ([type(e).__name__ for e in h.emitted],)
        )
        assert boosted[0].method == EXPECTED_METHOD, (
            "the event's method must name the route actually used. Expected %r, "
            "got %r" % (EXPECTED_METHOD, boosted[0].method)
        )
        assert result["method"] == EXPECTED_METHOD, (
            "the returned method and the emitted method must agree"
        )
        for stale in ("rest_api_v0", "rest_api_v2"):
            assert stale not in boosted[0].method, (
                "the method string still names a version that was never a "
                "write route: %r" % (boosted[0].method,)
            )


# =====================================================================
# Control 6 - no headline, no write
# =====================================================================

class TestEmptyHeadlineSendsNoWrite:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [
        {"profile": [{"resumeHeadline": ""}]},
        {"profile": [{}]},
        {"profile": []},
        {},
    ], ids=["empty-string", "key-missing", "no-profile-row", "empty-response"])
    async def test_no_headline_means_no_write(self, payload):
        with _BoostHarness() as h:
            h.api._profile = payload
            await _boost_visibility()

        assert h.transport.calls == [], (
            "with no headline to re-save there is nothing to write -- sending "
            "an empty resumeHeadline would blank the live profile. Transport "
            "calls: %r" % (h.transport.calls,)
        )
        assert h.events_named("ProfileBoosted") == [] or all(
            e.method.startswith("browser_") for e in h.events_named("ProfileBoosted")
        ), "only the browser fallback may claim a boost when there is no headline"
