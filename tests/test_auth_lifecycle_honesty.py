"""The auth lifecycle must be able to say "I do not know".

Guards ``naukri_session_info`` / ``naukri_logout`` / ``naukri_reauth`` against
the ONE defect the 2026-08-23 auth contract exists to prevent: deriving
``authenticated`` from the credential's PRESENCE instead of from a live
request. That substitution is what made the 8.5-hour outage of 2026-08-22
invisible -- the profile was there, the cookies were there, ``nauk_at`` was
there, and every presence check said "fine" while Naukri was refusing every
call.

Three null-not-false rules are pinned here, and each one has a CONTROL:

* ``authenticated`` is ``None`` whenever the live check did not complete.
  Never ``False``. ``False`` means Naukri said no.
* ``expired`` is ``None`` whenever no expiry is knowable. Never ``False``,
  which would read as "checked, still good".
* ``present`` is ``None`` when the jar could not be read at all, for the same
  reason: "not there" and "could not look" send the operator to two different
  places.

Plus the two properties that make the tools safe to call:

* NO CREDENTIAL VALUE leaves any of them, in any field, on any path, and none
  reaches a log record either.
* ``verify_live=False`` costs no network and no browser -- asserted with a
  tripwire that raises on any browser attribute other than the non-mutating
  ``cached_token`` read.

THE CONTROL that shows these can fail: ``scripts/presence_is_auth_control.py``
(a pytest plugin that re-creates the presence-is-auth build). Measured counts
are in its docstring. Tests that stay green under it are named there too, and
the asymmetry is the point.

SAFETY. The operator's session is LIVE. An autouse fixture points
``TokenManager._AUTH_STATE_FILE`` at a temp dir and replaces the browser
singleton, so no path through this module -- not even a buggy one -- can reach
the real ``auth_state.json`` or the real ``TokenManager``. The jar reader is
patched by default so the real Chrome profile is never read either. Nothing
here starts a browser or touches the network.
"""

import base64
import inspect
import json
import logging
import sqlite3
import time
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

SERVICE = "naukri_server.services.session_service"

#: A payload marker that could only have come from the token itself. Every
#: leak assertion looks for this, so a partial leak (one segment, a prefix)
#: fails just as loudly as a whole-token one.
TOKEN_MARKER = "SUPERSECRETNAUKATBODY"


