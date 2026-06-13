"""Block-state classifier — a PURE function over an already-received response.

Naukri sits behind Akamai Bot Manager. When it decides a request looks like
automation it does NOT always return a clean ``403``/``406``: it may return a
``200`` whose *body* is a CAPTCHA / "unusual activity" HTML interstitial, or a
``302`` redirect to a login / checkpoint page. Treating any of those as a
healthy success (incrementing success counters, resetting the circuit breaker)
makes a soft-blocked session read as "healthy" and corrupts the very signals
we use to detect a block — see the bug fixed in ``api.py`` where
``api_metrics.success`` / ``_api_circuit.record_success()`` fired BEFORE the
content-type guard.

This module classifies a response into one of:

    healthy       — a real, usable response
    soft_block    — generic bot-flag / "unusual activity" interstitial
    captcha       — explicit CAPTCHA / reCAPTCHA challenge
    rate_limited  — 429 / "too many requests" / "slow down"
    login_wall    — redirected to login / session checkpoint

It is deliberately:
  * PURE — no I/O, no network, no globals; takes the response parts it needs as
    plain arguments so it is trivially unit-testable with synthetic bodies.
  * CONSERVATIVE on ``healthy`` — markers are matched case-insensitively and a
    real JSON body that merely *mentions* a word is not enough on its own (the
    content-type and status are weighed first).
  * Defensive — never raises on weird input (None body, bytes, huge body).

Roll-out: callers should classify + LOG first (flag-gated) and only later let
the result gate behavior, so we observe its accuracy on real traffic before it
can trip a breaker. See ``BLOCK_STATE_CLASSIFIER_ENFORCE`` in ``config.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------


class BlockState(str, Enum):
    """Classification of a received response w.r.t. bot-blocking.

    ``str`` mixin so the value serializes cleanly into JSON / metrics / logs and
    compares equal to its string form (``BlockState.HEALTHY == "healthy"``).
    """

    HEALTHY = "healthy"
    SOFT_BLOCK = "soft_block"
    CAPTCHA = "captcha"
    RATE_LIMITED = "rate_limited"
    LOGIN_WALL = "login_wall"

    @property
    def is_block(self) -> bool:
        """True for any non-healthy state (i.e. something is wrong)."""
        return self is not BlockState.HEALTHY


@dataclass(frozen=True)
class BlockAssessment:
    """Result of classifying one response.

    Attributes:
        state:   the BlockState verdict.
        reason:  short human-readable explanation (for logs / notifications).
        signal:  the specific marker/condition that triggered a non-healthy
                 verdict (e.g. ``"body-marker:unusual activity"``,
                 ``"status:406"``, ``"redirect:/nlogin"``), or "" if healthy.
    """

    state: BlockState
    reason: str = ""
    signal: str = ""

    @property
    def is_block(self) -> bool:
        return self.state.is_block


# ---------------------------------------------------------------------------
# Marker tables — lower-cased substrings matched against the response body.
# Ordering of CHECKS below encodes priority (captcha > login_wall > rate >
# soft_block) so the most specific/actionable verdict wins.
# ---------------------------------------------------------------------------

# Explicit CAPTCHA / challenge markers.
_CAPTCHA_MARKERS = (
    "recaptcha",
    "g-recaptcha",
    "hcaptcha",
    "captcha",
    "i'm not a robot",
    "im not a robot",
    "/sec/",            # Akamai sec-cpt challenge path fragment
    "ak_bmsc",          # Akamai bot-manager challenge cookie/script marker
    "_abck",            # Akamai bot-manager sensor cookie marker
)

# Login / session-checkpoint markers (body text or redirect-target fragments).
_LOGIN_MARKERS = (
    "please log in",
    "please login",
    "sign in to continue",
    "session expired",
    "your session has expired",
    "log in to naukri",
    "loginform",
)
# URL-path fragments that mean "redirected to a login/checkpoint page".
_LOGIN_URL_FRAGMENTS = (
    "/nlogin",
    "/login",
    "/checkpoint",
    "/account/login",
)

# Rate-limit markers.
_RATE_MARKERS = (
    "too many requests",
    "rate limit",
    "rate-limit",
    "slow down",
    "try again later",
)

# Generic bot-flag / interstitial markers ("unusual activity", access denied,
# Akamai reference pages). Lowest priority of the block markers.
_SOFT_BLOCK_MARKERS = (
    "unusual activity",
    "unusual traffic",
    "suspicious activity",
    "access denied",
    "request blocked",
    "you have been blocked",
    "bot detected",
    "automated traffic",
    "verify you are human",
    "verify that you are a human",
    "reference #",            # Akamai "Reference #..." error page
    "akamai",
)


def _body_to_text(body: object) -> str:
    """Coerce a body of unknown type into a lower-cased str, defensively.

    Accepts str / bytes / None / anything; never raises. Caps at 64 KiB so a
    pathologically large body can't blow up the substring scans.
    """
    if body is None:
        return ""
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = str(body)
    elif isinstance(body, str):
        text = body
    else:
        text = str(body)
    # Cap before lower() to bound work on huge inputs.
    if len(text) > 65536:
        text = text[:65536]
    return text.lower()


def _first_marker(text: str, markers: tuple[str, ...]) -> Optional[str]:
    """Return the first marker found in ``text``, or None."""
    for m in markers:
        if m in text:
            return m
    return None


def _looks_like_html(content_type: str, text: str) -> bool:
    """Heuristic: response is HTML (interstitials are served as HTML)."""
    if "html" in content_type:
        return True
    stripped = text.lstrip()
    return stripped.startswith("<!doctype") or stripped.startswith("<html")


def _is_jsonish(content_type: str) -> bool:
    """True if the content-type is a JSON / JS API payload (the happy path)."""
    return "json" in content_type or "javascript" in content_type


# ---------------------------------------------------------------------------
# Public classifier
# ---------------------------------------------------------------------------


def classify_block_state(
    *,
    status: int,
    content_type: str = "",
    body: object = "",
    redirected: bool = False,
    final_url: str = "",
) -> BlockAssessment:
    """Classify an already-received response into a :class:`BlockState`.

    PURE: no I/O. Pass the parts of the response you already have. All string
    inputs are matched case-insensitively.

    Args:
        status:       HTTP status code as received.
        content_type: the ``Content-Type`` header (any case), e.g.
                      ``"text/html; charset=utf-8"``.
        body:         response body — str / bytes / None all accepted. For large
                      bodies pass the first chunk; markers appear early.
        redirected:   True if the request followed a redirect (aiohttp
                      ``resp.history`` non-empty). A redirect to a login URL is a
                      strong login-wall signal even on a 200.
        final_url:    the final URL after redirects (``resp.url``) — checked for
                      login/checkpoint path fragments.

    Returns:
        BlockAssessment(state, reason, signal). ``state == HEALTHY`` only when
        nothing suspicious is found.
    """
    ct = (content_type or "").lower()
    text = _body_to_text(body)
    url = (final_url or "").lower()

    # --- 1. Redirect to a login/checkpoint URL — strongest login-wall signal,
    #        independent of status (Akamai often 302→login, ending at 200). ----
    login_url_frag = _first_marker(url, _LOGIN_URL_FRAGMENTS) if url else None
    if login_url_frag is not None:
        return BlockAssessment(
            BlockState.LOGIN_WALL,
            reason=f"redirected to login/checkpoint URL ({login_url_frag})",
            signal=f"redirect:{login_url_frag}",
        )
    if redirected and login_url_frag is None and url:
        # Redirected somewhere non-login — weak signal; fall through to body
        # checks but remember it for the soft-block reason if nothing stronger.
        pass

    # --- 2. Explicit rate-limit by status. ---------------------------------
    if status == 429:
        return BlockAssessment(
            BlockState.RATE_LIMITED,
            reason="HTTP 429 Too Many Requests",
            signal="status:429",
        )

    # --- 3. Body-marker scan (priority: captcha > login > rate > soft). -----
    #        Markers are decisive regardless of status because Akamai serves
    #        interstitials with a 200.
    cap = _first_marker(text, _CAPTCHA_MARKERS)
    if cap is not None:
        return BlockAssessment(
            BlockState.CAPTCHA,
            reason=f"CAPTCHA/challenge marker in body ('{cap}')",
            signal=f"body-marker:{cap}",
        )

    login_body = _first_marker(text, _LOGIN_MARKERS)
    if login_body is not None:
        return BlockAssessment(
            BlockState.LOGIN_WALL,
            reason=f"login-wall marker in body ('{login_body}')",
            signal=f"body-marker:{login_body}",
        )

    rate_body = _first_marker(text, _RATE_MARKERS)
    if rate_body is not None:
        return BlockAssessment(
            BlockState.RATE_LIMITED,
            reason=f"rate-limit marker in body ('{rate_body}')",
            signal=f"body-marker:{rate_body}",
        )

    soft = _first_marker(text, _SOFT_BLOCK_MARKERS)
    if soft is not None:
        return BlockAssessment(
            BlockState.SOFT_BLOCK,
            reason=f"bot-flag/interstitial marker in body ('{soft}')",
            signal=f"body-marker:{soft}",
        )

    # --- 4. Status-based bot-check (403/406) with no decisive body marker. ---
    #        Akamai's classic automation rejection. Classify as soft_block
    #        unless the body looks like a CAPTCHA (handled above).
    if status in (403, 406):
        return BlockAssessment(
            BlockState.SOFT_BLOCK,
            reason=f"HTTP {status} bot-check (Akamai automation rejection)",
            signal=f"status:{status}",
        )

    # --- 5. 200-class but HTML where an API JSON payload was expected. -------
    #        A successful status carrying an HTML body on a REST endpoint is the
    #        hallmark of a silent interstitial even without a known marker.
    if 200 <= status < 300 and _looks_like_html(ct, text) and not _is_jsonish(ct):
        return BlockAssessment(
            BlockState.SOFT_BLOCK,
            reason=f"HTTP {status} returned HTML where JSON expected "
                   f"(content-type={content_type!r}) — likely interstitial",
            signal="html-on-success",
        )

    # --- 6. Followed a redirect to a non-login destination, success status,
    #        nothing else flagged — mild signal, treat as soft_block so it is
    #        at least visible (a clean API call shouldn't be redirected).
    if redirected and 200 <= status < 300 and not _is_jsonish(ct):
        return BlockAssessment(
            BlockState.SOFT_BLOCK,
            reason=f"unexpected redirect to {final_url!r} on HTTP {status}",
            signal="unexpected-redirect",
        )

    # --- Nothing suspicious. ------------------------------------------------
    return BlockAssessment(BlockState.HEALTHY)
