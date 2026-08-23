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

Six substitutions, which is the whole disease. They are one disease: every
one of them replaces "I do not know" or "I did not prove it" with a
confident-looking value that a client cannot tell apart from a measurement.

* ``authenticated = bool(credential.present)`` -- a cookie in the jar becomes
  a verdict.
* ``live_check.completed = True`` with ``why_not`` dropped, because a build
  that thinks it knows does not report that it could not ask.
* ``expired = False`` wherever the expiry is unknowable -- "no date" reads as
  "checked, still good".
* ``renewed = bool(credential.present)`` -- a token appearing becomes the
  answer instead of the reason to ask.
* ``logout.authenticated = False`` unconditionally -- an absence asserted from
  a clear that did not finish, in the same object as the ``not_removed`` line
  admitting a live credential is still on disk. Same disease, mirror image.
* ``renewal.session_lapses_at`` falling back to ``credential.expires_at`` when
  ``nauk_rt`` is unknowable -- the self-renewing token's minutes standing in
  for the session's months. This is the one that leaves a PLAUSIBLE date in
  the field, which is why a test asserting merely "it is null" would not have
  caught it and the shipped test asserts the inequality instead.

HOW TO RUN IT
-------------
    PYTHONPATH=scripts pytest tests/test_auth_lifecycle_honesty.py \\
        -p presence_is_auth_control

    # PowerShell
    $env:PYTHONPATH="scripts"; venv/Scripts/python -m pytest \\
        tests/test_auth_lifecycle_honesty.py -p presence_is_auth_control

MEASURED 2026-08-23, after all four contract items. The module is 124 passed
clean; with this plugin loaded::

    24 failed, 100 passed in 5.00s

    FAILED TestAuthenticatedIsNullNotFalse::test_an_unverifiable_check_is_null
    FAILED TestAuthenticatedIsNullNotFalse::test_a_real_denial_is_false
    FAILED TestAuthenticatedIsNullNotFalse::test_a_present_unexpired_credential_does_not_make_it_true
    FAILED TestAuthenticatedIsNullNotFalse::test_offline_mode_is_null_in_the_contracts_words
    FAILED TestExpiredIsNullNotFalse::test_no_knowable_expiry_reports_null
    FAILED TestExpiredIsNullNotFalse::test_an_undecodable_token_falls_through_to_null
    FAILED TestExpiredIsNullNotFalse::test_a_session_cookie_is_null_not_false
    FAILED TestExpiredIsNullNotFalse::test_presence_is_null_only_when_NO_store_could_be_read
    FAILED TestTheExportedStateIsAnExpirySource::test_a_token_with_no_exp_claim_is_present_but_undated
    FAILED TestTheRefreshCookieIsReported::test_an_unknowable_nauk_rt_is_null_never_nauk_ats_date
    FAILED TestOfflineModeIsFree::test_it_calls_neither_the_api_nor_the_browser
    FAILED TestLogout::test_a_partial_clear_reports_authenticated_NULL
    FAILED TestLogout::test_it_never_raises_when_the_manager_explodes
    FAILED TestLogout::test_authenticated_is_false_only_where_it_is_provable[locked-None]
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

Items 1-3 are INSIDE that set, not beside it: item 1's exported-state rules
(``test_presence_is_null_only_when_NO_store_could_be_read``,
``test_a_token_with_no_exp_claim_is_present_but_undated``), item 2's partial
logout (three entries under ``TestLogout``), and item 3's forbidden fallback
(``test_an_unknowable_nauk_rt_is_null_never_nauk_ats_date``).

Item 4 (``uses_browser`` / ``mechanism``) is deliberately NOT in it, and that
is a statement rather than an omission: it is a DISCLOSURE requirement, not an
honesty-of-verdict one. This control corrupts verdicts; it does not delete
fields. A control that also stripped ``mechanism`` would be testing that the
control removes what it removes. The disclosure tests guard against a
different failure -- a future edit quietly dropping the cost sentence -- and
their own not-vacuous assertions are what make them bite.

Note ``api_denied`` and ``http-401`` in the list too. A real NO goes red under
this build, and it should: presence-is-auth cannot express a denial any more
than it can express a null.

The 100 that survive are supposed to survive, and reading the list is the
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
* ``test_the_session_lapse_date_tracks_nauk_rt_NOT_nauk_at`` stays green, and
  that is the right asymmetry rather than a gap: in that fixture ``nauk_rt``
  HAS a date, so substitution 6 never fires. The forbidden fallback only
  bites when ``nauk_rt`` is unknowable, which is precisely the fixture
  ``test_an_unknowable_nauk_rt_is_null_never_nauk_ats_date`` builds -- and
  that one goes red. Two tests, two states, one rule.
* ``test_a_full_clear_still_reports_false`` and
  ``test_nothing_to_clear_still_reports_false`` stay green, because a PROVEN
  absence really is ``false`` under both builds. The null in logout has to be
  earned by a real partial, or ``authenticated`` becomes a field that never
  commits to anything.
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
    real_logout = session_service.logout

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

        # 6. the forbidden fallback: when nauk_rt has no date, quietly use the
        #    self-renewing token's. Leaves a plausible ISO8601 string in the
        #    field, which is what makes it dangerous rather than obvious.
        renewal = result.get("renewal")
        if isinstance(renewal, dict) and renewal.get("session_lapses_at") is None:
            renewal["session_lapses_at"] = credential.get("expires_at")
            renewal["session_lapses_in_days"] = credential.get("expires_in_days")
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

    async def presence_logout():
        result = await real_logout()
        # 5. false unconditionally, including beside `not_removed`
        result["authenticated"] = False
        return result

    session_service.session_info = presence_session_info
    session_service.reauth = presence_reauth
    session_service.logout = presence_logout
    print(
        "\n[presence_is_auth_control] authenticated / renewed / "
        "session_lapses_at now come from PRESENCE and fallbacks -- the "
        "pre-fix bug shape"
    )