def _b64(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwt(exp=None, marker=TOKEN_MARKER) -> str:
    """A syntactically real JWT whose payload carries a findable secret."""
    claims = {"sub": marker}
    if exp is not None:
        claims["exp"] = exp
    return "%s.%s.%s" % (
        _b64({"alg": "HS256", "typ": "JWT"}), _b64(claims), "c2lnbmF0dXJl")


def _status(logged_in, verified, reason):
    return {"status": "success", "logged_in": logged_in,
            "verified": verified, "reason": reason}


class _BrowserTripwire:
    """A browser provider on which everything but ``cached_token`` explodes.

    ``__getattr__`` only fires for attributes normal lookup did not find, so
    the two names defined below resolve silently and every other access --
    ``acquire_page``, ``navigate``, ``extract_token`` -- raises. This is what
    makes "verify_live=False touches no browser" a measurement rather than a
    claim about code nobody re-read.
    """

    def __init__(self, token=None):
        self.token = token

    async def cached_token(self):
        return self.token

    def __getattr__(self, name):
        raise AssertionError(
            "the offline path touched the browser provider: %r" % name)


class _FakeTokenManager:
    def __init__(self, token=None, cookies=None, mints=None, raises=None):
        self._token = token
        self._cookies = cookies
        self._mints = mints
        self._raises = raises
        self.invalidated = 0
        self.refresh_calls = []

    def invalidate(self):
        self.invalidated += 1
        self._token = None

    async def refresh_via_pool(self, page_pool, stale_token=None):
        self.refresh_calls.append(stale_token)
        if self._raises is not None:
            raise self._raises
        if self._mints:
            self._token = self._mints
        return self._mints


class _FakeBrowser:
    def __init__(self, manager=None, restart_mints=None, restart_raises=None):
        self.token_manager = manager or _FakeTokenManager()
        self.page_pool = MagicMock()
        self.stops = 0
        self.starts = 0
        self._restart_mints = restart_mints
        self._restart_raises = restart_raises

    async def stop(self):
        self.stops += 1

    async def start(self):
        self.starts += 1
        if self._restart_raises is not None:
            raise self._restart_raises
        self.token_manager._token = self._restart_mints


@pytest.fixture(autouse=True)
def _never_touch_the_live_session(tmp_path, monkeypatch):
    """The blast shield. See the module docstring."""
    from naukri_server.browser import TokenManager
    from naukri_server.services import session_service

    monkeypatch.setattr(
        TokenManager, "_AUTH_STATE_FILE", tmp_path / "auth_state.json")
    monkeypatch.setattr("naukri_server.browser.browser", _FakeBrowser())
    # A jar that reads fine and holds nothing, so a test that says nothing
    # about cookies gets a deterministic answer instead of this machine's.
    # THIS MODULE's binding only: `naukri_server.cookie_jar.read_jar` stays
    # real, because TestCookieJarReader below certifies that function itself
    # and would otherwise be testing this stub.
    monkeypatch.setattr(
        session_service, "_read_jar_rows", lambda profile, names: [])


def _jar(**by_name):
    """A read_jar stand-in returning ``{name: expires}`` in request order."""
    def read_jar(profile, names):
        return [{"name": n, "expires": by_name[n]} for n in names
                if n in by_name]
    return read_jar


async def _session_info(verify_live=True, token=None, status=None, jar=None,
                        jar_error=None):
    """Call the service with every outside edge scripted."""
    from naukri_server.services import session_service

    provider = MagicMock()
    provider.cached_token = AsyncMock(return_value=token)

    if jar_error is not None:
        def read_jar(profile, names):
            raise session_service.cookie_jar.CookieJarUnavailableError(jar_error)
    else:
        read_jar = _jar(**(jar or {}))

    login = AsyncMock(return_value=status or _status(False, True, "no_token"))
    with patch(SERVICE + ".browser_provider", provider), \
            patch(SERVICE + ".get_login_status", login), \
            patch(SERVICE + "._read_jar_rows", read_jar):
        result = await session_service.session_info(verify_live=verify_live)
    return result, login


# =====================================================================
# 1. authenticated: null is not false
# =====================================================================

class TestAuthenticatedIsNullNotFalse:

    async def test_an_unverifiable_check_is_null(self):
        """The exact substitution the contract bans. A 5xx, a dead network or
        an unreadable token all produce ``verified: False`` -- which is "could
        not ask", not "the answer was no"."""
        result, _ = await _session_info(
            token=_jwt(exp=time.time() + 3600),
            status=_status(False, False, "check_failed: NaukriAPIError http 503"),
        )

        assert result["authenticated"] is None, (
            "verified=False means the check could not run; reporting False "
            "here is the presence-is-auth bug wearing a different hat"
        )
        assert result["live_check"]["completed"] is False
        assert "503" in result["live_check"]["why_not"]
        assert "not because Naukri said no" in result["live_check"][
            "what_it_means"].replace("NOT because", "not because")

    async def test_a_real_denial_is_false(self):
        """The other half. A null that swallows a genuine no is just as wrong,
        and it is what a tool that always answers null would do."""
        result, _ = await _session_info(
            token=_jwt(exp=time.time() + 3600),
            status=_status(False, True, "api_denied"),
        )

        assert result["authenticated"] is False
        assert result["live_check"]["completed"] is True
        assert "real no" in result["live_check"]["what_it_means"]

    async def test_a_confirmed_session_is_true(self):
        result, login = await _session_info(
            token=_jwt(exp=time.time() + 3600),
            status=_status(True, True, "api_confirmed"),
        )

        assert result["authenticated"] is True
        assert result["live_check"]["completed"] is True
        assert result["live_check"]["attempted"] is True
        assert login.await_count == 1

    async def test_a_present_unexpired_credential_does_not_make_it_true(self):
        """THE defect, stated directly: a healthy-looking credential with a
        live check that came back empty is still a null. This is the shape of
        the 2026-08-22 outage -- nauk_at present, nauk_rt healthy, Naukri
        refusing every call."""
        result, _ = await _session_info(
            token=_jwt(exp=time.time() + 86400),
            jar={"nauk_at": time.time() + 86400,
                 "nauk_rt": time.time() + 188 * 86400},
            status=_status(False, False, "check_failed: RuntimeError"),
        )

        assert result["credential"]["present"] is True
        assert result["credential"]["expired"] is False
        assert result["authenticated"] is None

    async def test_offline_mode_is_null_in_the_contracts_words(self):
        result, login = await _session_info(verify_live=False, token=_jwt())

        assert result["authenticated"] is None
        assert result["live_check"]["attempted"] is False
        assert result["live_check"]["completed"] is False
        assert result["live_check"]["why_not"] == (
            "not attempted: this call asked for the offline answer")
        assert login.await_count == 0


# =====================================================================
# 2. expired: null is not false
# =====================================================================

class TestExpiredIsNullNotFalse:

    async def test_no_knowable_expiry_reports_null(self):
        """No cached token and no jar row: nothing on this machine knows when
        the credential lapses, and ``False`` there would read as 'checked,
        still good'."""
        result, _ = await _session_info(verify_live=False, token=None, jar={})

        cred = result["credential"]
        assert cred["expires_at"] is None
        assert cred["expires_in_days"] is None
        assert cred["expired"] is None
        assert cred["expiry_source"].startswith("unknown:")

    async def test_an_undecodable_token_falls_through_to_null(self):
        """A cached token that is not a JWT is not an expiry. It must not
        silently become one, and it must not become ``expired: False``."""
        result, _ = await _session_info(
            verify_live=False, token="not-a-jwt-at-all", jar={})

        assert result["credential"]["present"] is True
        assert result["credential"]["expired"] is None
        assert "no readable exp claim" in result["credential"]["expiry_source"]

    async def test_a_session_cookie_is_null_not_false(self):
        """Chrome's ``-1`` means "dies with the browser". There is no date to
        be on the right side of."""
        result, _ = await _session_info(
            verify_live=False, jar={"nauk_at": -1.0, "nauk_rt": -1.0})

        assert result["credential"]["expired"] is None
        assert "session cookie" in result["credential"]["note"]
        rt = [s for s in result["supporting"] if s["name"] == "nauk_rt"][0]
        assert rt["expired"] is None

    async def test_a_past_expiry_reports_true(self):
        """The control for the rule above: a knowable expiry in the past is a
        real True, or the null rule would be satisfiable by never deciding."""
        result, _ = await _session_info(
            verify_live=False, jar={"nauk_at": time.time() - 3 * 86400})

        assert result["credential"]["expired"] is True
        assert result["credential"]["expires_in_days"] < 0

    async def test_an_unreadable_jar_makes_presence_null_not_false(self):
        """"Not there" and "could not look" are different facts."""
        result, _ = await _session_info(
            verify_live=False, token=None, jar_error="jar is a directory")

        assert result["credential"]["present"] is None
        assert result["credential"]["expired"] is None
        assert "could not be read" in result["credential"]["expiry_source"]
        for entry in result["supporting"]:
            assert entry["present"] is None, entry
            assert entry["expired"] is None, entry


# =====================================================================
# 3. nauk_rt -- the field this whole build exists for
# =====================================================================

class TestTheRefreshCookieIsReported:

    async def test_supporting_names_nauk_rt_as_the_refresh_route(self):
        """The single field that would have turned 8.5 hours into 2 minutes:
        the refresh cookie, its role, and how long it has left."""
        result, _ = await _session_info(
            verify_live=False,
            jar={"nauk_at": time.time() - 3600,
                 "nauk_rt": time.time() + 188 * 86400,
                 "nauk_sid": time.time() + 188 * 86400,
                 "nauk_cs": time.time() + 364 * 86400},
        )

        roles = {s["name"]: s["role"] for s in result["supporting"]}
        assert roles == {"nauk_rt": "refresh", "nauk_sid": "session",
                         "nauk_cs": "csrf"}

        rt = [s for s in result["supporting"] if s["name"] == "nauk_rt"][0]
        assert rt["present"] is True
        assert rt["expired"] is False
        assert 187 < rt["expires_in_days"] < 189

        # The credential is dead and the refresh route is not: exactly the
        # 2026-08-22 state, and the reason renewal is silent rather than a
        # sign-in.
        assert result["credential"]["expired"] is True
        assert result["renewal"]["silent_renew_available"] is True
        assert result["renewal"]["tool"] == "naukri_reauth"
        assert "nauk_rt" in result["renewal"]["why"]

    async def test_durability_says_a_restart_costs_nothing(self):
        result, _ = await _session_info(verify_live=False)

        durability = result["durability"]
        assert durability["survives_server_restart"] is True
        assert durability["survives_machine_reboot"] is True
        assert "nauk_rt" in durability["why"]
        assert "no password" in durability["why"]

    async def test_the_profile_path_is_relativised(self):
        """`stored_in` goes through the repo's path relativiser, so it stays
        actionable without publishing the machine's layout."""
        from naukri_server.config import CHROME_PROFILE

        result, _ = await _session_info(verify_live=False)
        stored = result["durability"]["stored_in"]

        assert stored
        assert stored != CHROME_PROFILE
        assert ":" not in stored and "\\" not in stored


# =====================================================================
# 4. verify_live=False costs nothing
# =====================================================================

class TestOfflineModeIsFree:

    async def test_it_calls_neither_the_api_nor_the_browser(self):
        """A tripwire on both edges. ``cached_token`` is the one permitted
        browser touch -- it is an in-memory attribute read, and using
        ``extract_token`` instead would CLEAR the cache on a browser blip and
        destroy a working session during a status check."""
        from naukri_server.services import session_service

        tripwire = _BrowserTripwire(token=_jwt(exp=time.time() + 60))

        async def _no_network(*a, **k):
            raise AssertionError("the offline path called the live check")

        with patch(SERVICE + ".browser_provider", tripwire), \
                patch(SERVICE + ".get_login_status", _no_network), \
                patch("naukri_server.services.auth_service.api_client.get",
                      new_callable=AsyncMock) as api:
            result = await session_service.session_info(verify_live=False)

        assert result["authenticated"] is None
        assert api.await_count == 0

    async def test_the_tripwire_can_fire(self):
        """CONTROL: a fake that never objects proves nothing."""
        tripwire = _BrowserTripwire()
        with pytest.raises(AssertionError):
            tripwire.acquire_page

    async def test_verify_live_true_does_call_the_live_check(self):
        """CONTROL the other way: the default must actually ask, or the two
        modes are the same mode."""
        _, login = await _session_info(
            verify_live=True, status=_status(True, True, "api_confirmed"))
        assert login.await_count == 1


# =====================================================================
# 5. The credential value never leaves
# =====================================================================

class TestNoCredentialValueEscapes:

    def _walk(self, value, found):
        if isinstance(value, str):
            if TOKEN_MARKER in value:
                found.append(value)
        elif isinstance(value, dict):
            for k, v in value.items():
                self._walk(k, found)
                self._walk(v, found)
        elif isinstance(value, (list, tuple)):
            for v in value:
                self._walk(v, found)

    @pytest.mark.parametrize("verify_live", [True, False])
    async def test_session_info_never_returns_the_token(self, verify_live):
        token = _jwt(exp=time.time() + 3600)
        result, _ = await _session_info(
            verify_live=verify_live, token=token,
            status=_status(True, True, "api_confirmed"),
            jar={"nauk_at": time.time() + 3600})

        found = []
        self._walk(result, found)
        assert found == [], "the token reached a returned field: %s" % found
        # The expiry DID come through, so this is not vacuous.
        assert result["credential"]["expires_at"] is not None

    async def test_session_info_never_logs_the_token(self, caplog):
        token = _jwt(exp=time.time() + 3600)
        with caplog.at_level(logging.DEBUG):
            await _session_info(token=token,
                                status=_status(True, True, "api_confirmed"))

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert TOKEN_MARKER not in blob

    async def test_an_undecodable_token_does_not_leak_through_the_error_path(self):
        """The likeliest leak: a decoder that reports what it choked on. This
        one has no such branch, and the assertion is against a token that
        cannot be decoded at all."""
        broken = "%s.@@not-base64@@.%s" % (_b64({"alg": "none"}), TOKEN_MARKER)
        result, _ = await _session_info(verify_live=False, token=broken)

        found = []
        self._walk(result, found)
        assert found == []

    async def test_reauth_never_returns_the_token(self):
        token = _jwt(exp=time.time() + 3600)
        result = await _reauth(mints=token,
                               status=_status(True, True, "api_confirmed"))

        found = []
        self._walk(result, found)
        assert found == []
        assert result["credential"]["expires_at"] is not None


# =====================================================================
# 6. jwt_exp -- the decoder, pinned at its two return shapes
# =====================================================================

class TestJwtExp:

    @pytest.mark.parametrize("bad", [
        None, "", "notajwt", "a.b", "a.b.c.d",
        "%s.@@@.sig" % _b64({"alg": "none"}),
        "%s.%s.sig" % (_b64({"alg": "none"}), _b64(["not", "a", "dict"])),
        "%s.%s.sig" % (_b64({"alg": "none"}), _b64({"no": "exp"})),
        "%s.%s.sig" % (_b64({"alg": "none"}), _b64({"exp": "soon"})),
        "%s.%s.sig" % (_b64({"alg": "none"}), _b64({"exp": None})),
        "%s.%s.sig" % (_b64({"alg": "none"}), _b64({"exp": True})),
        123, object(),
    ])
    def test_anything_undecodable_is_none(self, bad):
        from naukri_server.services.session_service import jwt_exp
        assert jwt_exp(bad) is None

    def test_a_bool_exp_is_rejected_rather_than_read_as_1970(self):
        """bool is a subclass of int, so ``{"exp": true}`` would otherwise
        decode to 1.0 and be reported as a 56-year-old expiry."""
        from naukri_server.services.session_service import jwt_exp
        assert jwt_exp(_jwt(exp=True)) is None

    def test_a_real_exp_comes_back_as_a_float(self):
        from naukri_server.services.session_service import jwt_exp
        when = 1800000000
        got = jwt_exp(_jwt(exp=when))
        assert isinstance(got, float) and got == float(when)

    def test_the_return_is_only_ever_a_float_or_none(self):
        """The containment stated as a property: no input produces a string,
        so no input can produce the token."""
        from naukri_server.services.session_service import jwt_exp
        for candidate in (_jwt(exp=1), _jwt(), "x", None, 5, [], {}):
            got = jwt_exp(candidate)
            assert got is None or isinstance(got, float), repr(got)


# =====================================================================
# 7. logout -- local only, and it never raises
# =====================================================================

async def _logout(token="cached", state_file=None, manager=None):
    from naukri_server.browser import TokenManager
    from naukri_server.services import session_service

    fake = _FakeBrowser(manager or _FakeTokenManager(token=token))
    ctx = patch("naukri_server.browser.browser", fake)
    if state_file is not None:
        with ctx, patch.object(TokenManager, "_AUTH_STATE_FILE", state_file):
            return await session_service.logout(), fake
    with ctx:
        return await session_service.logout(), fake


class _LockedStateFile:
    """A Windows-shaped failure: the file is there and will not go."""

    def exists(self):
        return True

    def unlink(self):
        raise PermissionError("used by another process")


class TestLogout:

    async def test_it_clears_the_token_and_the_exported_state(self, tmp_path):
        state = tmp_path / "auth_state.json"
        state.write_text('{"token": "x"}', encoding="utf-8")

        result, fake = await _logout(state_file=state)

        assert result["cleared"] is True
        assert fake.token_manager.invalidated == 1
        assert fake.token_manager._token is None
        assert not state.exists()

    async def test_it_reports_false_when_there_was_nothing_to_clear(self, tmp_path):
        result, _ = await _logout(
            token=None, state_file=tmp_path / "never-written.json")

        assert result["cleared"] is False
        assert "nothing to clear" in result["reason"]

    async def test_it_never_raises_when_the_file_is_locked(self):
        """A logout that throws leaves the caller unable to tell how far it
        got -- the worst state for the one tool whose job is to make the state
        simple."""
        result, fake = await _logout(state_file=_LockedStateFile())

        assert result["cleared"] is True, "the token still went"
        assert fake.token_manager.invalidated == 1
        assert "not_removed" in result
        assert "auth_state.json" in result["not_removed"]
        assert "PermissionError" in result["not_removed"]

    async def test_it_never_raises_when_the_manager_explodes(self, tmp_path):
        manager = MagicMock()
        manager._token = "cached"
        manager.invalidate.side_effect = RuntimeError("browser is gone")

        result, _ = await _logout(manager=manager,
                                  state_file=tmp_path / "gone.json")

        assert result["authenticated"] is False
        assert "not_removed" in result

    async def test_it_does_not_delete_the_profile_or_sign_out(self, tmp_path):
        """The scope claim, asserted rather than trusted: the directory
        HOLDING the state file survives, and nothing was sent to Naukri.

        A stand-in profile dir rather than the real ``CHROME_PROFILE``, on
        purpose -- the real one is gitignored, so it does not exist on the CI
        runner and an assertion about it would be green here and red there for
        a reason that has nothing to do with logout.
        """
        profile = tmp_path / "chrome-profile"
        profile.mkdir()
        state = profile / "auth_state.json"
        state.write_text("{}", encoding="utf-8")
        sibling = profile / "Default"
        sibling.mkdir()

        with patch("naukri_server.services.auth_service.api_client.get",
                   new_callable=AsyncMock) as api:
            result, _ = await _logout(state_file=state)

        assert api.await_count == 0
        assert not state.exists(), "the state file was the target"
        assert profile.is_dir() and sibling.is_dir(), (
            "logout removed part of the profile -- it must only unlink the "
            "one exported file"
        )
        assert "NOT the Chrome profile itself" in result["scope"]
        assert "untouched" in result["what_is_lost"]
        assert "naukri_reauth" in result["recover_by"]

    def test_it_reaches_no_directory_removal_primitive(self):
        """Structural, because "it did not delete the profile this time" is a
        weaker claim than "it has no branch that could"."""
        import ast

        source = (Path(__file__).resolve().parent.parent / "naukri_server"
                  / "services" / "session_service.py").read_text(encoding="utf-8")
        fn = [n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "logout"]
        assert len(fn) == 1

        called = set()
        for node in ast.walk(fn[0]):
            if isinstance(node, ast.Call):
                func = node.func
                called.add(func.attr if isinstance(func, ast.Attribute)
                           else getattr(func, "id", ""))

        banned = {"rmtree", "rmdir", "remove", "removedirs", "rename",
                  "replace", "write_text", "write_bytes"}
        assert not (called & banned), sorted(called & banned)
        assert "unlink" in called, "not vacuous - it does remove one file"
        assert "invalidate" in called

    async def test_the_shape_is_the_contracts(self, tmp_path):
        result, _ = await _logout(state_file=tmp_path / "auth_state.json")

        for key in ("cleared", "scope", "authenticated", "reason",
                    "what_is_lost", "recover_by"):
            assert key in result, key
        assert result["authenticated"] is False
        assert isinstance(result["cleared"], bool)


# =====================================================================
# 8. reauth -- a fresh token is a reason to ask, never an answer
# =====================================================================

async def _reauth(token="stale", cookies="nauk_at=stale", mints=None,
                  raises=None, restart_mints=None, restart_raises=None,
                  status=None, jar=None):
    from naukri_server.services import session_service

    manager = _FakeTokenManager(
        token=token, cookies=cookies, mints=mints, raises=raises)
    fake = _FakeBrowser(manager, restart_mints=restart_mints,
                        restart_raises=restart_raises)
    login = AsyncMock(
        return_value=status or _status(True, True, "api_confirmed"))
    with patch("naukri_server.browser.browser", fake), \
            patch(SERVICE + ".get_login_status", login), \
            patch(SERVICE + "._read_jar_rows", _jar(**(jar or {}))):
        result = await session_service.reauth()
    result["_fake"] = fake
    return result


#: Every METHOD call that could put a sign-in form in front of the operator,
#: or type into one. ``reauth`` reaching any of these means it is ``login``
#: wearing a different name, which the contract rules out for this server.
#: Method-shaped on purpose -- matched against ``x.f()`` calls only, because
#: the bare names collide with builtins (``type(exc).__name__`` is not a
#: keystroke into a login form).
NAVIGATION_CALLS = {
    "safe_goto", "safe_fill", "goto", "navigate", "acquire_page",
    "click", "fill", "type", "new_page",
}

#: The module-level navigation helpers, matched against bare-name calls.
NAVIGATION_HELPERS = {"page_goto", "page_safe_fill", "page_intercept_json"}


def _reauth_calls_and_literals():
    """(attribute calls, bare-name calls, literals) inside ``reauth``."""
    import ast

    source = (Path(__file__).resolve().parent.parent / "naukri_server"
              / "services" / "session_service.py").read_text(encoding="utf-8")
    fn = [n for n in ast.walk(ast.parse(source))
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "reauth"]
    assert len(fn) == 1, "reauth not found in session_service.py"

    attr_calls = set()
    name_calls = set()
    literals = []
    for node in ast.walk(fn[0]):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                attr_calls.add(func.attr)
            elif isinstance(func, ast.Name):
                name_calls.add(func.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
    return attr_calls, name_calls, literals


class TestReauth:

    async def test_a_confirmed_renew_reports_renewed(self):
        result = await _reauth(mints="fresh-token",
                               status=_status(True, True, "api_confirmed"))

        assert result["renewed"] is True
        assert result["authenticated"] is True
        assert result["stage"] == "cache_refresh"
        assert result["_fake"].token_manager.refresh_calls == ["stale"]
        assert result["_fake"].starts == 0, "stage 2 must not run on success"

    async def test_a_minted_token_with_an_unverifiable_check_is_not_renewed(self):
        """THE rule that stops this being a decoy. Naukri hands a nauk_at to a
        revoked account too, so a token appearing proves nothing."""
        result = await _reauth(
            mints="fresh-token",
            status=_status(False, False, "check_failed: NaukriAPIError http 502"),
        )

        assert result["renewed"] is False, (
            "renewed must require a live check that said yes")
        assert result["authenticated"] is None, "unverifiable is not a no"
        assert result["stage"] == "cache_refresh"
        assert "could not complete" in result["reason"]

    async def test_a_minted_token_the_api_refuses_is_not_renewed(self):
        result = await _reauth(mints="fresh-token",
                               status=_status(False, True, "api_denied"))

        assert result["renewed"] is False
        assert result["authenticated"] is False
        assert "naukri_login" in result["reason"]

    async def test_a_failed_renew_puts_the_old_credential_back(self):
        """refresh_via_pool nulls the cached token before it reloads, so
        without this a diagnostic call would DESTROY a session that was
        working."""
        result = await _reauth(
            token="was-working", cookies="nauk_at=was-working",
            raises=RuntimeError("chrome is not answering"),
            status=_status(False, False, "check_failed: RuntimeError"),
        )
        manager = result["_fake"].token_manager

        assert result["renewed"] is False
        assert manager._token == "was-working"
        assert manager._cookies == "nauk_at=was-working"

    async def test_stage_two_runs_only_when_stage_one_produced_nothing(self):
        result = await _reauth(mints=None, restart_mints="fresh-after-restart",
                               status=_status(True, True, "api_confirmed"))

        assert result["stage"] == "browser_restart"
        assert result["renewed"] is True
        assert result["_fake"].stops == 1 and result["_fake"].starts == 1

    async def test_both_stages_failing_names_naukri_login(self):
        result = await _reauth(
            raises=RuntimeError("pool is dead"),
            restart_raises=RuntimeError("profile is locked"),
            status=_status(False, True, "no_token"),
        )

        assert result["renewed"] is False
        assert result["stage"] == "none"
        assert "naukri_login" in result["reason"]
        assert "pool is dead" in result["reason"]
        assert "profile is locked" in result["reason"]

    async def test_it_takes_no_credential_parameter(self):
        """By signature, so it cannot be handed a password by accident."""
        from naukri_server.services.session_service import reauth
        from naukri_server.tools.auth import naukri_reauth

        assert list(inspect.signature(reauth).parameters) == []
        target = getattr(naukri_reauth, "__wrapped__", naukri_reauth)
        assert list(inspect.signature(target).parameters) == []

    def test_it_never_navigates_to_a_login_page(self):
        """A STRUCTURAL scan, because the guarantee is about a branch that
        must not exist rather than one that must not fire.

        AST rather than substring matching, and that is not fastidiousness:
        the prose in this function says "no password is typed" and "no login
        page", so a text scan for those words fails on the very docstring that
        promises the property. Calls are the thing that can navigate.
        """
        attr_calls, name_calls, literals = _reauth_calls_and_literals()

        overlap = (attr_calls & NAVIGATION_CALLS) | (
            name_calls & NAVIGATION_HELPERS)
        assert not overlap, (
            "reauth calls %s -- it must never open a sign-in" % sorted(overlap))
        for text in literals:
            assert "nlogin" not in text, text

        # Not vacuous: it must still do the two things it is for.
        assert "refresh_via_pool" in attr_calls
        assert "get_login_status" in name_calls

    def test_CONTROL_the_navigation_scan_can_fire(self):
        """A scanner that has never been shown detecting certifies nothing."""
        import ast

        tree = ast.parse(
            "async def reauth():\n"
            "    await browser_provider.safe_goto(page, '/nlogin/login')\n"
            "    await page_goto(page, '/nlogin/login')\n")
        fn = tree.body[0]
        attr_calls = {n.func.attr for n in ast.walk(fn)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)}
        name_calls = {n.func.id for n in ast.walk(fn)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name)}

        assert attr_calls & NAVIGATION_CALLS == {"safe_goto"}
        assert name_calls & NAVIGATION_HELPERS == {"page_goto"}

    async def test_the_shape_is_the_contracts(self):
        result = await _reauth(mints="fresh-token")

        for key in ("renewed", "authenticated", "method", "stage",
                    "checked_against", "reason", "credential"):
            assert key in result, key
        assert isinstance(result["renewed"], bool)
        assert result["credential"]["name"] == "nauk_at"


