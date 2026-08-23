# naukri auth-lifecycle slice - naukri_session_info / naukri_logout / naukri_reauth

Slice of the 2026-08-23 auth contract
(`mcp-servers/_audit/2026-08-23-auth-contract.md`), naukri repo only.

**Commit:** `d00193b1464a8936fb67ab1b5555f6888271a180` on `master`,
`feat(auth): the session outlives nauk_at, and nauk_rt is why`.
NOT PUSHED - the wave lead pushes after review. No AI co-author trailer.

## 1. The three tools

| tool | file:line | service |
|---|---|---|
| `naukri_session_info(verify_live: bool = True)` | `naukri_server/tools/auth.py:239` | `naukri_server/services/session_service.py:423` |
| `naukri_logout()` | `naukri_server/tools/auth.py:276` | `naukri_server/services/session_service.py:506` |
| `naukri_reauth()` | `naukri_server/tools/auth.py:300` | `naukri_server/services/session_service.py:594` |

Supporting code, both new:

* `naukri_server/cookie_jar.py` - the PORT of
  `linkedin/linkedin_server/cookie_jar.py`. `read_jar` at line 167. Copy the
  jar plus its `-journal` / `-wal` / `-shm` siblings to a temp dir, open the
  COPY, delete it in a `finally`; metadata columns named one by one, no
  wildcard, no value column. Domain filter is naukri's, matched on label
  boundaries so `notnaukri.com` is rejected.
* `naukri_server/services/session_service.py` - all three implementations plus
  `jwt_exp` (line 97), the `exp`-claim decoder.

Adapters at `tools/auth.py:150/155/160` (`_session_info`, `_logout`,
`_reauth`), mirroring the existing `_get_login_status` seam so tests can patch
at the tool path.

## 2. Test counts

| | collected | note |
|---|---|---|
| baseline, before any edit | **2775** passed, 8 deselected | measured, matches the contract's stated baseline |
| after, full suite | **2864** passed, 8 deselected | `venv/Scripts/python.exe -m pytest tests -q` -> `2864 passed, 8 deselected in 179.70s` |
| new module alone | **89** passed | `tests/test_auth_lifecycle_honesty.py` |

Delta is +89, all in the one new module. No existing test was edited, skipped,
weakened or deleted.

## 3. The control, measured

`scripts/presence_is_auth_control.py` - a pytest plugin that re-creates the
exact bug: `authenticated` and `renewed` derived from the credential's
PRESENCE instead of from the live request, plus `expired`/`present` false
where they are unknowable, plus `live_check.completed` forced true with
`why_not` dropped.

Run:

```
PYTHONPATH=scripts venv/Scripts/python.exe -m pytest \
    tests/test_auth_lifecycle_honesty.py -q -p presence_is_auth_control
```

Measured 2026-08-23 against this commit. The pytest summary line:

```
19 failed, 70 passed in 3.19s
```

The 19:

```
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
```

The asymmetry is the point, and two survivors are worth naming:

* `test_a_confirmed_session_is_true` and `test_a_confirmed_renew_reports_renewed`
  stay GREEN. When the session really is live the permissive build agrees -
  which is precisely why the bug was invisible for 8.5 hours, and why
  "it worked when I tried it" proves nothing in this area.
* `test_completed_never_disagrees_with_the_verdict` stays GREEN on all eight
  branches. The permissive build is INTERNALLY CONSISTENT, so a
  self-consistency check waves it straight through. Only a check that knows
  what the answer should have been catches it.

`TestJwtExp`, `TestLogout`, `TestCookieJarReader`, the leak walkers and the
registration tests also stay green: the control changes the VERDICT, not the
value containment, the decoder, the local clear or the jar reader. A control
that reddens everything measures nothing.

## 4. Measured against the live profile

Through the new reader (copy-first, metadata columns only - the live jar was
never opened), 2026-08-23 07:44 UTC:

| cookie | expires | remaining |
|---|---|---|
| `nauk_at` | 2026-08-22T18:22:02Z | **-0.3 days** (already past) |
| `nauk_rt` | 2027-02-27T05:07:52Z | +188.1 days |
| `nauk_sid` | 2027-02-27T05:07:52Z | +188.1 days |
| `nauk_cs` | 2027-08-22T17:22:13Z | +364.6 days |

This reproduces the contract's own evidence exactly and is the shape of the
2026-08-22 outage: the credential dead, the refresh route healthy.
`naukri_session_info(verify_live=False)` was run once end to end against the
real profile and returned that state with `authenticated: null`. No other tool
was run live.

## 5. Safety - what was NOT run

* `naukri_logout` and `naukri_reauth` were **never executed** against the real
  profile, the real `TokenManager` or the real `auth_state.json`. Every test
  uses fakes plus `tmp_path`, and an autouse fixture in the new module repoints
  `TokenManager._AUTH_STATE_FILE` at a temp dir and replaces
  `naukri_server.browser.browser`, so no path through the module - including a
  buggy one - can reach the live store.
* No apply / profile-write tool was called: not `naukri_apply`,
  `naukri_apply_top_fits`, `naukri_batch_apply`, `naukri_auto_hunt`,
  `naukri_agent_run_now`, none of them.
* The Chrome profile cookie jar was read ONLY through the copy-first reader.
  `chrome-profile/Default/Network/Cookies` was never opened directly.
* No browser was started; no network call was made.
* Strict ASCII in every file written. `naukri_server/tools/auth.py` has 8
  pre-existing non-ASCII lines (em-dashes, lines 1/23/27/51/111/138/179/180);
  none is in an added line - verified by scanning the added lines of the diff.

