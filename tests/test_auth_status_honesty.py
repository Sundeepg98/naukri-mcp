"""naukri_auth_status must be able to answer "no".

Background (measured, not guessed): ``services.auth_service.get_login_status``
called ``api_client.get(ACTIVITY_LEVEL_API)`` - an endpoint that REQUIRES a
token. With no token the call raised, ``handle_tool_action`` turned that into
``{"status": "error", ...}`` with NO ``logged_in`` key, and every agent-loop
preflight gate written as ``if auth_status()["logged_in"]`` blew up with a
KeyError. The one tool whose whole job is answering "am I logged in?" was
structurally incapable of returning False.

Contract pinned here - ALWAYS status "success", ALWAYS a bool ``logged_in``,
plus ``verified`` (did we actually determine this?) and ``reason``:

  no cached token                 -> False / verified True  / "no_token"
  API says logged in              -> True  / verified True  / "api_confirmed"
  API says NOT logged in          -> False / verified True  / "api_denied"
  NotLoggedInError/AuthExpired    -> False / verified True  / "session_expired"
  NaukriAPIError 401 / 403        -> False / verified True  / "session_expired: ..."
  anything else (net/5xx/bug)     -> False / verified FALSE / "check_failed: ..."

Plus the side-effect hazard: reading the token for a STATUS CHECK must never
run ``TokenManager.extract()``, because extract() CLEARS the cached token when
cookie extraction fails - a status check during a browser blip would destroy a
perfectly good session.

Every test is PURE: no network, no browser, no file I/O.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_server.api import NaukriAPIError
from naukri_server.browser import AuthExpiredError, NotLoggedInError

AUTH_SERVICE = "naukri_server.services.auth_service"


def _fake_provider(token):
    """A BrowserProvider stand-in whose cached_token() returns ``token``."""
    provider = MagicMock()
    provider.cached_token = AsyncMock(return_value=token)
    provider.extract_token = AsyncMock(return_value=token)
    return provider


async def _run(token, api_returns=None, api_raises=None):
    """Call get_login_status() with a cached token and a scripted API result."""
    from naukri_server.services.auth_service import get_login_status

    provider = _fake_provider(token)
    with patch(AUTH_SERVICE + ".browser_provider", provider, create=True), \
            patch(AUTH_SERVICE + ".api_client.get", new_callable=AsyncMock) as mock_get:
        if api_raises is not None:
            mock_get.side_effect = api_raises
        else:
            mock_get.return_value = api_returns
        result = await get_login_status()
    return result, mock_get, provider


# =====================================================================
# 1. The negative case the tool could not previously express
# =====================================================================

class TestNegativeCase:

    @pytest.mark.asyncio
    async def test_not_logged_in_error_reports_logged_in_false(self):
        """The exact live failure: token present, API rejects it."""
        result, _, _ = await _run(
            "cached-jwt",
            api_raises=NotLoggedInError("Not logged in - call naukri_login first"),
        )

        assert result["status"] == "success"
        assert result["logged_in"] is False
        assert result["verified"] is True
        assert result["reason"] == "session_expired"

    @pytest.mark.asyncio
    async def test_auth_expired_error_reports_logged_in_false(self):
        result, _, _ = await _run(
            "cached-jwt",
            api_raises=AuthExpiredError("Session expired - login wall"),
        )

        assert result["logged_in"] is False
        assert result["verified"] is True
        assert result["reason"] == "session_expired"

    @pytest.mark.asyncio
    async def test_no_cached_token_never_calls_the_api(self):
        """No token -> answerable without an API call that would need one."""
        result, mock_get, _ = await _run(None, api_returns={"loggedInStatus": True})

        assert result["status"] == "success"
        assert result["logged_in"] is False
        assert result["verified"] is True
        assert result["reason"] == "no_token"
        assert mock_get.call_count == 0, (
            "the no-token branch must not call a token-requiring endpoint"
        )


# =====================================================================
# 2. The positive and explicit-denial cases
# =====================================================================

class TestPositiveAndDenial:

    @pytest.mark.asyncio
    async def test_api_confirms_logged_in(self):
        result, mock_get, _ = await _run(
            "cached-jwt", api_returns={"loggedInStatus": True, "level": "HIGH"})

        assert result["logged_in"] is True
        assert result["verified"] is True
        assert result["reason"] == "api_confirmed"
        assert mock_get.call_count == 1

    @pytest.mark.asyncio
    async def test_api_explicitly_denies(self):
        result, _, _ = await _run(
            "cached-jwt", api_returns={"loggedInStatus": False})

        assert result["logged_in"] is False
        assert result["verified"] is True
        assert result["reason"] == "api_denied"


# =====================================================================
# 3. Fail-closed: could-not-check is NOT a proven logout
# =====================================================================

class TestFailClosed:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [401, 403])
    async def test_http_401_403_is_a_proven_expiry(self, status_code):
        result, _, _ = await _run(
            "cached-jwt",
            api_raises=NaukriAPIError(status=status_code, message="Unauthorized"),
        )

        assert result["logged_in"] is False
        assert result["verified"] is True
        assert "session_expired" in result["reason"]
        assert str(status_code) in result["reason"]

    @pytest.mark.asyncio
    async def test_http_500_is_a_could_not_check(self):
        result, _, _ = await _run(
            "cached-jwt",
            api_raises=NaukriAPIError(status=500, message="Internal Server Error"),
        )

        assert result["logged_in"] is False
        assert result["verified"] is False, (
            "a 5xx proves nothing about the session - verified must be False"
        )
        assert result["reason"].startswith("check_failed")

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_a_could_not_check(self):
        result, _, _ = await _run(
            "cached-jwt", api_raises=RuntimeError("connection reset by peer"))

        assert result["logged_in"] is False
        assert result["verified"] is False
        assert "RuntimeError" in result["reason"]

    @pytest.mark.asyncio
    async def test_token_read_failure_is_a_could_not_check(self):
        """A dead browser makes the token unreadable - that is not a logout."""
        from naukri_server.services.auth_service import get_login_status

        provider = MagicMock()
        provider.cached_token = AsyncMock(side_effect=RuntimeError("browser dead"))
        with patch(AUTH_SERVICE + ".browser_provider", provider, create=True), \
                patch(AUTH_SERVICE + ".api_client.get",
                      new_callable=AsyncMock) as mock_get:
            result = await get_login_status()

        assert result["status"] == "success"
        assert result["logged_in"] is False
        assert result["verified"] is False
        assert result["reason"].startswith("check_failed")
        assert mock_get.call_count == 0


# =====================================================================
# 4. Shape invariants across every branch
# =====================================================================

BRANCHES = [
    # (token, api_returns, api_raises, expected_verified)
    (None, {"loggedInStatus": True}, None, True),
    ("t", {"loggedInStatus": True}, None, True),
    ("t", {"loggedInStatus": False}, None, True),
    ("t", None, NotLoggedInError("gone"), True),
    ("t", None, AuthExpiredError("gone"), True),
    ("t", None, NaukriAPIError(status=401, message="Unauthorized"), True),
    ("t", None, NaukriAPIError(status=503, message="Unavailable"), False),
    ("t", None, RuntimeError("boom"), False),
]


class TestShapeInvariants:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("token,api_returns,api_raises,expected_verified", BRANCHES)
    async def test_every_branch_has_the_full_key_set(
            self, token, api_returns, api_raises, expected_verified):
        result, _, _ = await _run(token, api_returns=api_returns, api_raises=api_raises)

        assert result["status"] == "success", (
            "get_login_status must never surface as status=error: %r" % (result,)
        )
        assert isinstance(result["logged_in"], bool), (
            "logged_in must be a real bool, got %r" % (result["logged_in"],)
        )
        assert isinstance(result["verified"], bool)
        assert isinstance(result["reason"], str) and result["reason"]
        assert result["verified"] is expected_verified

    @pytest.mark.asyncio
    @pytest.mark.parametrize("token,api_returns,api_raises,expected_verified", BRANCHES)
    async def test_get_login_status_never_raises(
            self, token, api_returns, api_raises, expected_verified):
        result, _, _ = await _run(token, api_returns=api_returns, api_raises=api_raises)
        assert "logged_in" in result


# =====================================================================
# 5. The non-mutating token read (the side-effect hazard)
# =====================================================================

class TestNonMutatingTokenRead:

    @pytest.mark.asyncio
    async def test_cached_token_does_not_re_extract(self):
        """PlaywrightBrowserProvider.cached_token() must NOT call extract().

        TokenManager.extract() sets ``self._token = None`` whenever cookie
        extraction fails, so calling it from a read-only status check can
        destroy a valid session during a transient browser blip.
        """
        from naukri_server.interfaces import PlaywrightBrowserProvider

        token_mgr = MagicMock()
        token_mgr._token = "cached-jwt"
        token_mgr.extract = AsyncMock()
        fake_browser = MagicMock()
        fake_browser.token_manager = token_mgr

        with patch("naukri_server.browser.browser", fake_browser):
            token = await PlaywrightBrowserProvider().cached_token()

        assert token == "cached-jwt"
        token_mgr.extract.assert_not_awaited()
        token_mgr.extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_cached_token_returns_none_when_manager_missing(self):
        from naukri_server.interfaces import PlaywrightBrowserProvider

        fake_browser = MagicMock()
        fake_browser.token_manager = None

        with patch("naukri_server.browser.browser", fake_browser):
            assert await PlaywrightBrowserProvider().cached_token() is None

    @pytest.mark.asyncio
    async def test_extract_token_still_extracts(self):
        """The mutating reader keeps its old behaviour - we only ADDED a reader."""
        from naukri_server.interfaces import PlaywrightBrowserProvider

        token_mgr = MagicMock()
        token_mgr._token = "cached-jwt"
        token_mgr.extract = AsyncMock()
        fake_browser = MagicMock()
        fake_browser.token_manager = token_mgr

        with patch("naukri_server.browser.browser", fake_browser):
            token = await PlaywrightBrowserProvider().extract_token()

        assert token == "cached-jwt"
        token_mgr.extract.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_abc_default_delegates_to_extract_token(self):
        """cached_token() is CONCRETE on the ABC so existing fakes keep working."""
        from naukri_server.interfaces import BrowserProvider

        class LegacyFakeProvider(BrowserProvider):
            """A pre-existing fake that only implements the old abstract set."""

            def __init__(self):
                self.extract_calls = 0

            async def navigate(self, url):
                return None

            def acquire_page(self):
                return None

            async def evaluate_js(self, page, js, arg=None, timeout=15):
                return None

            async def safe_goto(self, page, url, wait_until="domcontentloaded"):
                return True

            async def safe_text(self, page, selector):
                return None

            async def safe_fill(self, page, selector, value, delay=30):
                return True

            async def intercept_json(self, page, url, url_pattern=None,
                                     timeout=10, wait="domcontentloaded"):
                return None

            async def extract_token(self):
                self.extract_calls += 1
                return "legacy-token"

        fake = LegacyFakeProvider()
        assert await fake.cached_token() == "legacy-token"
        assert fake.extract_calls == 1


# =====================================================================
# 6. End-to-end through the MCP tool wrapper
# =====================================================================

class TestToolSurface:

    @pytest.mark.asyncio
    async def test_naukri_auth_status_surfaces_logged_in_false(self):
        """The preflight gate `auth_status()["logged_in"]` must not KeyError."""
        from naukri_server.tools.auth import naukri_auth_status

        provider = _fake_provider(None)
        with patch(AUTH_SERVICE + ".browser_provider", provider, create=True), \
                patch(AUTH_SERVICE + ".api_client.get", new_callable=AsyncMock):
            result = await naukri_auth_status()

        assert result["status"] == "success"
        assert result["logged_in"] is False
        assert result["verified"] is True
        assert result["reason"] == "no_token"