# =====================================================================
# 9. Shape invariants across every branch of session_info
# =====================================================================

BRANCHES = [
    # (id, verify_live, token, status, expected_authenticated)
    ("offline-no-token", False, None, None, None),
    ("offline-with-token", False, "cached", None, None),
    ("no_token", True, None, _status(False, True, "no_token"), False),
    ("api_confirmed", True, "cached", _status(True, True, "api_confirmed"), True),
    ("api_denied", True, "cached", _status(False, True, "api_denied"), False),
    ("http-401", True, "cached",
     _status(False, True, "session_expired: http 401"), False),
    ("check_failed-exception", True, "cached",
     _status(False, False, "check_failed: RuntimeError"), None),
    ("check_failed-missing-field", True, "cached",
     _status(False, False, "check_failed: missing loggedInStatus"), None),
]
BRANCH_IDS = [b[0] for b in BRANCHES]
BRANCH_ARGS = [b[1:] for b in BRANCHES]


class TestShapeInvariants:

    @pytest.mark.parametrize("verify_live,token,status,expected", BRANCH_ARGS,
                             ids=BRANCH_IDS)
    async def test_every_branch_carries_the_full_contract_shape(
            self, verify_live, token, status, expected):
        result, _ = await _session_info(
            verify_live=verify_live, token=token, status=status)

        for key in ("server", "authenticated", "checked_against", "live_check",
                    "credential", "supporting", "credential_source",
                    "durability", "renewal", "on_expiry"):
            assert key in result, key
        assert result["server"] == "naukri"
        assert result["authenticated"] is expected
        assert result["live_check"]["completed"] is (expected is not None)
        if expected is None:
            assert result["live_check"]["why_not"]
        assert result["credential"]["expired"] in (True, False, None)
        assert result["credential"]["kind"] == "cookie"
        assert result["credential"]["name"] == "nauk_at"

    @pytest.mark.parametrize("verify_live,token,status,expected", BRANCH_ARGS,
                             ids=BRANCH_IDS)
    async def test_completed_never_disagrees_with_the_verdict(
            self, verify_live, token, status, expected):
        """Two fields that can disagree is how a null starts reading as a no."""
        result, _ = await _session_info(
            verify_live=verify_live, token=token, status=status)

        assert result["live_check"]["completed"] == (
            result["authenticated"] is not None)


