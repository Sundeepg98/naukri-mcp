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
from contextlib import ExitStack
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


#: The base64url of `{"alg":"HS256","typ":"JWT"}` -- BYTE-IDENTICAL in every
#: HS256 JWT ever minted, this server's and anybody else's. It is a public
#: constant, not a secret, which is why `_token_needles` refuses to hunt it.
JWT_PUBLIC_HEADER = _b64({"alg": "HS256", "typ": "JWT"})


def _jwt(exp=None, marker=TOKEN_MARKER) -> str:
    """A syntactically real JWT whose payload carries a findable secret.

    The SIGNATURE is derived from the marker rather than hardcoded. It used to
    be the literal `c2lnbmF0dXJl`, which made it a constant shared by every
    token this module builds -- so two tokens carrying DIFFERENT secrets
    collided on it and `_find_leak` reported a leak of one when shown the
    other. A per-token signature is what makes the needle set discriminating.
    """
    claims = {"sub": marker}
    if exp is not None:
        claims["exp"] = exp
    return "%s.%s.%s" % (
        JWT_PUBLIC_HEADER, _b64(claims), _b64({"sig": marker}))


def _status(logged_in, verified, reason):
    return {"status": "success", "logged_in": logged_in,
            "verified": verified, "reason": reason}


def _token_needles(token):
    """The whole token AND each of its SECRET-BEARING segments.

    Segments matter: a leak that publishes only the payload is still a leak,
    and searching for a plaintext marker misses BOTH -- the marker lives
    base64-encoded inside the payload and never appears in the token string.

    THE HEADER IS EXCLUDED, and that is the point of this function rather than
    an optimisation. `JWT_PUBLIC_HEADER` is identical in every HS256 JWT, so a
    needle set containing it fires on any unrelated token and proves nothing
    about THIS credential. Measured 2026-08-23: with the header in the set,
    `_find_leak` reported a leak when handed a completely different token whose
    only shared substring was those 36 public characters. A needle that cannot
    distinguish the secret from a public constant is not evidence of a leak --
    it is a coin flip that happens to land on "leak" whenever a JWT is nearby.
    `test_CONTROL_the_walker_is_quiet_on_a_foreign_token` pins both directions.
    """
    parts = str(token).split(".")
    if len(parts) == 3 and parts[0] == JWT_PUBLIC_HEADER:
        parts = parts[1:]
    return [str(token)] + [p for p in parts if len(p) > 8]


def _find_leak(value, needles):
    """Every string anywhere in ``value`` containing any of ``needles``.

    Walks dict KEYS as well as values: a result keyed by the credential leaks
    it just as thoroughly as one that returns it.
    """
    hits = []

    def walk(node):
        if isinstance(node, str):
            if any(n and n in node for n in needles):
                hits.append(node)
        elif isinstance(node, dict):
            for key, val in node.items():
                walk(key)
                walk(val)
        elif isinstance(node, (list, tuple)):
            for val in node:
                walk(val)

    walk(value)
    return hits


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


class _UnreadableStateFile:
    """auth_state.json is there and will not open -- a Windows-shaped lock."""

    def is_file(self):
        return True

    def read_text(self, encoding=None):
        raise PermissionError("used by another process")


def _write_state(tmp_path, token=None, body=None):
    """A real auth_state.json, in the shape ``_export_auth_state`` writes.

    The ``cookies`` header carries the token VALUE, exactly as the live file
    does. That is deliberate: it gives the leak walkers something real to
    catch if the reader ever starts returning more than a date.
    """
    path = tmp_path / "auth_state.json"
    if body is None:
        body = {"cookies": "nauk_at=%s; nauk_rt=REFRESHCOOKIEVALUE" % (
            token or ""), "exported_at": time.time(), "cdp_port": 9223}
        if token is not None:
            body["token"] = token
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