## 6. Interpretation calls the lead should ratify

Two places where the contract's shape sketch and its section-0 honesty rules
pull in different directions. Both are flagged rather than quietly decided.

**(a) `present: null` when the jar cannot be read.** The shape sketch types
`credential.present` and `supporting[].present` as `bool`. But when the jar
read fails and there is no cached token, "no credential" and "could not look"
are the same distinction section 0 makes absolute for `authenticated` and
`expired`. So this build reports `present: null` plus a named `why_unknown`
field in that one case, rather than `false`. If a cross-repo parity check
asserts `isinstance(present, bool)`, this is where it will fire - say the word
and it becomes `false` with the reason moved into `why_unknown` only.

**(b) `expires_in_days` can render as `-0.0`.** The contract fixes the formula
as `round(seconds / 86400, 1)`, so a credential that lapsed an hour ago prints
`-0.0`, which reads as "zero days" rather than "past". Left exactly as the
contract specifies; `expired: true` carries the signal. Raising it because the
formula is normative and I did not want to vary it silently.

**(c) reauth restore is the LITERAL contract rule.** The previous cached token
and cookie header are put back on any outcome other than `renewed: true`,
including the case where a stage minted a new token and only the live check was
inconclusive. That is what section 3 says. It is safe here because the restore
returns the server to exactly its pre-call state and the REST layer's own 401
path re-mints from there, so the worst case is a no-op rather than a
regression. Noting it because a reader might expect the new token to be kept.

## 7. What I could NOT do, and why

**The tool-count / tool-registry test does not exist in the collected suite.**
The brief expected one. I searched for it and it is not there. What exists:

* `tests/test_server_info.py:280` - `surface["tools"] == len(mcp._tool_manager._tools)`
  and `surface["tools"] > 100`. Both DYNAMIC; they self-update and needed no
  change.
* `tests/test_resource_prompt_scrubbing.py:241` -
  `len(mcp._tool_manager._tools) >= 100`. Dynamic.
* `tests/test_path_leaks.py:318` - certifies the registration PATH, not a count.

The only hard count in the repo is `_sweep/final_verdicts.py:111`
(`assert len(out) == 120`) with `_sweep/spec.py` declaring "every one of the
120 registered tools". **I deliberately did not touch it.** `_sweep/` is not in
`testpaths` (pytest.ini: `testpaths = tests`), so it is not part of the suite;
it is the recorded artifact of the 2026-08-22 end-to-end run, with
`results.json` and `final_verdicts.json` beside it. Its `120` is the count at
run time, and its assert compares against its own static spec, not the live
registry - so my three tools neither break it nor belong in it. Editing it
would falsify a historical record, and `e2e-naukri` may own that directory.
**Flagging for the lead's ruling rather than deciding unilaterally.**

Instead of weakening or inventing a count, I extended the registry coverage in
the shape the repo already uses - named, not numbered, in
`tests/test_auth_lifecycle_honesty.py` (`TestTheToolsAreRegistered`):

1. each of the three names is in `mcp._tool_manager._tools`;
2. each carries `__wrapped_by_watchdog__`, i.e. it went through
   `_watchdogged_tool` and therefore through the server-wide path scrubber -
   registration alone would not prove that;
3. the three sit alongside `naukri_login` / `naukri_verify_otp` /
   `naukri_auth_status`, so one going missing fails.

Live registry after the change: **124 tools** (121 before).

**Two stale doc counts, left alone as out of scope:** `README.md:33` says
"27 tool modules (117 tools)" and `naukri_server/__init__.py:255` records
"MEASURED 2026-08-21: 120 of 120 tools scrubbed". The README line was already
stale before this slice (121 registered, not 117); the `__init__.py` line is a
dated historical measurement and is correct as of its date. Neither is a test.
Handing them to whoever owns hygiene rather than editing another slice's files.

## 8. Contract conformance, field by field

`session_info`: `server`, `authenticated`, `checked_against`, `live_check`
(`attempted` / `completed` / `endpoint` / `why_not` / `what_it_means`),
`credential` (`kind` / `name` / `present` / `format` / `expires_at` /
`expires_in_days` / `expired` / `expiry_source` / `expiry_is_authoritative`),
`supporting` (`nauk_rt` role `refresh` FIRST, then `nauk_sid` role `session`,
`nauk_cs` role `csrf`), `credential_source`, `durability` (`stored_in` through
`policy.display_path` -> `"chrome-profile"`, survives restart and reboot, and
a `why` that says the profile keeps the long-lived cookies so a restart
re-mints `nauk_at` with no password), `renewal` (`silent_renew_available:
true`, `tool: "naukri_reauth"`, `why` naming the `nauk_rt` mechanism),
`on_expiry`. `completed` is computed as `authenticated is not None`, never
tracked separately. `verify_live=False` emits the contract's exact `why_not`
string.

`logout`: `cleared`, `scope`, `authenticated: false`, `reason`,
`what_is_lost`, `recover_by`, plus `not_removed` only when something (a locked
file) survived. Never raises - proven with a locked-file stub and an exploding
token manager. Structurally proven not to reach `rmtree` / `rmdir` / `remove`.

`reauth`: `renewed`, `authenticated`, `method`, `stage`, `checked_against`,
`reason`, `credential`. `stage` is `cache_refresh` / `browser_restart` /
`none`. Takes no parameters (asserted from the signature, on both the service
and the registered tool) and reaches no navigation call (asserted by AST scan
with its own control).