# =====================================================================
# 10. Registered, and registered THROUGH the wrapper
# =====================================================================

NEW_TOOLS = ("naukri_session_info", "naukri_logout", "naukri_reauth")


class TestTheToolsAreRegistered:

    @pytest.mark.parametrize("name", NEW_TOOLS)
    def test_the_tool_is_in_the_registry(self, name):
        import naukri_server.tools.auth  # noqa: F401 - registers on import
        from naukri_server import mcp

        assert name in mcp._tool_manager._tools

    @pytest.mark.parametrize("name", NEW_TOOLS)
    def test_the_tool_goes_through_the_watchdog_and_scrubber(self, name):
        """Registration alone is not enough: a tool that bypassed
        ``_watchdogged_tool`` also bypassed the server-wide path scrubber."""
        from naukri_server.tools import auth

        fn = getattr(auth, name)
        assert getattr(fn, "__wrapped_by_watchdog__", False), (
            "%s is not registered through the wrapper" % name)

    def test_the_three_tools_joined_the_existing_auth_surface(self):
        """The registry census, extended. Named rather than counted, so it
        stays true as the surface grows and still fails if one goes missing."""
        from naukri_server import mcp

        registered = set(mcp._tool_manager._tools)
        expected = set(NEW_TOOLS) | {
            "naukri_login", "naukri_verify_otp", "naukri_auth_status"}
        assert expected <= registered, sorted(expected - registered)