async def _session_info(verify_live=True, token=None, status=None, jar=None,
                        jar_error=None, state_path=None):
    """Call the service with every outside edge scripted.

    ``state_path`` is whatever ``_auth_state_path()`` should return -- a real
    file from :func:`_write_state`, or a stub like
    :class:`_UnreadableStateFile`. Left unset, the autouse fixture's temp path
    applies, which reads as "no exported state".
    """
    from naukri_server.services import session_service

    provider = MagicMock()
    provider.cached_token = AsyncMock(return_value=token)

    if jar_error is not None:
        def read_jar(profile, names):
            raise session_service.cookie_jar.CookieJarUnavailableError(jar_error)
    else:
        read_jar = _jar(**(jar or {}))

    login = AsyncMock(return_value=status or _status(False, True, "no_token"))
    with ExitStack() as stack:
        stack.enter_context(patch(SERVICE + ".browser_provider", provider))
        stack.enter_context(patch(SERVICE + ".get_login_status", login))
        stack.enter_context(patch(SERVICE + "._read_jar_rows", read_jar))
        if state_path is not None:
            stack.enter_context(
                patch(SERVICE + "._auth_state_path", lambda: state_path))
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
        """No cached token, no exported state and no jar row: nothing on this
        machine knows when the credential lapses, and ``False`` there would
        read as 'checked, still good'."""
        result, _ = await _session_info(verify_live=False, token=None, jar={})

        cred = result["credential"]
        assert cred["expires_at"] is None
        assert cred["expires_in_days"] is None
        assert cred["expired"] is None
        assert cred["expiry_source"].startswith("unknown:")
        # All three sources named, so a reader can tell which one to fix.
        assert "no cached nauk_at" in cred["expiry_source"]
        assert "auth_state.json" in cred["expiry_source"]
        assert "cookie jar" in cred["expiry_source"]

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

    async def test_presence_is_null_only_when_NO_store_could_be_read(self):
        """"Not there" and "could not look" are different facts -- but the
        null is earned only when NEITHER on-disk store answered. Here the jar
        is locked AND auth_state.json will not open, which is the one state in
        which nothing on this machine can say whether a credential exists."""
        result, _ = await _session_info(
            verify_live=False, token=None, jar_error="jar is a directory",
            state_path=_UnreadableStateFile())

        cred = result["credential"]
        assert cred["present"] is None
        assert cred["expired"] is None
        assert "could not be read" in cred["expiry_source"]
        assert "auth_state.json" in cred["why_unknown"]
        assert "cookie jar" in cred["why_unknown"]
        for entry in result["supporting"]:
            assert entry["present"] is None, entry
            assert entry["expired"] is None, entry

    async def test_one_readable_store_saying_nothing_is_a_real_false(
            self, tmp_path):
        """The other side of the same rule, and the reason the null above is
        not just a way of never committing: auth_state.json READ fine and
        holds no token, so absence is measured and ``present`` is a bool even
        though the jar is locked."""
        result, _ = await _session_info(
            verify_live=False, token=None, jar_error="[WinError 32] locked",
            state_path=_write_state(tmp_path, token=None))

        assert result["credential"]["present"] is False
        assert result["credential"]["format"] == "absent"


# =====================================================================
# 2b. auth_state.json -- the source that answers when the jar cannot
# =====================================================================

class TestTheExportedStateIsAnExpirySource:
    """MEASURED 2026-08-23 08:20 local, and the reason this source exists.

    Asked from a process with no browser of its own while the operator's
    Chrome was running, the cached-token / jar chain returned ``present:
    null`` and ``expired: null``: the jar could not even be COPIED, because
    Chrome held it (``[WinError 32] ... used by another process``). That is
    the outage question going unanswered. ``auth_state.json`` was readable
    throughout and said 0.023 days left. It also DISAGREED with the jar in the
    right direction -- jar row 0.3 days PAST, exported token 0.02 days FUTURE
    -- because the live browser had not flushed.
    """

    async def test_a_locked_jar_still_yields_a_real_date_and_a_bool(
            self, tmp_path):
        """THE BOUNCE. The exact live state, reproduced: no cached token, jar
        unreadable, auth_state.json present. Both fields must be real."""
        soon = time.time() + 1800
        result, _ = await _session_info(
            verify_live=False, token=None,
            jar_error="[WinError 32] The process cannot access the file "
                      "because it is being used by another process",
            state_path=_write_state(tmp_path, token=_jwt(exp=soon)))

        cred = result["credential"]
        assert cred["present"] is True, "a bool, not a null"
        assert cred["expires_at"] is not None
        assert cred["expired"] is False
        assert "auth_state.json" in cred["expiry_source"]
        assert "FRESHER" in cred["expiry_source"]

    async def test_it_beats_the_jar_when_the_two_disagree(self, tmp_path):
        """The measured disagreement, as a test: the jar row says the
        credential lapsed hours ago, the export says it is still good. The
        export is what the server actually holds, so it wins.

        Twelve hours rather than the measured thirty minutes, because
        ``expires_in_days`` is ``round(seconds / 86400, 1)`` per the contract
        and a sub-hour horizon renders as ``0.0`` -- see
        ``test_a_sub_hour_horizon_still_rounds_to_zero_days`` for that, pinned
        separately rather than hidden here.
        """
        result, _ = await _session_info(
            verify_live=False, token=None,
            jar={"nauk_at": time.time() - 8 * 3600},
            state_path=_write_state(
                tmp_path, token=_jwt(exp=time.time() + 12 * 3600)))

        assert result["credential"]["expired"] is False
        assert result["credential"]["expires_in_days"] == 0.5
        assert "auth_state.json" in result["credential"]["expiry_source"]

    async def test_a_sub_hour_horizon_still_rounds_to_zero_days(self, tmp_path):
        """The operator's REAL state at 08:20 on 2026-08-23: 0.023 days left.
        The contract fixes the formula at round(seconds / 86400, 1), so that
        renders as 0.0 -- which reads like 'no time information' but is not.
        ``expired: false`` and ``expires_at`` carry the signal. Pinned so the
        rounding is a known property rather than a surprise in an incident."""
        result, _ = await _session_info(
            verify_live=False, token=None, jar={},
            state_path=_write_state(
                tmp_path, token=_jwt(exp=time.time() + 1800)))

        cred = result["credential"]
        assert cred["expires_in_days"] == 0.0
        assert cred["expired"] is False, "0.0 days is not expired"
        assert cred["expires_at"] is not None

    async def test_the_cached_token_still_beats_it(self, tmp_path):
        """Precedence, not preference. The in-process token is the copy tools
        actually send, so it stays first."""
        from naukri_server.services.session_service import SOURCE_CACHED

        result, _ = await _session_info(
            verify_live=False, token=_jwt(exp=time.time() + 7200),
            state_path=_write_state(tmp_path, token=_jwt(exp=time.time() + 1800)))

        assert result["credential"]["expiry_source"] == SOURCE_CACHED
        assert result["credential"]["expires_in_days"] > 0.06

    async def test_it_falls_through_to_the_jar_when_there_is_no_export(self):
        result, _ = await _session_info(
            verify_live=False, token=None,
            jar={"nauk_at": time.time() + 3 * 86400})

        assert "cookie jar" in result["credential"]["expiry_source"]
        assert result["credential"]["expires_in_days"] > 2

    async def test_a_token_with_no_exp_claim_is_present_but_undated(
            self, tmp_path):
        """Presence and expiry are separate facts and must not be merged: the
        export proves a credential exists even when its date will not decode."""
        result, _ = await _session_info(
            verify_live=False, token=None, jar={},
            state_path=_write_state(tmp_path, token=_jwt(exp=None)))

        assert result["credential"]["present"] is True
        assert result["credential"]["expired"] is None
        assert "no readable exp claim" in result["credential"]["expiry_source"]

    async def test_credential_source_names_the_export_when_it_contributed(
            self, tmp_path):
        result, _ = await _session_info(
            verify_live=False, token=None, jar={},
            state_path=_write_state(tmp_path, token=_jwt(exp=time.time() + 60)))

        assert "auth_state.json" in result["credential_source"]
        assert "No browser was launched" in result["credential_source"]


