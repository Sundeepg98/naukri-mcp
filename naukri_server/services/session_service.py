"""The auth LIFECYCLE, as facts rather than as a guess.

Implements the three tools of the 2026-08-23 auth contract
(``_audit/2026-08-23-auth-contract.md``, generalised from
``linkedin_session_info``): ``session_info``, ``logout``, ``reauth``.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE. ``authenticated`` comes from a
REAL AUTHENTICATED REQUEST and from nowhere else. Not from a cookie being in
the jar, not from a token being in the cache, not from the profile directory
existing. Where the live check could not run, ``authenticated`` is ``None``
WITH A REASON -- never ``False``, because ``False`` means Naukri said no, and
"I could not ask" and "the answer was no" send the operator to two different
places. ``expired`` follows the same rule: ``True`` only when a KNOWABLE
expiry is in the past, ``None`` when the expiry is not knowable from here.

WHY IT IS WORTH A MODULE. On 2026-08-22 the operator lost 8.5 hours to a
Naukri session that looked fine from every angle a presence check can see: the
profile was there, the cookies were there, ``nauk_at`` was there. What had
happened is that ``nauk_at`` had lapsed -- the jar row measured 0.3 days PAST
on 2026-08-23 -- while ``nauk_rt``, the refresh cookie sitting in the same
profile, still had 188 days on it. A server restart re-minted a fresh
``nauk_at`` from that refresh cookie and the outage evaporated with no
password typed. The single field that would have shortened those 8.5 hours to
minutes is ``supporting[nauk_rt]``: it is what makes silent renewal possible
and it says how long that route has left.

A CREDENTIAL VALUE NEVER LEAVES HERE. Name, presence, format and expiry only
-- no value, no length, no prefix, and nothing in an error message either.
:func:`jwt_exp` is the enforcement point for the token itself (it has exactly
two return shapes, ``float`` and ``None``), and ``cookie_jar`` is the
enforcement point for the jar (it selects metadata columns by name and copies
the file before opening it).
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional

from naukri_server import cookie_jar
from naukri_server.cookie_jar import read_jar as _read_jar_rows
from naukri_server.config import ACTIVITY_LEVEL_API, CHROME_PROFILE, logger
from naukri_server.interfaces import browser_provider
from naukri_server.policy import display_path
from naukri_server.services.auth_service import get_login_status

__all__ = [
    "SERVER",
    "CREDENTIAL_COOKIE",
    "SUPPORTING_COOKIES",
    "AUTH_ENDPOINT",
    "jwt_exp",
    "session_info",
    "logout",
    "reauth",
]

SERVER = "naukri"

#: THE cookie that authenticates a REST call. Short-lived, and a JWT, so its
#: own ``exp`` claim is the most precise expiry available anywhere.
CREDENTIAL_COOKIE = "nauk_at"

#: Everything else that governs RECOVERY, with the role each one plays.
#: ``nauk_rt`` is first because it is the one that matters: it is why
#: ``naukri_reauth`` can exist at all.
SUPPORTING_COOKIES = (
    ("nauk_rt", "refresh"),
    ("nauk_sid", "session"),
    ("nauk_cs", "csrf"),
)

#: The endpoint whose answer IS the verdict. Named in every result so a reader
#: can see what was asked, rather than taking "authenticated" on trust.
AUTH_ENDPOINT = "GET %s" % ACTIVITY_LEVEL_API

#: Said wherever a null verdict is reported, in the same words, because it is
#: the sentence this whole module exists to enforce.
A_COOKIE_IS_NOT_A_SESSION = (
    "a cookie in the jar is NOT a session. nauk_at being present and unexpired "
    "means the login has not lapsed on its own; it does not mean Naukri still "
    "honours it. Only the live call establishes that."
)

_RECOVER_BY = (
    "naukri_reauth first -- it is silent, needs no password and renews from "
    "nauk_rt in the profile. Only if that reports renewed=false, naukri_login."
)


# ---------------------------------------------------------------------------
# The token decoder -- the narrowest surface in this file, on purpose
# ---------------------------------------------------------------------------

def jwt_exp(token: Any) -> Optional[float]:
    """Return a JWT's ``exp`` claim as POSIX seconds, or ``None``.

    THE RETURN TYPE IS THE CONTAINMENT. This function is handed the operator's
    live Naukri credential, and it has exactly two return shapes: a ``float``
    built by ``float(exp)``, or ``None``. There is no branch that can return
    the token, a slice of it, its length, or any string derived from it, and
    every failure -- wrong segment count, bad base64, non-JSON payload,
    missing claim, a claim that is not a number -- lands on the same bare
    ``return None``. The exception is swallowed rather than logged or
    formatted, because a decoder's exception text quotes the input it choked
    on, and the input here is a working login.

    ``bool`` is rejected explicitly: it is a subclass of ``int`` in Python, so
    ``{"exp": true}`` would otherwise decode to the year 1970 and be reported
    as a decade-old expiry rather than as unknowable.

    No signature check and no expiry check. This is a READER of a claim the
    server itself minted and cached; verifying Naukri's signature would need
    Naukri's key, and "is it expired" is the caller's question to ask of the
    number, not this function's to answer.
    """
    try:
        parts = str(token).split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        if not isinstance(claims, dict):
            return None
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        value = float(exp)
        # NaN and the infinities all survive float(); none of them is a date,
        # and NaN in particular would make every comparison below False, so a
        # NaN expiry would report as "not expired" forever.
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Expiry, rendered the one way the contract specifies
# ---------------------------------------------------------------------------

def _iso(posix_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(posix_seconds))


def _expiry_facts(expires: Optional[float]) -> dict:
    """Turn one POSIX expiry into the three fields the contract names.

    ``expires`` follows Playwright's convention, which ``cookie_jar`` also
    speaks: seconds since the epoch, or ``-1`` for a cookie that dies with the
    browser. ``None`` means nothing supplied a date at all.

    A session cookie and an unknown date both yield ``expired: None``, and
    they are different facts, so the session-cookie case carries a ``note``
    saying which one it is. What neither yields is ``expired: False`` -- there
    is no date to be on the right side of, and ``False`` there would read as
    "checked, still good".
    """
    if expires is None:
        return {"expires_at": None, "expires_in_days": None, "expired": None}
    if expires <= 0:
        return {
            "expires_at": None,
            "expires_in_days": None,
            "expired": None,
            "note": (
                "a session cookie: it lives only as long as the browser "
                "process does, so it has no date to expire on"
            ),
        }
    remaining = expires - time.time()
    return {
        "expires_at": _iso(expires),
        "expires_in_days": round(remaining / 86400.0, 1),
        "expired": remaining <= 0,
    }


# ---------------------------------------------------------------------------
# The jar, read once per call
# ---------------------------------------------------------------------------

def _read_jar(profile_dir: Any = None) -> tuple:
    """Read every cookie this module reports. Returns ``(by_name, error)``.

    Never raises: a jar that cannot be read is a FACT to report next to the
    others, not a reason to fail the whole tool. ``error`` is the scrubbed
    message when the read failed and ``None`` when it did not; ``by_name`` is
    ``{cookie_name: expires_float}`` and is empty on failure.

    The distinction that matters downstream: an EMPTY ``by_name`` with no
    error means the jar was read and holds none of these cookies (sign in),
    whereas an empty one WITH an error means nothing was measured at all
    (presence is unknown, not false).

    The reader is bound as a module-level name (``_read_jar_rows``) rather
    than reached through ``cookie_jar.read_jar``, so a test can script THIS
    module's jar without patching the shared reader out from under the tests
    that certify the reader itself.
    """
    from naukri_server.utils import scrub_paths

    names = [CREDENTIAL_COOKIE] + [n for n, _role in SUPPORTING_COOKIES]
    try:
        records = _read_jar_rows(profile_dir or CHROME_PROFILE, names)
    except cookie_jar.CookieJarUnavailableError as exc:
        # cookie_jar builds its messages with the path INSIDE the prose, where
        # a field rename cannot reach it, so it is scrubbed here rather than
        # trusted to the result-shape.
        return {}, scrub_paths(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return {}, scrub_paths("%s: %s" % (type(exc).__name__, exc))

    by_name = {}
    for record in records:
        name = record.get("name")
        if name is not None and name not in by_name:
            by_name[name] = record.get("expires")
    return by_name, None


# ---------------------------------------------------------------------------
# The blocks
# ---------------------------------------------------------------------------

def _credential_block(cached_token: Any, jar: dict, jar_error: Optional[str]) -> dict:
    """The ``credential`` block: what authenticates, and when it lapses.

    Expiry has a preference order, and ``expiry_source`` always names which
    one supplied the number:

    1. the ``exp`` claim of the CACHED ``nauk_at``. This is the exact instant
       the server's own copy stops working, and it is the only source that
       describes the credential actually in use.
    2. the profile jar's ``nauk_at`` row. One layer out: it is the browser's
       copy, which is what a restart would pick up.
    3. nothing, in which case the field is ``None`` and ``expiry_source`` says
       WHY rather than going quiet.

    ``present`` is ``None`` rather than ``False`` when the jar could not be
    read and there is no cached token: "no credential" and "could not look"
    are the same distinction as ``authenticated`` false versus null, and they
    earn the same treatment.
    """
    from_cache = jwt_exp(cached_token) if cached_token else None
    from_jar = jar.get(CREDENTIAL_COOKIE)

    if from_cache is not None:
        expires = from_cache
        source = (
            "the exp claim of the nauk_at JWT this server has cached (the "
            "copy tools actually send)"
        )
    elif from_jar is not None:
        expires = from_jar
        source = (
            "the nauk_at row in the Chrome profile cookie jar, read from a "
            "copy without launching a browser"
        )
    else:
        expires = None
        if cached_token:
            source = (
                "unknown: this server holds a cached nauk_at but it carries "
                "no readable exp claim, and "
            )
        else:
            source = "unknown: there is no cached nauk_at, and "
        source += (
            "the profile cookie jar could not be read (%s)" % jar_error
            if jar_error
            else "the profile cookie jar holds no nauk_at row"
        )

    if cached_token or from_jar is not None:
        present: Optional[bool] = True
    elif jar_error:
        present = None
    else:
        present = False

    block = {
        "kind": "cookie",
        "name": CREDENTIAL_COOKIE,
        "present": present,
        "format": "absent" if present is False else "jwt",
        "expiry_source": source,
        # Naukri does not revoke nauk_at ahead of its own exp: the JWT is
        # self-describing and the REST layer's 401 path exists for the case
        # where it is rejected anyway. Nothing here is a guess about a
        # shorter, undocumented server-side lifetime.
        "expiry_is_authoritative": True,
    }
    block.update(_expiry_facts(expires))
    if present is None:
        block["why_unknown"] = (
            "no cached nauk_at, and the jar that would have said whether one "
            "is on disk could not be read: %s" % jar_error
        )
    return block


def _supporting_block(jar: dict, jar_error: Optional[str]) -> list:
    """The ``supporting`` block: what governs RECOVERY.

    ``nauk_rt`` is the reason this whole build exists. It is the refresh
    cookie: navigating with it makes Naukri mint a fresh ``nauk_at``, which is
    exactly what ``TokenManager.refresh_via_pool`` does, and it is why the
    8.5-hour outage of 2026-08-22 was recoverable with no password. Its
    ``expires_in_days`` is how long that route has left, and it is the field
    that would have made that outage a two-minute one.
    """
    out = []
    for name, role in SUPPORTING_COOKIES:
        expires = jar.get(name)
        if expires is not None:
            present: Optional[bool] = True
        elif jar_error:
            present = None
        else:
            present = False
        entry = {"name": name, "role": role, "present": present}
        entry.update(_expiry_facts(expires))
        if present is None:
            entry["why_unknown"] = (
                "the profile cookie jar could not be read: %s" % jar_error
            )
        out.append(entry)
    return out


def _durability() -> dict:
    return {
        "stored_in": display_path(CHROME_PROFILE),
        "survives_server_restart": True,
        "survives_machine_reboot": True,
        "why": (
            "the login lives in an on-disk Chrome profile, not in this "
            "process. The profile keeps the LONG-LIVED cookies -- nauk_rt and "
            "nauk_sid, both measured at +188 days on 2026-08-23 -- so "
            "stopping the server or rebooting the machine costs nothing: the "
            "next start navigates with them and Naukri mints a fresh nauk_at, "
            "with no password typed. What ends it is Naukri revoking those "
            "cookies, a sign-out, or the profile directory being deleted."
        ),
    }


def _renewal() -> dict:
    return {
        "silent_renew_available": True,
        "tool": "naukri_reauth",
        "why": (
            "nauk_rt is Naukri's refresh cookie and it sits in the same "
            "profile as the credential it renews. Navigating with it makes "
            "Naukri issue a fresh nauk_at -- no login page, no password, no "
            "credential typed -- which is what TokenManager.refresh_via_pool "
            "already does on every 401. The nauk_rt entry under 'supporting' "
            "says how long that route has left."
        ),
    }


_ON_EXPIRY = (
    "REST-backed tools return the ordinary error envelope with error_code "
    "AUTH_ERROR naming naukri_login, never an empty result that reads like "
    "'no jobs found'. Recovery in order: " + _RECOVER_BY
)


def _live_check_block(
    *, attempted: bool, authenticated: Optional[bool], why_not: Optional[str]
) -> dict:
    """The block that says whether the verdict above was MEASURED.

    ``completed`` is true exactly when ``authenticated`` is not null, which is
    the contract's definition and not an independently tracked flag -- two
    fields that can disagree is how a null verdict starts reading as a no.
    """
    block = {
        "attempted": attempted,
        "completed": authenticated is not None,
        "endpoint": AUTH_ENDPOINT,
    }
    if authenticated is True:
        block["what_it_means"] = (
            "the activity-level endpoint was asked and answered yes, so "
            "'authenticated' above is a measurement, not an inference from a "
            "cookie being present"
        )
    elif authenticated is False:
        block["what_it_means"] = (
            "the activity-level endpoint was asked and Naukri rejected the "
            "credential. This is a real no: sign in again"
        )
    else:
        block["why_not"] = why_not or "not attempted"
        block["what_it_means"] = (
            "'authenticated' is null because the live call could not be "
            "completed, NOT because Naukri said no. The credential facts "
            "below are the only thing measured here, and "
            + A_COOKIE_IS_NOT_A_SESSION
        )
    return block


def _why_not_from_reason(reason: str) -> str:
    """Turn get_login_status's terse ``reason`` into the contract's prose."""
    return (
        "the live check did not complete (%s), so nothing here is a verdict "
        "from Naukri" % (reason or "no reason given")
    )


# ---------------------------------------------------------------------------
# 1. session_info
# ---------------------------------------------------------------------------

async def session_info(verify_live: bool = True) -> dict:
    """Report the session's state and how long it has left.

    ``verify_live=True`` puts the question to Naukri through
    :func:`services.auth_service.get_login_status`, which is the ONLY source
    of an ``authenticated`` verdict anywhere in this server. Its
    ``{logged_in, verified}`` maps onto the contract exactly once, here:
    ``verified`` false means the check could not run, so ``authenticated`` is
    null with the reason attached; ``verified`` true means ``authenticated``
    is ``logged_in``, no and no only.

    ``verify_live=False`` costs no network and no browser. It reads the
    already-cached token (an in-memory attribute read -- never
    ``extract_token()``, which CLEARS the cache when cookie extraction fails
    and would destroy a good session on a status check) and a COPY of the
    profile's cookie jar. ``authenticated`` is null and says so in the exact
    words the contract specifies.

    Never returns a credential value, in any field, on any path.
    """
    cached = await _cached_token()
    jar, jar_error = _read_jar()

    if verify_live:
        status = await get_login_status()
        verified = bool(status.get("verified"))
        logged_in = bool(status.get("logged_in"))
        authenticated = logged_in if verified else None
        live = _live_check_block(
            attempted=True,
            authenticated=authenticated,
            why_not=_why_not_from_reason(str(status.get("reason", ""))),
        )
    else:
        authenticated = None
        live = _live_check_block(
            attempted=False,
            authenticated=None,
            why_not="not attempted: this call asked for the offline answer",
        )

    return {
        "server": SERVER,
        "authenticated": authenticated,
        "checked_against": AUTH_ENDPOINT,
        "live_check": live,
        "credential": _credential_block(cached, jar, jar_error),
        "supporting": _supporting_block(jar, jar_error),
        "credential_source": (
            "this server's cached nauk_at, plus the profile's on-disk cookie "
            "jar read from a copy without launching a browser"
            if cached
            else "the on-disk store, read without a browser"
        ),
        "durability": _durability(),
        "renewal": _renewal(),
        "on_expiry": _ON_EXPIRY,
    }


async def _cached_token() -> Optional[str]:
    """The NON-MUTATING token read, with its own failure contained.

    ``browser_provider.cached_token()`` is documented never to raise, but this
    is the one call in ``session_info`` that touches the browser layer at all,
    and a status tool that dies because the browser died is the opposite of
    useful. A failure here degrades to "no cached token", which the expiry
    fallback already handles.
    """
    try:
        return await browser_provider.cached_token()
    except Exception as exc:
        logger.warning(
            "session_info: cached token unreadable (%s) - falling back to the "
            "jar", type(exc).__name__,
        )
        return None


# ---------------------------------------------------------------------------
# 2. logout
# ---------------------------------------------------------------------------

async def logout() -> dict:
    """Clear the LOCAL credential. Nothing else, and never a raise.

    Removes two things and only two: the cached ``nauk_at`` in this process
    (``TokenManager.invalidate``) and the exported ``auth_state.json`` that
    cross-process agents read. It does NOT delete the Chrome profile and it
    does NOT sign out on Naukri -- no request is sent to Naukri at all, which
    is why ``authenticated`` false here is a statement about the local store
    and says so rather than posing as a verdict.

    That is also why it is nearly free to run: the profile still holds
    ``nauk_rt``, so ``naukri_reauth`` usually gets straight back in.

    NEVER RAISES, including when the file is locked (Windows refuses to unlink
    a file another process has open) or already gone. A logout that throws
    leaves the caller unable to tell how far it got, which is the worst
    possible state for the one tool whose job is to make the state simple. A
    file that could not be removed is reported under ``not_removed`` rather
    than silently folded into ``cleared``.
    """
    token_cleared = False
    file_cleared = False
    not_removed = None

    try:
        from naukri_server import browser as _browser_mod

        manager = _browser_mod.browser.token_manager
        token_cleared = bool(getattr(manager, "_token", None))
        manager.invalidate()
    except Exception as exc:
        not_removed = "the cached token (%s)" % type(exc).__name__
        logger.warning("logout: could not invalidate the cached token: %s", exc)

    try:
        from naukri_server.browser import TokenManager

        state_file = TokenManager._AUTH_STATE_FILE
        if state_file.exists():
            state_file.unlink()
            file_cleared = True
    except Exception as exc:
        detail = "auth_state.json (%s)" % type(exc).__name__
        not_removed = "%s and %s" % (not_removed, detail) if not_removed else detail
        logger.warning("logout: could not remove the exported auth state: %s", exc)

    cleared = token_cleared or file_cleared
    result = {
        "cleared": cleared,
        "scope": (
            "the cached nauk_at in this process and the exported "
            "auth_state.json in the Chrome profile directory. NOT the Chrome "
            "profile itself, and NOT your Naukri session -- no request is "
            "sent to Naukri and you stay signed in there."
        ),
        "authenticated": False,
        "reason": (
            "no credential is left here to sign a request with, so no "
            "authenticated request can be made. This is provable without "
            "asking Naukri, and it is the only sense in which this false is a "
            "measurement -- Naukri was not consulted and has not signed you out."
        ),
        "what_is_lost": (
            "only the local copy. The Chrome profile at %s is untouched, so "
            "nauk_rt and nauk_sid are still there and naukri_reauth will "
            "usually mint a fresh nauk_at straight away with no password. "
            "Anything reading auth_state.json across processes loses its copy "
            "until the next export." % display_path(CHROME_PROFILE)
        ),
        "recover_by": _RECOVER_BY,
    }
    if not cleared and not not_removed:
        result["reason"] = (
            "there was nothing to clear -- no cached nauk_at and no exported "
            "auth_state.json -- so " + result["reason"]
        )
    if not_removed:
        result["not_removed"] = (
            "%s could not be removed, so the clear is partial. Re-run, or "
            "stop the process holding it." % not_removed
        )
    return result


# ---------------------------------------------------------------------------
# 3. reauth
# ---------------------------------------------------------------------------

async def reauth() -> dict:
    """Renew ``nauk_at`` with no sign-in. The proven silent path.

    Two stages, escalating, each named in ``stage``:

    1. ``cache_refresh`` -- ``TokenManager.refresh_via_pool`` navigates an
       existing pool page. The profile's ``nauk_rt`` rides along and Naukri
       mints a fresh ``nauk_at``. Cheap, and the usual answer.
    2. ``browser_restart`` -- only when stage 1 produced nothing. Stops and
       restarts the browser from the SAME persistent profile and re-extracts.
       This is the move that ended the 8.5-hour outage on 2026-08-22.

    IT NEVER NAVIGATES TO A LOGIN PAGE and it takes no credential parameter,
    by signature. If both stages fail it says so and names ``naukri_login``.

    THEN IT ASKS. A fresh token appearing is a reason to ask, never an answer
    -- Naukri hands out a ``nauk_at`` that a revoked account will still be
    refused with. ``renewed`` is true only after
    :func:`services.auth_service.get_login_status` came back ``logged_in`` AND
    ``verified``; an inconclusive check leaves ``renewed`` false and
    ``authenticated`` null, which are different fields saying different things.

    SNAPSHOT AND RESTORE. The cached token and cookie header are captured
    before anything runs and put back on any outcome other than a proven
    success, per the contract. This is load-bearing rather than tidy:
    ``refresh_via_pool`` sets ``_token = None`` before it reloads, so a failed
    stage 1 would otherwise DESTROY a cached token that was working, turning a
    diagnostic call into an outage. Restoring is at worst a no-op back to the
    state before the call -- the REST layer's own 401 path re-mints from there
    exactly as it did before -- and at best it saves the session.
    """
    from naukri_server import browser as _browser_mod

    manager = _browser_mod.browser.token_manager
    before_token = getattr(manager, "_token", None)
    before_cookies = getattr(manager, "_cookies", None)

    stage = "none"
    minted = None
    failures = []

    try:
        minted = await manager.refresh_via_pool(
            _browser_mod.browser.page_pool, stale_token=before_token
        )
    except Exception as exc:
        failures.append(
            "cache_refresh: %s: %s" % (type(exc).__name__, exc))
    if minted:
        stage = "cache_refresh"
    elif not failures:
        failures.append("cache_refresh: returned no token")

    if not minted:
        try:
            await _browser_mod.browser.stop()
            await _browser_mod.browser.start()
            minted = getattr(manager, "_token", None)
            if not minted:
                failures.append(
                    "browser_restart: the profile relaunched but no nauk_at "
                    "came back, which is what a revoked nauk_rt looks like"
                )
        except Exception as exc:
            failures.append(
                "browser_restart: %s: %s" % (type(exc).__name__, exc))
        if minted:
            stage = "browser_restart"

    status = await get_login_status()
    verified = bool(status.get("verified"))
    logged_in = bool(status.get("logged_in"))
    authenticated = logged_in if verified else None
    renewed = bool(verified and logged_in)

    if not renewed:
        manager._token = before_token
        manager._cookies = before_cookies

    jar, jar_error = _read_jar()
    cached = getattr(manager, "_token", None)

    return {
        "renewed": renewed,
        "authenticated": authenticated,
        "method": (
            "silent renew from the persistent Chrome profile: navigate with "
            "the existing nauk_rt so Naukri re-mints nauk_at. No login page "
            "is opened, no password is typed, and this tool takes no "
            "credential parameter."
        ),
        "stage": stage,
        "checked_against": AUTH_ENDPOINT,
        "reason": _reauth_reason(
            renewed=renewed,
            verified=verified,
            minted=bool(minted),
            stage=stage,
            status_reason=str(status.get("reason", "")),
            failures=failures,
        ),
        "credential": _credential_block(cached, jar, jar_error),
    }


def _reauth_reason(
    *, renewed: bool, verified: bool, minted: bool, stage: str,
    status_reason: str, failures: list,
) -> str:
    """The sentence that has to distinguish four outcomes people confuse.

    Renewed; minted-but-refused; minted-but-unverifiable; and nothing minted.
    The middle two both leave ``renewed`` false, and they send the operator to
    completely different places -- one to ``naukri_login``, the other to
    waiting for the network -- so they do not share wording.
    """
    if renewed:
        return (
            "a fresh nauk_at was minted at stage '%s' and the live check "
            "confirmed it: renewed is true because Naukri said yes, not "
            "because a token appeared." % stage
        )
    if minted and verified:
        return (
            "a fresh nauk_at was minted at stage '%s', and the live check "
            "still came back not-authenticated (%s). A new token is not a "
            "session: the profile's nauk_rt is no longer trusted. Sign in "
            "with naukri_login." % (stage, status_reason)
        )
    if minted:
        return (
            "a fresh nauk_at was minted at stage '%s', but the live check "
            "could not complete (%s), so renewed stays false and "
            "authenticated is null rather than assumed. Retry, or read "
            "naukri_session_info. The previous cached credential was put "
            "back, so nothing was lost by asking." % (stage, status_reason)
        )
    return (
        "no fresh nauk_at could be minted, so nothing changed and the "
        "previous cached credential was put back. What was tried: %s. Sign "
        "in with naukri_login." % ("; ".join(failures) or "nothing")
    )
