"""A PUBLIC-CONSTANT-NEEDLE build of the leak walker, for showing it fails.

WHY THIS FILE IS IN THE REPO
----------------------------
``tests/test_auth_lifecycle_honesty.py`` asserts that no credential value ever
leaves ``naukri_session_info`` / ``naukri_reauth``, in any field, on any path.
Every one of those assertions delegates its verdict to one helper pair --
``_jwt`` builds the secret and ``_token_needles`` decides what counts as a
sighting of it. So the whole class is only ever as good as that pair, and the
pair has now been wrong TWICE:

* ROUND 1 -- the walker hunted the plaintext marker ``SUPERSECRETNAUKATBODY``.
  That marker lives base64url-ENCODED inside the JWT payload and never appears
  in the token string, so a result echoing the ENTIRE credential passed clean.
  Every "the token never leaks" test was decorative. Fixed 2026-08-23.

* ROUND 2 -- the fix over-corrected. ``_token_needles`` returned all three
  segments, two of which carry no secret at all: the header
  ``eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9``, which is byte-identical in every
  HS256 JWT ever minted, and the signature ``c2lnbmF0dXJl``, which was
  hardcoded and therefore shared by every token the module builds. The walker
  could no longer tell this credential from any other one. This plugin
  re-creates that build.

The two failures are the same disease seen from opposite sides. Round 1 could
not fire when it should; round 2 fired when it should not. Both destroy the
same thing -- the link between "the walker went red" and "a secret escaped" --
and only a control that pins BOTH directions notices either.

THE TWO SUBSTITUTIONS
---------------------
* ``_jwt`` gets its hardcoded ``c2lnbmF0dXJl`` signature back, so two tokens
  carrying different secrets collide on their third segment.
* ``_token_needles`` stops excluding the public header, so two tokens carrying
  different secrets collide on their first segment as well.

Nothing else is touched. In particular the round-1 bug is NOT restored: this
build still hunts the token string rather than the plaintext marker, so the
tests that catch a real planted leak stay green. A control that reddens
everything measures nothing.

HOW TO RUN IT
-------------
    PYTHONPATH=scripts pytest tests/test_auth_lifecycle_honesty.py \\
        -p public_constant_needle_control

    # PowerShell
    $env:PYTHONPATH="scripts"; venv/Scripts/python -m pytest \\
        tests/test_auth_lifecycle_honesty.py -p public_constant_needle_control

MEASURED 2026-08-23 against the fixed module. Clean it is 124 passed; with
this plugin loaded::

    1 failed, 123 passed

    FAILED TestNoCredentialValueEscapes::test_CONTROL_the_walker_is_quiet_on_a_foreign_token

ONE red, and that is the correct answer rather than a weak one. The bug this
plugin restores is a FALSE-POSITIVE bug: it makes the walker see leaks that
are not there. No test in the module hands the walker a foreign token, because
until today none existed -- which is precisely why the defect survived round 2
and why the new control is the only thing that can see it. If that control is
ever deleted or weakened, this plugin goes fully green and the walker can go
back to matching public constants with nothing to say so.

The 123 that survive are supposed to survive:

* ``test_CONTROL_the_walker_catches_a_planted_token`` stays green. A needle
  set with EXTRA members still catches every real leak -- over-matching does
  not break detection, it breaks discrimination. This is the asymmetry that
  let round 2 ship.
* Every ``test_..._never_returns_the_token`` and ``..._never_logs_the_token``
  stays green, because the server genuinely does not leak and adding needles
  cannot change that.
* ``TestJwtExp`` stays green: the decoder reads segment 1 only and does not
  care what the signature says.
"""


def pytest_sessionstart(session):
    from tests import test_auth_lifecycle_honesty as mod

    def _jwt_with_a_shared_signature(exp=None, marker=mod.TOKEN_MARKER):
        """The round-2 shape: the signature is a constant, not the secret."""
        claims = {"sub": marker}
        if exp is not None:
            claims["exp"] = exp
        return "%s.%s.%s" % (
            mod.JWT_PUBLIC_HEADER, mod._b64(claims), "c2lnbmF0dXJl")

    def _needles_including_the_public_header(token):
        """The round-2 shape: every segment, secret-bearing or not."""
        parts = [p for p in str(token).split(".") if len(p) > 8]
        return [str(token)] + parts

    mod._jwt = _jwt_with_a_shared_signature
    mod._token_needles = _needles_including_the_public_header