class TestTheExportedStateReader:
    """The reader itself, including every way it declines to answer."""

    def _read(self, path):
        from naukri_server.services import session_service
        with patch(SERVICE + "._auth_state_path", lambda: path):
            return session_service._read_exported_state()

    def test_a_real_export_yields_exp_and_present(self, tmp_path):
        when = 1800000000
        exp, has, err = self._read(_write_state(tmp_path, token=_jwt(exp=when)))

        assert exp == float(when)
        assert has is True
        assert err is None

    def test_a_missing_file_is_a_readable_absence_not_an_error(self, tmp_path):
        """``(None, False, None)``: we looked and there is nothing. That is
        what lets ``present`` be a real False rather than a null."""
        assert self._read(tmp_path / "not-written.json") == (None, False, None)

    def test_a_file_with_no_token_key_is_also_a_readable_absence(self, tmp_path):
        assert self._read(_write_state(tmp_path, token=None)) == (
            None, False, None)

    def test_an_unreadable_file_is_an_ERROR_not_an_absence(self, tmp_path):
        """The distinction the whole null rule rests on. A locked file must
        never read as "no credential"."""
        exp, has, err = self._read(_UnreadableStateFile())

        assert exp is None
        assert has is None, "None, not False -- we could not look"
        assert "PermissionError" in err

    def test_malformed_json_never_quotes_the_document(self, tmp_path):
        """json's own exception embeds what it choked on, and what it choked
        on is a working login plus the entire cookie header."""
        path = tmp_path / "auth_state.json"
        path.write_text('{"token": "%s", broken' % TOKEN_MARKER,
                        encoding="utf-8")

        exp, has, err = self._read(path)

        assert (exp, has) == (None, None)
        assert TOKEN_MARKER not in err
        assert err == "auth_state.json is present but is not valid JSON"

    def test_a_json_array_is_rejected_without_quoting_it(self, tmp_path):
        path = tmp_path / "auth_state.json"
        path.write_text(json.dumps([TOKEN_MARKER]), encoding="utf-8")

        exp, has, err = self._read(path)

        assert (exp, has) == (None, None)
        assert TOKEN_MARKER not in err

    def test_the_cookie_header_is_never_touched(self, tmp_path):
        """The file holds the whole Cookie header beside the token. Only a
        float and a bool come back, so neither can escape."""
        token = _jwt(exp=1800000000)
        path = _write_state(tmp_path, token=token)
        on_disk = path.read_text(encoding="utf-8")
        assert token in on_disk and "REFRESHCOOKIEVALUE" in on_disk, (
            "not vacuous - the credential really is in the file")

        exp, has, err = self._read(path)

        assert (exp, has, err) == (1800000000.0, True, None)
        assert all(isinstance(v, (float, bool, type(None)))
                   for v in (exp, has, err))


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

    async def test_the_session_lapse_date_tracks_nauk_rt_NOT_nauk_at(self):
        """THE field this wave was about, and the substitution it forbids.

        Measured on the live profile 2026-08-23: nauk_at had 0.02 days left
        and nauk_rt had 188.1. Both true, different questions. A client that
        read credential.expires_at as "when do I sign in again" would see
        minutes on naukri and a year on linkedin, when the real comparison is
        188 days against 364. session_lapses_at is the one that answers it.
        """
        now = time.time()
        result, _ = await _session_info(
            verify_live=False,
            jar={"nauk_at": now - 8 * 3600,          # lapsed hours ago
                 "nauk_rt": now + 188 * 86400,       # six months of headroom
                 "nauk_sid": now + 188 * 86400,
                 "nauk_cs": now + 364 * 86400},
        )
        renewal = result["renewal"]
        rt = [s for s in result["supporting"] if s["name"] == "nauk_rt"][0]

        assert renewal["session_lapses_at"] == rt["expires_at"], (
            "it must be READ from the nauk_rt entry, not recomputed - two "
            "routes to one date is two dates")
        assert 187 < renewal["session_lapses_in_days"] < 189

        # The point, stated as the inequality: months, not the credential's
        # minutes.
        assert result["credential"]["expired"] is True
        assert renewal["session_lapses_at"] != result["credential"]["expires_at"]
        assert (renewal["session_lapses_in_days"]
                > result["credential"]["expires_in_days"] + 180)

        assert "nauk_rt" in renewal["session_lapses_source"]
        assert "NOT nauk_at" in renewal["session_lapses_source"]

    async def test_an_unknowable_nauk_rt_is_null_never_nauk_ats_date(
            self, tmp_path):
        """The forbidden fallback, asserted as an inequality against a REAL
        nauk_at date. A null is easy to get right by accident; silently
        reaching for the credential's date when nauk_rt is missing is the bug,
        and it would leave a plausible-looking date sitting in the field."""
        result, _ = await _session_info(
            verify_live=False, token=None,
            jar_error="[WinError 32] locked by Chrome",
            state_path=_write_state(
                tmp_path, token=_jwt(exp=time.time() + 1800)))
        renewal = result["renewal"]

        assert result["credential"]["expires_at"] is not None, (
            "not vacuous - a nauk_at date WAS available to wrongly fall back on")
        assert renewal["session_lapses_at"] is None
        assert renewal["session_lapses_in_days"] is None
        assert renewal["session_lapses_at"] != result["credential"]["expires_at"]
        assert renewal["session_lapses_source"].startswith("unknown:")
        assert "locked by Chrome" in renewal["session_lapses_source"]

    async def test_a_profile_with_no_nauk_rt_row_says_so_specifically(self):
        """Three ways to have no date, and they are not the same fix: jar
        unreadable, no row, or a session cookie. Each gets its own words."""
        result, _ = await _session_info(verify_live=False, jar={})

        source = result["renewal"]["session_lapses_source"]
        assert result["renewal"]["session_lapses_at"] is None
        assert "holds no nauk_rt row" in source

    async def test_a_session_scoped_nauk_rt_is_null_and_says_why(self):
        result, _ = await _session_info(
            verify_live=False, jar={"nauk_rt": -1.0})

        source = result["renewal"]["session_lapses_source"]
        assert result["renewal"]["session_lapses_at"] is None
        assert "SESSION cookie" in source

    async def test_silent_renew_stays_available_and_named(self):
        result, _ = await _session_info(
            verify_live=False, jar={"nauk_rt": time.time() + 188 * 86400})

        assert result["renewal"]["silent_renew_available"] is True
        assert result["renewal"]["tool"] == "naukri_reauth"

    async def test_renewal_discloses_that_it_drives_a_browser(self):
        """"Silent renew available" reads as "free" and it is not. This wave
        exists because something expensive happened and nothing said so, and a
        renew that quietly launches a browser would be that defect again."""
        result, _ = await _session_info(verify_live=False)
        renewal = result["renewal"]

        assert renewal["uses_browser"] is True
        assert renewal["mechanism"]
        assert "pool" in renewal["mechanism"]
        assert "browser_restart" in renewal["mechanism"]
        # The distinction stated, not left to be inferred.
        assert "does not mean free" in renewal["mechanism"]
        assert "NO SIGN-IN AND NO PASSWORD" in renewal["mechanism"]

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
        # PRIMARY, and the only one of these three that can fire on the runner
        # that gates a merge. CI is ubuntu-latest, where CHROME_PROFILE is
        # /home/runner/work/naukri/naukri/chrome-profile -- no colon, no
        # backslash -- so the line below passes there without being able to see
        # a total leak. `stored != CHROME_PROFILE` only catches a leak that is
        # EXACTLY the path and misses "profile at <path>", which is the shape a
        # message would actually take.
        assert CHROME_PROFILE not in stored, (
            "the absolute profile path survived into durability.stored_in: %r"
            % stored)
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
    """The walker hunts the TOKEN STRING, not a plaintext marker.

    It hunted the marker first, and that check could not fail: ``_jwt`` puts
    the marker inside a base64url payload, so the literal
    ``SUPERSECRETNAUKATBODY`` never appears in the token and a result echoing
    the whole credential would have passed clean. Caught 2026-08-23 by the
    not-vacuous assertion in ``test_the_cookie_header_is_never_touched`` going
    red for the same reason. Needles are now the token itself plus each of its
    three segments, so a PARTIAL leak fails too, and
    ``test_CONTROL_the_walker_catches_a_planted_token`` proves it fires.
    """

    @pytest.mark.parametrize("verify_live", [True, False])
    async def test_session_info_never_returns_the_token(self, verify_live):
        token = _jwt(exp=time.time() + 3600)
        result, _ = await _session_info(
            verify_live=verify_live, token=token,
            status=_status(True, True, "api_confirmed"),
            jar={"nauk_at": time.time() + 3600})

        assert _find_leak(result, _token_needles(token)) == []
        # The expiry DID come through, so this is not vacuous.
        assert result["credential"]["expires_at"] is not None

    def test_CONTROL_the_walker_catches_a_planted_token(self):
        """A leak walker that has never been shown catching one certifies
        nothing -- and this one genuinely did not, until today."""
        token = _jwt(exp=1800000000)
        needles = _token_needles(token)

        assert _find_leak({"credential": {"value": token}}, needles)
        assert _find_leak([{"a": ["x", token.split(".")[1]]}], needles)
        assert _find_leak({token: "as a KEY"}, needles)
        assert _find_leak({"clean": "no token here"}, needles) == []

    def test_CONTROL_the_walker_is_quiet_on_a_foreign_token(self):
        """The OTHER direction, and the one that was broken until today.

        Catching every planted leak is half an instrument. A walker that also
        fires on things that are NOT this credential cannot tell the two apart,
        and "it went red" stops being information. Measured before the fix: a
        token carrying a completely different secret was reported as a leak of
        this one, because `_token_needles` included the public JWT header and
        a hardcoded signature that every token in this module shared.

        Asserted as an explicit comparison so the trap is visible: the two
        tokens really DO share the public header, and that sharing must not be
        enough to trip the walker.
        """
        mine = _jwt(exp=1800000000, marker="MINE_" + TOKEN_MARKER)
        foreign = _jwt(exp=1900000000, marker="A_DIFFERENT_SECRET_ENTIRELY")
        needles = _token_needles(mine)

        assert mine != foreign
        assert mine.split(".")[0] == foreign.split(".")[0] == JWT_PUBLIC_HEADER, (
            "if these no longer share a header this control tests nothing")
        assert JWT_PUBLIC_HEADER not in needles, (
            "the public header is back in the needle set")

        assert _find_leak({"other": foreign}, needles) == [], (
            "a foreign token was reported as a leak of ours")
        assert _find_leak({"h": JWT_PUBLIC_HEADER}, needles) == [], (
            "the bare public header was reported as a leak")
        # ...and the walker has NOT been silenced: our own token still trips it.
        assert _find_leak({"ours": mine}, needles)
        assert _find_leak({"payload_only": mine.split(".")[1]}, needles)
        assert _find_leak({"sig_only": mine.split(".")[2]}, needles)

    async def test_session_info_never_logs_the_token(self, caplog):
        token = _jwt(exp=time.time() + 3600)
        with caplog.at_level(logging.DEBUG):
            await _session_info(token=token,
                                status=_status(True, True, "api_confirmed"))

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert _find_leak(blob, _token_needles(token)) == []

    async def test_an_undecodable_token_does_not_leak_through_the_error_path(self):
        """The likeliest leak: a decoder that reports what it choked on. This
        one has no such branch, and the assertion is against a token that
        cannot be decoded at all."""
        broken = "%s.@@not-base64@@.%s" % (_b64({"alg": "none"}), TOKEN_MARKER)
        result, _ = await _session_info(verify_live=False, token=broken)

        assert _find_leak(result, [broken, TOKEN_MARKER]) == []

    async def test_the_exported_token_never_reaches_a_field(self, tmp_path):
        """The new source reads a file holding both the token AND the whole
        cookie header. Only a date may come out."""
        token = _jwt(exp=time.time() + 1800)
        result, _ = await _session_info(
            verify_live=False, token=None, jar={},
            state_path=_write_state(tmp_path, token=token))

        leaked = _find_leak(
            result, _token_needles(token) + ["REFRESHCOOKIEVALUE"])
        assert leaked == [], "the exported token reached a field: %s" % leaked
        # Not vacuous: the date really did come through this source.
        assert result["credential"]["expires_at"] is not None
        assert "auth_state.json" in result["credential"]["expiry_source"]

    async def test_the_exported_token_never_reaches_a_log(self, tmp_path, caplog):
        token = _jwt(exp=time.time() + 1800)
        with caplog.at_level(logging.DEBUG):
            await _session_info(
                verify_live=False, token=None, jar={},
                state_path=_write_state(tmp_path, token=token))

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert _find_leak(
            blob, _token_needles(token) + ["REFRESHCOOKIEVALUE"]) == []

    async def test_reauth_never_returns_the_token(self):
        token = _jwt(exp=time.time() + 3600)
        result = await _reauth(mints=token,
                               status=_status(True, True, "api_confirmed"))

        assert _find_leak(result, _token_needles(token)) == []
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

    async def test_a_partial_clear_reports_authenticated_NULL(self):
        """The other direction of the same lie. A logout that could not unlink
        auth_state.json used to answer ``authenticated: false`` in the same
        object as a ``not_removed`` line admitting a live credential was still
        sitting in that file -- readable by exactly the cross-process agents
        ``scope`` names. An absence claimed from a clear that did not happen.

        ``is None`` and not a falsy check, deliberately: ``False`` passes
        ``not result["authenticated"]``, and ``False`` is the bug.
        """
        result, _ = await _logout(state_file=_LockedStateFile())

        assert result["authenticated"] is None
        assert result["authenticated"] is not False
        assert "not_removed" in result
        assert "PARTIAL" in result["reason"]
        assert "treated as still present" in result["reason"]

    async def test_a_full_clear_still_reports_false(self, tmp_path):
        """CONTROL for the rule above. The null must be earned by a real
        partial, or 'authenticated' becomes a field that never commits."""
        state = tmp_path / "auth_state.json"
        state.write_text("{}", encoding="utf-8")

        result, _ = await _logout(state_file=state)

        assert result["authenticated"] is False
        assert "not_removed" not in result

    async def test_nothing_to_clear_still_reports_false(self, tmp_path):
        """The nothing-was-there case is a PROVEN absence, not a partial: no
        credential means no authenticated request, established without asking
        Naukri."""
        result, _ = await _logout(
            token=None, state_file=tmp_path / "never-written.json")

        assert result["cleared"] is False
        assert result["authenticated"] is False
        assert "not_removed" not in result

    async def test_it_never_raises_when_the_manager_explodes(self, tmp_path):
        manager = MagicMock()
        manager._token = "cached"
        manager.invalidate.side_effect = RuntimeError("browser is gone")

        result, _ = await _logout(manager=manager,
                                  state_file=tmp_path / "gone.json")

        assert result["authenticated"] is None, (
            "the cached token could not be invalidated, so nothing about the "
            "credential is proven")
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

    @pytest.mark.parametrize("state_file,expected", [
        ("clean", False),
        ("locked", None),
    ])
    async def test_authenticated_is_false_only_where_it_is_provable(
            self, tmp_path, state_file, expected):
        """Both branches side by side, so neither can drift alone."""
        target = (_LockedStateFile() if state_file == "locked"
                  else tmp_path / "auth_state.json")
        if state_file == "clean":
            target.write_text("{}", encoding="utf-8")

        result, _ = await _logout(state_file=target)

        assert result["authenticated"] is expected


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
                    "uses_browser", "mechanism", "checked_against", "reason",
                    "credential"):
            assert key in result, key
        assert isinstance(result["renewed"], bool)
        assert result["credential"]["name"] == "nauk_at"

    @pytest.mark.parametrize("kwargs,stage,marker", [
        ({"mints": "fresh"}, "cache_refresh", "borrowed from the existing pool"),
        ({"mints": None, "restart_mints": "fresh"}, "browser_restart",
         "stopped and relaunched"),
        ({"raises": RuntimeError("pool dead"),
          "restart_raises": RuntimeError("profile locked")}, "none",
         "NO stage succeeded, and both costs were still paid"),
    ])
    async def test_mechanism_names_the_stage_that_actually_ran(
            self, kwargs, stage, marker):
        """Including the failure branch, which is the one that matters: a
        reauth where neither stage produced a token still paid for a pooled
        navigation AND a full browser restart. Reporting only the successful
        cost would understate exactly the case worth seeing."""
        result = await _reauth(status=_status(True, True, "api_confirmed"),
                               **kwargs)

        assert result["stage"] == stage
        assert result["uses_browser"] is True
        assert marker in result["mechanism"], result["mechanism"]
        assert "does not mean free" in result["mechanism"]


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
        for key in ("silent_renew_available", "tool", "uses_browser",
                    "mechanism", "session_lapses_at", "session_lapses_in_days",
                    "session_lapses_source"):
            assert key in result["renewal"], key
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
    something to leak.

    THE SECRET GOES IN BOTH VALUE COLUMNS. `encrypted_value` used to hold the
    literal b"sealed", which meant `JAR_SECRET not in json.dumps(rows)` was
    blind to a leak out of `encrypted_value` -- and on a real Chrome profile
    that is the column the credential actually lives in, because `value` is
    empty whenever the OS keyring is in use. The needle could only see the
    column a live jar leaves blank.
    """
    jar = profile_dir / "Default" / "Network" / "Cookies"
    jar.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(jar))
    con.execute(
        "CREATE TABLE cookies (name TEXT, host_key TEXT, value TEXT, "
        "encrypted_value BLOB, expires_utc INTEGER, has_expires INTEGER, "
        "is_persistent INTEGER)")
    con.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(n, h, JAR_SECRET, JAR_SECRET.encode(), e, he, ip)
         for n, h, e, he, ip in rows])
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

    def test_CONTROL_the_value_assertions_can_actually_fail(self):
        """The five jar assertions guard a WORKING Naukri login, and until
        today not one of them had ever been watched failing.

        What this control can and cannot do is worth being exact about. The
        two result-shape assertions are shown firing on the record a
        value-selecting reader WOULD hand back, and the two query assertions on
        the queries that would produce it. That is a planted leak, not an
        end-to-end one -- see `test_a_wildcard_query_cannot_leak_it_crashes`
        below for why an end-to-end version is not constructible.
        """
        # The shape `read_jar` would return if somebody added a value column
        # and passed it through -- exactly the regression these guard.
        leaky = [{"name": "nauk_at", "expires": 1.0, "value": JAR_SECRET}]
        assert JAR_SECRET in json.dumps(leaky), (
            "the needle cannot see a planted value - it is measuring nothing")
        assert not all(set(r) == {"name", "expires"} for r in leaky)

        # ...and quiet on the real shape, which is the other half of a control.
        clean = [{"name": "nauk_at", "expires": 1.0}]
        assert JAR_SECRET not in json.dumps(clean)
        assert all(set(r) == {"name", "expires"} for r in clean)

        # The query guards, shown firing on the two queries that would leak.
        assert "*" in "SELECT * FROM cookies"
        assert "value" in "SELECT name, value FROM cookies".lower()
        # Both forms of the encrypted column trip the same guard, which is why
        # it tests `"value" in`, not `== "value"`.
        assert "value" in "SELECT name, encrypted_value FROM cookies".lower()

    def test_a_wildcard_query_cannot_leak_it_crashes(self, tmp_path):
        """An accidental structural defence, pinned so a refactor keeps it.

        `read_jar` unpacks each row into exactly five names. So `SELECT *`,
        the obvious way this could go wrong, does not return the secret -- it
        raises before any record is built. That is a real property and the
        reason the wildcard guard at the query is a belt-and-braces check
        rather than the only thing standing between the jar and a leak.

        If a future refactor unpacks by index or by column name, this test
        fails and says: the wildcard guard is now load-bearing on its own.
        """
        from naukri_server import cookie_jar

        _build_jar(tmp_path, [
            ("nauk_at", ".naukri.com", (1800000000 + WEBKIT) * 1000000, 1, 1)])

        with patch.object(cookie_jar, "_JAR_QUERY", "SELECT * FROM cookies"):
            with pytest.raises(ValueError):
                cookie_jar.read_jar(tmp_path, ["nauk_at"])

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


class TestTheRenewalBlockCannotContradictItself:
    """`silent_renew_available` was a hardcoded `True` until 2026-08-25.

    The block's own docstring says a payload that contradicts itself about when
    the session dies is worse than one that says it does not know. That rule was
    obeyed by `session_lapses_at` and broken by the field two lines above it:
    when nauk_rt is unreadable or session-scoped, the payload said "no refresh
    date could be found" AND "silent renew is available" at the same time.

    Three sibling servers are copying this block, so the honesty has to live in
    the value rather than in the prose around it.
    """

    async def test_no_refresh_row_means_renew_is_NOT_advertised(self):
        result, _ = await _session_info(verify_live=False, jar={})
        renewal = result["renewal"]
        assert renewal["session_lapses_at"] is None
        assert renewal["silent_renew_available"] is False, (
            "claimed silent renew with no nauk_rt to renew from"
        )

    async def test_a_session_scoped_refresh_cookie_means_the_same(self):
        """A nauk_rt with no expiry dies with the browser session.

        It is present, so a presence check would pass it -- and it still cannot
        support a silent renew tomorrow. This is why the field keys off the
        DATE rather than off the row existing.
        """
        result, _ = await _session_info(verify_live=False, jar={"nauk_rt": -1.0})
        renewal = result["renewal"]
        assert renewal["session_lapses_at"] is None
        assert renewal["silent_renew_available"] is False
        assert "SESSION cookie" in renewal["session_lapses_source"]

    async def test_a_dated_refresh_cookie_still_advertises_renew(self):
        """The negative cases must not have cost the positive one."""
        result, _ = await _session_info(
            verify_live=False, jar={"nauk_rt": time.time() + 188 * 86400})
        renewal = result["renewal"]
        assert renewal["session_lapses_at"] is not None
        assert renewal["silent_renew_available"] is True
        assert renewal["tool"] == "naukri_reauth"

    async def test_the_two_fields_agree_in_every_case(self):
        """The invariant, stated once rather than implied three times."""
        for jar in ({}, {"nauk_rt": -1.0},
                    {"nauk_rt": time.time() + 188 * 86400}):
            result, _ = await _session_info(verify_live=False, jar=jar)
            renewal = result["renewal"]
            assert renewal["silent_renew_available"] is (
                renewal["session_lapses_at"] is not None), (
                "renewal block contradicts itself for jar=%r" % (jar,)
            )


class TestAuthoritativeMeansAuthoritativeAboutSomething:
    """`expiry_is_authoritative` was a bare `True` beside `format: "absent"`.

    True means "the exp inside this JWT is the real lifetime, Naukri does not
    revoke ahead of it". With no JWT there is nothing for it to be true ABOUT,
    and printing it next to an absent credential invites a consumer to trust a
    date that does not exist.
    """

    async def test_absent_credential_makes_the_claim_None(self):
        result, _ = await _session_info(verify_live=False, token=None, jar={})
        cred = result["credential"]
        if cred["present"] is False:
            assert cred["format"] == "absent"
            assert cred["expiry_is_authoritative"] is None, (
                "asserted an authoritative expiry with no credential"
            )

    async def test_a_present_credential_keeps_the_claim(self):
        result, _ = await _session_info(
            verify_live=False, token=_jwt(time.time() + 3600))
        cred = result["credential"]
        assert cred["present"] is True
        assert cred["expiry_is_authoritative"] is True


class TestWhatAStrangerSeesOnFirstRun:
    """These repos are public. Whoever clones one is not the person who built it.

    A fresh clone has no Chrome profile and no session, so the first
    authenticated tool a stranger touches is the whole first-run experience.
    MEASURED before this class existed: a signed-out `naukri_get_profile`
    returned `error_code: "API_ERROR"` -- not even classified as auth -- with
    the exception class printed twice and NO tool named. Nothing in that reply
    told a stranger what to do next.

    The same 401 was also classified two ways depending on which dispatch path
    a tool used: `api_tool` said AUTH_ERROR, `handle_tool_action` said
    API_ERROR. A caller cannot act on a code that depends on an implementation
    detail of the tool it called.
    """

    def test_401_classifies_as_auth_and_names_the_next_step(self):
        from naukri_server.error_handler import classify_api_error
        from naukri_server.api import NaukriAPIError

        code, message = classify_api_error(NaukriAPIError(401, "Unauthorized"))
        assert code == "AUTH_ERROR"
        assert "naukri_login" in message
        assert "naukri_auth_status" in message

    def test_403_is_auth_too_and_not_a_permissions_dead_end(self):
        """Naukri answers 403 for a lapsed session on some routes.

        Reading it as a permissions problem sends the caller hunting for an
        entitlement they already have.
        """
        from naukri_server.error_handler import classify_api_error
        from naukri_server.api import NaukriAPIError

        code, message = classify_api_error(NaukriAPIError(403, "Forbidden"))
        assert code == "AUTH_ERROR"
        assert "naukri_login" in message

    def test_a_real_server_error_is_NOT_dressed_up_as_auth(self):
        """The negative control. A classifier that calls everything AUTH_ERROR
        is as useless as one that calls nothing."""
        from naukri_server.error_handler import classify_api_error
        from naukri_server.api import NaukriAPIError

        code, message = classify_api_error(NaukriAPIError(500, "Server error"))
        assert code == "API_ERROR"
        assert "naukri_login" not in message

    def test_the_next_step_mentions_silent_renewal_for_a_returning_user(self):
        """A stranger needs naukri_login; someone whose session lapsed does not.

        Naming only login would send a returning user through a sign-in form
        they do not need -- reauth renews without one.
        """
        from naukri_server.error_handler import _SIGNED_OUT_NEXT_STEP
        assert "naukri_reauth" in _SIGNED_OUT_NEXT_STEP

    def test_the_message_does_not_print_the_status_twice(self):
        from naukri_server.error_handler import classify_api_error
        from naukri_server.api import NaukriAPIError

        _, message = classify_api_error(NaukriAPIError(401, "Unauthorized"))
        assert message.count("401") == 1, message

    async def test_a_signed_out_profile_call_is_actionable_end_to_end(self):
        """Through the real tool, not just the classifier."""
        from unittest.mock import patch as _patch
        from naukri_server.interfaces import api_client
        from naukri_server.api import NaukriAPIError
        from naukri_server.tools import profile as P

        async def unauthorised(*a, **k):
            raise NaukriAPIError(401, "Unauthorized")

        fn = getattr(P.naukri_get_profile, "fn", P.naukri_get_profile)
        with _patch.object(api_client, "get",
                           new=AsyncMock(side_effect=unauthorised)):
            result = await fn()

        assert result["error_code"] == "AUTH_ERROR"
        assert "naukri_login" in result["message"]
        # and the double-print is gone
        assert "NaukriAPIError: NaukriAPIError" not in result["message"]