# =====================================================================
# 11. The cookie jar reader -- copy first, metadata only
# =====================================================================

WEBKIT = 11644473600
JAR_SECRET = "COOKIEVALUETHATMUSTNEVERBEREAD"


def _build_jar(profile_dir: Path, rows):
    """A synthetic Chrome cookie jar, values included, so a leak has
    something to leak."""
    jar = profile_dir / "Default" / "Network" / "Cookies"
    jar.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(jar))
    con.execute(
        "CREATE TABLE cookies (name TEXT, host_key TEXT, value TEXT, "
        "encrypted_value BLOB, expires_utc INTEGER, has_expires INTEGER, "
        "is_persistent INTEGER)")
    con.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(n, h, JAR_SECRET, b"sealed", e, he, ip) for n, h, e, he, ip in rows])
    con.commit()
    con.close()
    return jar


class TestCookieJarReader:

    def test_it_reads_the_webkit_epoch_correctly(self, tmp_path):
        from naukri_server import cookie_jar

        when = 1800000000
        _build_jar(tmp_path, [
            ("nauk_at", ".naukri.com", (when + WEBKIT) * 1000000, 1, 1)])

        rows = cookie_jar.read_jar(tmp_path, ["nauk_at"])

        assert rows == [{"name": "nauk_at", "expires": float(when)}]

    def test_a_session_cookie_reads_as_minus_one(self, tmp_path):
        from naukri_server import cookie_jar

        _build_jar(tmp_path, [("nauk_sid", ".naukri.com", 0, 0, 0)])

        assert cookie_jar.read_jar(tmp_path, ["nauk_sid"]) == [
            {"name": "nauk_sid", "expires": -1.0}]

    def test_a_lookalike_domain_is_not_naukri(self, tmp_path):
        """``endswith("naukri.com")`` would accept notnaukri.com, which is a
        different site that can set a cookie with the same name."""
        from naukri_server import cookie_jar

        _build_jar(tmp_path, [
            ("nauk_at", "notnaukri.com", (1800000000 + WEBKIT) * 1000000, 1, 1)])

        assert cookie_jar.read_jar(tmp_path, ["nauk_at"]) == []

    def test_no_cookie_value_can_come_back(self, tmp_path):
        from naukri_server import cookie_jar

        _build_jar(tmp_path, [
            ("nauk_at", ".naukri.com", (1800000000 + WEBKIT) * 1000000, 1, 1),
            ("nauk_rt", ".naukri.com", (1900000000 + WEBKIT) * 1000000, 1, 1)])

        rows = cookie_jar.read_jar(tmp_path, ["nauk_at", "nauk_rt"])

        assert rows, "not vacuous - rows were returned"
        assert JAR_SECRET not in json.dumps(rows)
        assert all(set(r) == {"name", "expires"} for r in rows)

    def test_the_query_names_metadata_columns_only(self):
        """The enforcement point, asserted at the query rather than at the
        result: a wildcard select would pass the test above on a jar whose
        value column happened to be empty."""
        from naukri_server.cookie_jar import _JAR_QUERY

        assert "*" not in _JAR_QUERY
        assert "value" not in _JAR_QUERY.lower()

    def test_the_live_file_is_never_opened(self, tmp_path):
        """sqlite writes to what it opens -- it replays journals and takes
        locks -- and the file in question is the operator's live profile."""
        from naukri_server import cookie_jar

        jar = _build_jar(tmp_path, [("nauk_at", ".naukri.com", 0, 0, 0)])
        opened = []
        real_connect = sqlite3.connect

        def spy(path, *a, **k):
            opened.append(str(path))
            return real_connect(path, *a, **k)

        with patch.object(cookie_jar.sqlite3, "connect", spy):
            cookie_jar.read_jar(tmp_path, ["nauk_at"])

        assert opened, "not vacuous - sqlite was used"
        assert str(jar) not in opened, (
            "the LIVE jar was opened: %s" % opened)

    def test_the_journal_travels_with_the_copy(self, tmp_path):
        """A copy taken without its journal can hand back rows the journal
        would have rolled back."""
        from naukri_server import cookie_jar

        jar = _build_jar(tmp_path, [("nauk_at", ".naukri.com", 0, 0, 0)])
        jar.with_name(jar.name + "-journal").write_bytes(b"journal")
        copied = []

        real_connect = sqlite3.connect

        def spy(path, *a, **k):
            copied.append(sorted(p.name for p in Path(path).parent.iterdir()))
            return real_connect(path, *a, **k)

        with patch.object(cookie_jar.sqlite3, "connect", spy):
            cookie_jar.read_jar(tmp_path, ["nauk_at"])

        assert copied and "Cookies-journal" in copied[0], copied

    def test_the_copy_is_deleted_afterwards(self, tmp_path):
        from naukri_server import cookie_jar

        _build_jar(tmp_path, [("nauk_at", ".naukri.com", 0, 0, 0)])
        seen = []
        real_connect = sqlite3.connect

        def spy(path, *a, **k):
            seen.append(Path(path).parent)
            return real_connect(path, *a, **k)

        with patch.object(cookie_jar.sqlite3, "connect", spy):
            cookie_jar.read_jar(tmp_path, ["nauk_at"])

        assert seen and not seen[0].exists()

    def test_a_missing_profile_raises_rather_than_returning_empty(self, tmp_path):
        """An empty list because the path was wrong is indistinguishable from
        an empty list because the operator is signed out, and the two mean
        opposite things."""
        from naukri_server import cookie_jar

        with pytest.raises(cookie_jar.CookieJarUnavailableError) as exc:
            cookie_jar.read_jar(tmp_path / "nope", ["nauk_at"])
        assert "does not exist" in str(exc.value)

    def test_a_profile_with_no_jar_raises(self, tmp_path):
        from naukri_server import cookie_jar

        with pytest.raises(cookie_jar.CookieJarUnavailableError) as exc:
            cookie_jar.read_jar(tmp_path, ["nauk_at"])
        assert "never been signed in" in str(exc.value)

    def test_a_file_that_is_not_a_jar_raises(self, tmp_path):
        from naukri_server import cookie_jar

        jar = tmp_path / "Default" / "Network" / "Cookies"
        jar.parent.mkdir(parents=True)
        jar.write_text("this is not sqlite", encoding="utf-8")

        with pytest.raises(cookie_jar.CookieJarUnavailableError):
            cookie_jar.read_jar(tmp_path, ["nauk_at"])

    def test_a_requested_name_that_is_absent_is_simply_absent(self, tmp_path):
        from naukri_server import cookie_jar

        _build_jar(tmp_path, [("nauk_at", ".naukri.com", 0, 0, 0)])

        rows = cookie_jar.read_jar(tmp_path, ["nauk_at", "nauk_rt"])

        assert [r["name"] for r in rows] == ["nauk_at"]
