"""A PRESENCE-IS-AUTH build of the session tools, for showing the guards fail.

WHY THIS FILE IS IN THE REPO
----------------------------
``tests/test_auth_lifecycle_honesty.py`` asserts that ``authenticated`` is
null rather than false whenever the live check did not complete, that
``expired`` is null rather than false whenever no expiry is knowable, and that
``renewed`` stays false until a live check says yes. Those assertions are only
worth anything if they are capable of going red -- and this repo has produced
more than a dozen checks that could not fail in a week. A test that has never
been shown failing is a claim, not a measurement.

This pytest plugin re-creates the EXACT bug the 2026-08-23 auth contract
exists to prevent, and the one that hid an 8.5-hour outage on 2026-08-22:
``authenticated`` derived from the credential's PRESENCE instead of from a
live request. Under that build the profile was there, the cookies were there,
``nauk_at`` was there, and every presence check said "fine" while Naukri
refused every call.

Four substitutions, which is the whole disease:

* ``authenticated = bool(credential.present)`` -- a cookie in the jar becomes
  a verdict.
* ``live_check.completed = True`` with ``why_not`` dropped, because a build
  that thinks it knows does not report that it could not ask.
* ``expired = False`` wherever the expiry is unknowable -- "no date" reads as
  "checked, still good".
* ``renewed = bool(credential.present)`` -- a token appearing becomes the
  answer instead of the reason to ask.

HOW TO RUN IT
-------------
    PYTHONPATH=scripts pytest tests/test_auth_lifecycle_honesty.py \\
        -p presence_is_auth_control

    # PowerShell
    $env:PYTHONPATH="scripts"; venv/Scripts/python -m pytest \\
        tests/test_auth_lifecycle_honesty.py -p presence_is_auth_control

MEASURED 2026-08-23, against the commit that adds the three tools. The module
is 89 passed clean; with this plugin loaded::

    19 failed, 70 passed in 3.19s

    FAILED TestAuthenticatedIsNullNotFalse::test_an_unverifiable_check_is_null
    FAILED TestAuthenticatedIsNullNotFalse::test_a_real_denial_is_false
    FAILED TestAuthenticatedIsNullNotFalse::test_a_present_unexpired_credential_does_not_make_it_true
    FAILED TestAuthenticatedIsNullNotFalse::test_offline_mode_is_null_in_the_contracts_words
    FAILED TestExpiredIsNullNotFalse::test_no_knowable_expiry_reports_null
    FAILED TestExpiredIsNullNotFalse::test_an_undecodable_token_falls_through_to_null
    FAILED TestExpiredIsNullNotFalse::test_a_session_cookie_is_null_not_false
    FAILED TestExpiredIsNullNotFalse::test_an_unreadable_jar_makes_presence_null_not_false
    FAILED TestOfflineModeIsFree::test_it_calls_neither_the_api_nor_the_browser
    FAILED TestReauth::test_a_minted_token_with_an_unverifiable_check_is_not_renewed
    FAILED TestReauth::test_a_minted_token_the_api_refuses_is_not_renewed
    FAILED TestReauth::test_a_failed_renew_puts_the_old_credential_back
    FAILED TestReauth::test_both_stages_failing_names_naukri_login
    FAILED TestShapeInvariants::test_every_branch_carries_the_full_contract_shape[offline-no-token]
    FAILED TestShapeInvariants::test_every_branch_carries_the_full_contract_shape[offline-with-token]
    FAILED TestShapeInvariants::test_every_branch_carries_the_full_contract_shape[api_denied]
    FAILED TestShapeInvariants::test_every_branch_carries_the_full_contract_shape[http-401]
    FAILED TestShapeInvariants::test_every_branch_carries_the_full_contract_shape[check_failed-exception]
    FAILED TestShapeInvariants::test_every_branch_carries_the_full_contract_shape[check_failed-missing-field]

Note ``api_denied`` and ``http-401`` in that list. A real NO goes red under
this build too, and it should: presence-is-auth cannot express a denial any
more than it can express a null.

The 70 that survive are supposed to survive, and reading the list is the
point:

* ``test_a_confirmed_session_is_true`` and
  ``test_a_confirmed_renew_reports_renewed`` stay green, because when the
  session really IS live the permissive build agrees -- which is exactly why
  the bug was invisible for 8.5 hours and exactly why "it worked when I tried
  it" proves nothing here.
* ``test_a_past_expiry_reports_true`` stays green: a KNOWABLE past expiry is
  reported the same either way. The null rule is not satisfiable by never
  deciding.
* ``test_completed_never_disagrees_with_the_verdict`` stays green across all
  eight branches, and that is the sharpest thing in this file. The permissive
  build is INTERNALLY CONSISTENT -- it sets ``completed`` true and hands back
  a non-null verdict, so a check for self-consistency waves it straight
  through. Only a check that knows what the ANSWER should have been catches
  it.
* ``TestNoCredentialValueEscapes``, ``TestJwtExp``, ``TestLogout``,
  ``TestCookieJarReader`` and the registration tests stay green: this control
  changes the VERDICT, not the value containment, the decoder, the local
  clear or the jar reader, and a control that reddens everything measures
  nothing.

That asymmetry is the property worth having. If the honesty rules silently
stop being enforced, the guarded tests go red and these stay green.
"""


def pytest_sessionstart(session):
    from naukri_server.services import session_service

    real_session_info = session_service.session_info
    real_reauth = session_service.reauth

    def _presence(result):
        """The substitution, applied to a correctly-built result."""
        credential = result.get("credential") or {}
        present = bool(credential.get("present"))

        # 1. presence becomes the verdict
        result["authenticated"] = present

        # 2. a build that thinks it knows does not report that it could not ask
        live = result.get("live_check")
        if isinstance(live, dict):
            live["completed"] = True
            live.pop("why_not", None)

        # 3. an unknowable expiry reads as "checked, still good"
        if credential.get("expired") is None:
            credential["expired"] = False
        if credential.get("present") is None:
            credential["present"] = False
        for entry in result.get("supporting") or ():
            if entry.get("expired") is None:
                entry["expired"] = False
            if entry.get("present") is None:
                entry["present"] = False
        return result

    async def presence_session_info(verify_live=True):
        return _presence(await real_session_info(verify_live=verify_live))

    async def presence_reauth():
        result = await real_reauth()
        credential = result.get("credential") or {}
        present = bool(credential.get("present"))
        # 4. a token appearing becomes the answer instead of the question
        result["renewed"] = present
        result["authenticated"] = present
        if credential.get("expired") is None:
            credential["expired"] = False
        return result

    session_service.session_info = presence_session_info
    session_service.reauth = presence_reauth
    print(
        "\n[presence_is_auth_control] authenticated / renewed now come from "
        "the credential's PRESENCE -- the pre-fix bug shape"
    )
