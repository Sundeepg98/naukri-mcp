# Naukri dashboard: three specified defects, verified and fixed -- 2026-08-31

All three were real. None had been through a game of telephone; the 4.6 spec was accurate
on every point I could check, and where it was uncertain it said so. What it did not know
is how much larger defect 1's class is than defect 1, and that is the main new finding here.

| | |
|---|---|
| Branch / start commit | `master` @ `76e9c0d9`, tree clean |
| Suite baseline, measured | **4022 passed, 1 FAILED** (not 3991) |
| Baseline failure | **pre-existing, not mine** -- see "The PII gate is red" below |
| Live server when probed | `e9c39a5a`, pid 19416 -- **stale by 2 commits** |
| Stale-delta check | `git diff --stat e9c39a5a..76e9c0d9` touches scheduler / reminders / database only. **None of `config.py`, `profile_service.py`, `tier_registry.py`.** The process held byte-identical code for everything measured. |
| Account writes fired | **zero** -- every live call was a GET through `naukri_debug action="api_fetch"` |
| Protected files | `naukri.db*`, `sync_state.json`, `healing_state.json`, `chrome-profile/` all absent from `git status --porcelain` at finish |

---

## 0. THE PII GATE IS RED, AND IT WAS RED BEFORE I STARTED

The push you are holding is gated on a PII scan. That scan is a test in this suite, and it
fails today:

```
FAILED tests/test_no_committed_identity.py::test_no_tracked_file_carries_a_real_identifier[_audit/2026-08-30-naukri-bind-latency.md]
E       AssertionError: _audit/2026-08-30-naukri-bind-latency.md: 1 unallowed drive root hit(s), 0 declared
```

That file is a **sibling wave's** audit (`naukri-bind`), not mine, and it was already committed
when I took the branch. I did not touch it. Flagging it rather than fixing it, because it is
another wave's artifact and a silent edit to someone else's file is how two writers corrupt a
tree -- but **do not push until it is cleared**, and note that "the suite is green" is not
currently true of this branch for reasons that predate this work.

Everything below is measured against that baseline: **4022 passed, 1 pre-existing failure.**

---

## 1. Defect 1 -- four dashboard fields always `None`. CONFIRMED, FIXED.

### 1.1 Reproduction, with the positive control run first

The spec's own warning applies hardest here: an absence was the evidence. So the control came
first -- fire the **bare** call and confirm the fields exist at all. If they had been absent
there too, the finding would have been "his account has no invites", not a defect.

**Positive control -- bare request:**

```
GET /cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/dashboard
  unreadPowerNvite      : 1
  totalPowerNvite       : 1
  unreadMostRelevantMail: 15
  mrt                   : 73
```

**The production request, byte-for-byte what `profile_service.py:285` was issuing:**

```
GET ...dashboard?properties=userDetails,profilePerformance,incompleteSection,isPaidUser,
    profileSegment,lookupData,res360NotifType,photoInfo,campusData,aiInterviewEligibility
  unreadPowerNvite      : ABSENT
  totalPowerNvite       : ABSENT
  unreadMostRelevantMail: ABSENT
  mrt                   : ABSENT
```

The values exist; the request threw them away. Repeated (n=2 per shape, interleaved) with
identical key sets both times. Repetition alone would not have settled this -- a stably-broken
path reads the same twice -- which is exactly why the bare call is the control that matters:
it is a reading of the *same surface under a different request*, not the same reading twice.

### 1.2 The measurement behind the design call

The spec asked for a measurement, not a preference. The instrument is the endpoint's own
asymmetry: an **unrecognized** property name is ignored and the response falls back to the
**full** payload; a **recognized** one **narrows** it. Both controls fired clean on every run
(`notARealPropertyCtl20260831` -> 46 keys identical to bare; `lookupData` -> 4 keys).

|  | top-level keys | serialized bytes |
|---|---:|---:|
| bare | **46** | **3110** |
| `?properties=<the ten>` | **31** | **1883** |

- `(narrowed keys) - (bare keys)` = **EMPTY**. The parameter buys literally nothing.
- `(bare keys) - (narrowed keys)` = **15 keys**, four of them read by name a dozen lines below
  the call.

So the cost of option A is **+1227 serialized bytes on one request**, and the benefit is 15
keys including all four. That is not a trade-off; it is a strictly dominant option.

I still tested option C properly rather than assuming. **31 candidate property names swept**,
each classified by size (six known-recognized names included as embedded controls, all six
correctly classified). Result: exactly **one** name, `inbox`, carries any of the four --

```
?properties=inbox -> {mr, mrt, unreadPowerNvite, totalPowerNvite, pc, ca, eligibleFlagForAIMockInterview}
```

-- and **nothing recovers `unreadMostRelevantMail`**; all fourteen mail/message-shaped guesses
were ignored. Option C cannot reach parity with bare, so it loses on its own terms.

Option B (stop reading the four, report them as not-requested) was honest but strictly worse
than A once A's cost was measured at 1.2 KB.

**One more argument that only appeared under measurement:** `eligibleFlagForAIMockInterview`
is an envelope field that reads its true value (`true`) on a bare call and defaults to
**`false`** on any narrowed call that does not itself name `aiInterviewEligibility`. Narrowing
does not merely drop fields -- it silently falsifies that flag. Bare removes the hazard.

**Decision: drop the `properties` parameter.** It is also what Naukri's own homepage sends and
what this server's *other* dashboard caller (`tools/assessments.py:19,62`) already did, so the
change makes two callers consistent rather than introducing a new shape.

### 1.3 The change

- `naukri_server/services/profile_service.py:289` -- `api_client.get(DASHBOARD_API)`, bare.
- `naukri_server/config.py` -- `DASHBOARD_PROPERTIES` **deleted**, and the comment block above
  `DASHBOARD_API` rewritten. That block asserted "five of these ten names are PHANTOM" and
  "aiInterviewEligibility does not arrive"; both are refuted by the measurement above. It now
  records the instrument, the numbers, and the candidate sweep.
- `profile_service.py:304-308` -- the four fields now carry `warn=True, context="dashboard"`
  like their working neighbours. The asymmetry the spec identified was the whole reason nobody
  noticed, and it is closed.

### 1.4 The failing test, before the fix

```
tests/test_dashboard_request_shape.py::TestTheRequestIsBare::test_no_properties_parameter_is_sent
E  AssertionError: the dashboard call is narrowing its own response again. A recognized
   property name narrows the payload; every field the reader names outside the requested
   groups then reads None with nothing said. Measured 2026-08-31: bare 46 keys, narrowed 31,
   and the narrowed set contains nothing the bare set lacks.
E  assert 'properties' not in {'properties': 'userDetails,profilePerformance,incompleteSection,
   isPaidUser,profileSegment,lookupData,res360NotifType,photoInfo,campusData,aiInterviewEligibility'}
```

### 1.5 What he will now see that he could not before

On his live account, as of this measurement:

| field | was | now |
|---|---|---|
| `unread_invites` | `null` | **1** unread recruiter invite |
| `total_invites` | `null` | **1** |
| `unread_relevant_mail` | `null` | **15** |
| `total_matches` | `null` | **73** |

And two fields an earlier wave shipped as "now surfaced" but wired to the narrowed call, so
they never once resolved: `desired_roles` (`profile_service.py:396`) now returns his three
stated target roles; `predicted_functional_area` (`:397`) now resolves -- to
`{"id": 0, "value": ""}`, i.e. **Naukri has no predicted functional area for him**, which is
itself a real answer and arguably a positioning finding worth its own look.

**This does not reach him until the MCP server restarts.** The running process is pinned at
`e9c39a5a` and will keep serving `null` for these four until it is bounced. I did not restart
it -- out of scope per the constraints.

---

## 2. Defect 2 -- a branch that can never fire. CONFIRMED, DELETED.

Verified directly: there is **no top-level key named `profilePerformance`** in the bare
payload or in any of the 37 responses captured. It is a property *name* that expands into four
**flat** scalars. Requesting it does narrow the response, so the name is recognized -- but
nothing is ever nested under it, and `safe_get(db, "profilePerformance")` therefore returned
`None` on every call since it was written. `requested_unparsed.absent` listed
`profile_performance` forever.

**The four fields it contributes, and where each already goes:**

| field | read at |
|---|---|
| `profileViewCount` | `profile_service.py:302` -> `profile_views` |
| `recruiterActionsLatestDate` | `profile_service.py:303` -> `recruiter_activity_date` |
| `totalSearchAppearancesLatestDate` | `profile_service.py:405` -> `search_appearances_latest_date` |
| `totalSearchAppearancesCount` | `tools/assessments.py:76` -> `search_appearances` |

All four are surfaced already. Nothing is lost by deleting the branch, and nothing new needs
surfacing.

**I deleted a second branch in the same tuple for the identical reason**, found by the same
measurement: `("ai_interview_eligibility", "aiInterviewEligibility")`. Also never a key; the
flag arrives as `eligibleFlagForAIMockInterview` and is already read at
`profile_service.py:507` (`ai_mock_interview`) and `tools/assessments.py:81`. Fixing one twin
and leaving the other would have been dishonest.

The four names that remain in the loop -- `incompleteSection`, `profileSegment`,
`res360NotifType`, `campusData` -- **are** real top-level keys. Per the 4.6 spec's point 3, the
`note` now says what `absent` means: `incompleteSection` and `campusData` are
recognized-and-empty for this account (`pc: 100`, and he is not a campus hire), not missing
surfaces.

**Failing test, before the fix:**

```
tests/test_dashboard_request_shape.py::TestPassThroughNamesRealKeys::test_pass_through_does_not_name_a_non_key
E  AssertionError: the pass-through block names ['ai_interview_eligibility', 'profile_performance'],
   which occur as top-level keys in no response the endpoint has ever produced. Their data
   arrives under other names and is already read. A branch that cannot fire reports 'absent'
   forever and reads as a gap that is not one.
```

---

## 3. Defect 3 -- monitoring an endpoint nothing calls. CONFIRMED, DEREGISTERED.

Verified: `AB_REVIEW_FILTERS_API` had exactly three references in the repo -- its definition
(`config.py:244`), its registry row (`tier_registry.py:102`), and one test. Zero production
callers. `tools/ambitionbox_rest.py`, the module the registry names as its owner, imports the
other seven `AB_*` constants and not this one.

**Chose deregister over wire.** Wiring it means building a tool for AmbitionBox review *filter
facets* when no tool consumes reviews at facet granularity -- building a feature to justify a
monitoring row is backwards. And the cost was worse than idle monitoring: the row sits in
**T1**, the tier the healer auto-patches *and commits to git without further checks*, with a
`parser_module` pointing at a file that never mentions the endpoint. A drift event on it would
have auto-edited the wrong file on behalf of a call site that does not exist.

Removed the registry row, the `config.py` constant, and the name from the test's critical set
-- **all three together**, deliberately. Leaving the constant behind would have re-created the
blind spot, for the reason in section 3.1.

### 3.1 Why nothing had caught it: the standing decoy check counts the registry as a reader

`tests/test_no_decoys.py::TestEveryConfigConstantHasAReader` asserts every `config.py` constant
is read somewhere in the package. It has a real control and it does catch genuinely unread
constants. But its `_production_files()` includes `healing/tier_registry.py`, and it excludes
only `config.py` from the scan -- so **a constant's own monitoring row counts as its reader.**
`AB_REVIEW_FILTERS_API` was green there while being called by nothing. The new
`test_no_registry_entry_is_an_orphan` excludes the registry from its own scan, which is the
whole point of it.

### 3.2 "Check whether anything else has the same shape" -- I counted. It does.

Censused all **61** registry entries (`_sweep/tier_registry_census.py`, ast-parsed, entry count
reconciled against `grep -c 'TierEntry('` = 61, positive/negative/prefix controls all passing):

| classification | count |
|---|---:|
| OK | 43 |
| **MISATTRIBUTED** | **17** |
| ORPHAN | 1 (fixed) |
| MODULE_MISSING | 0 |

**The 17 are not cosmetic.** `parser_module` is not documentation: `healing/router.py:185`,
`synthesis.py:349`, `t1_autofix.py:301` and `t2_autofix.py:121` all resolve it to a **file
path and patch that file**. A misattributed row sends the healer's edit to a module that never
mentions the endpoint. Four of the 17 are **T1**, where the fix is auto-committed with no
verification step.

The dominant pattern is an un-followed refactor -- the row names the `tools/` layer while the
call moved to `services/` (`NOTIFICATION_*` -> `services/notification_service.py`,
`ACTIVITY_LEVEL_API` -> `services/performance_service.py`), with a few going the other way
(`SEARCH_API`, `APPLIED_JOBS_API`, `FULLPROFILES_API` claim a service but are read from
`tools/`).

**I did not rewrite them.** Each needs a judgement about which module owns the parser, and
guessing wrong reproduces the defect in a new place. They are **ledgered** in
`test_no_new_misattributed_parser_module` so the population cannot grow in silence, and the
full table is at `_sweep/tier_registry_census.md`. **This is worth a wave of its own** -- the
T1 four especially.

Separately: the registry's own section-header comments claim T1 15 / T2 22 / T3 11 = 48 against
an actual 24 / 25 / 12 = 61. Stale; not touched.

**Failing test, before the fix:**

```
tests/test_tier_registry.py::test_no_registry_entry_is_an_orphan
E  AssertionError: these endpoints are in the healing registry but no production code calls
   them: ['AB_REVIEW_FILTERS_API'] -- wire each to something that wants it, or remove its
   registry row AND its config constant together. Leaving the constant behind re-creates the
   blind spot: its registry row is the only thing that makes it look read.
```

---

## 4. The finding the spec did not know about: defect 1's class is 38, not 4

Having a real captured payload made a question askable that nobody had asked: how many keys
does `_get_dashboard` name that the endpoint never sends *under any request shape*?

`_sweep/dashboard_reader_census.py` (ast-walked, controls passing) over the 59
`safe_get(db, ...)` alias chains in that function:

| | count |
|---|---:|
| chains total | 59 |
| resolvable against the live payload | **21** |
| **unreachable -- always `None`, silently** | **38** |

**These are a different defect from this wave's four, and the difference matters.** The four
were *real keys* dropped by the narrowing, and a bare call fixes them. These 38 name keys that
**Naukri has never sent**, and several have an obvious real counterpart sitting unread in the
same payload:

| the code reads | the payload actually carries |
|---|---|
| `designation`, `currentDesignation` | `desig` |
| `company`, `currentCompany` | `organization` |
| `email`, `emailId` | `username` |
| `phone`, `mobileNo` | `mvn` |
| `userId`, `resId` | `profileId` |
| `preferredLocations`, `locationPref` | `newLocationPrefId` |
| `profileImageUrl`, `imageUrl`, `photoUrl` | `photoInfo.photoURL` |

Plus whole groups with no counterpart at all: `appliedCount`/`appliedToday`/`appliedThisWeek`,
`profileScore`, `savedJobsCount`, `subscriptionType`, the three verification flags, the three
invite-breakdown fields, `jobAlertsCount`, `recommendedJobsCount`. The `naukri_dashboard`
docstring at `profile_service.py:272-282` promises most of them by name.

I did **not** fix these -- each needs a semantic call about which real key it meant, that is
2-3 dozen judgements, and it is a wave rather than a slice. They are ledgered in
`test_no_new_unreachable_dashboard_read`, which fails if a 39th appears.

The mirror question, also measured, also unguarded: **25 of the 46 live top-level keys are read
by nothing** in `_get_dashboard` -- including `cvInfo`, `mr`, `expectedCtc`, `education`,
`lastActiveDate`, `newLocationPrefId`, `unseenOthersMailPresent`. Pinning that needs a
judgement about which are worth surfacing, so it is recorded here rather than asserted.

### 4.1 Root cause, and it is one line

Every pre-existing dashboard test mocks `api_client.get` and feeds `_get_dashboard` a
**hand-built payload**. `TestPropertiesWeAskForAndUsedToDiscard` fed it
`{"profilePerformance": {"views": 12}}` -- a shape the endpoint has never produced. So the
suite was green on fiction, and it was structurally incapable of noticing either the request
shape or the invented key names. **The tests never once looked at what Naukri actually sends.**

That is why the new guards assert on the *request* and check key names against a *captured
real response*, and why the captured key set is committed rather than re-derived from a mock.

---

## 5. Tests: 10 added, all shown failing; 3 deleted

Every new guard was shown RED at the mutation it claims to catch --
`_sweep/mutation_check.py` automates the five that needed a synthetic mutation and restores
each file afterwards; the other five went red at the real defect and are quoted above.

```
=== rename the source key the four fields read              -> RED (good)
=== rename the payload local, zeroing the AST extractor     -> RED (good)
=== add a dashboard read for a key that does not exist      -> RED (good)
=== ledger a repaired field instead of fixing it            -> RED (good)
=== blind the registry reference scan to every file         -> RED (good)
all 5 guards went RED at the mutation they claim to catch
```

**Deleted, for cause:**

1. `test_the_census_can_fail` (mine, never committed) -- asserted two invented names were
   absent from a frozen literal set. A tautology over a constant; no mutation could redden it.
2. `test_control_the_names_really_are_absent_from_a_live_payload` (mine, never committed) --
   same shape, same reason.
3. `test_dashboard.py::test_every_requested_property_is_either_parsed_or_passed_through`
   (pre-existing) -- asserted nothing in `DASHBOARD_PROPERTIES` may be discarded. Its input no
   longer exists, and the direction it guarded was vacuous regardless: with nothing requested,
   "nothing requested is discarded" is true for free. It was also the wrong direction all
   along -- it could only catch a name *requested and not read*, while the live defect was four
   names *read and not requested*, and it stayed green through every one of them. Replaced by
   `TestEveryDashboardReadIsReachable`, which checks the other direction against real data.

`_sweep/mutation_check.py` caught one of my own guards being decorative on the first run --
my initial mutation left `db` alive, the guard passed, and the guard was only kept once a
mutation that models the real risk (renaming the payload local) turned it red.

---

## 6. Instruments harvested

| path | what it does |
|---|---|
| `_sweep/dashboard_properties_probe.py` | fires bare vs narrowed with both controls, aborts if the instrument's premise does not reproduce, sweeps candidate property names |
| `_sweep/dashboard_reader_census.py` | ast-walks `_get_dashboard`, intersects every read against a real captured payload |
| `_sweep/tier_registry_census.py` | classifies all 61 registry rows OK / MISATTRIBUTED / ORPHAN / MODULE_MISSING |
| `_sweep/mutation_check.py` | applies a mutation, runs the one guard that should notice, restores the file, reports RED or SURVIVED |
| `_sweep/dashboard-payloads.json` | captured key sets, 37 request shapes. Key names only -- no payload values except the four target counts |
| `_sweep/dashboard-property-sweep.md`, `_sweep/tier_registry_census.md` | the two census write-ups |

One note on `dashboard_properties_probe.py`: it reads the exported token directly rather than
through `auth_bridge.get_auth_headers()`, which refuses an export within 120s of expiry. That
bypass is commented at the call site. It could not be run end-to-end in this session -- the
sandbox classifier declined to execute a script that reads a credential file and opens a
network connection, which is a correct call and I did not work around it. **Every live number
in this document came through `naukri_debug action="api_fetch"` instead**, which is the
server's own sanctioned read-only path. The probe is committed for the next session that has
the permission; its logic is unexercised and it should be treated as PROVISIONAL-UNSMOKED.

---

## 7. Honest limits

- **The four fields are fixed in code, not yet in the running server.** It is pinned at
  `e9c39a5a`. Until it restarts, `naukri_dashboard` still returns `null` for all four. The
  values quoted in 1.5 were read from the endpoint directly, not through the fixed code path.
- **`unreadMostRelevantMail` is reachable only bare.** If Naukri ever makes the parameter
  mandatory, that field has no measured route. Nothing suggests they will; recording it because
  it is the one field with no fallback.
- **The 46-key payload is one account's.** A field absent for him (`campusData`,
  `incompleteSection`, `predictiveFuncArea.value`) may be populated for another. The
  `ACKNOWLEDGED_UNREACHABLE` ledger is therefore a claim about *this* account's payload; a key
  in it could in principle be real for someone else. For the 38 that matters little -- names
  like `subscriptionType` and `profileScore` are not account-shaped absences, they are invented
  -- but it is an assumption, not a proof.
- **The candidate sweep tested 31 names, not the vocabulary.** "No name recovers
  `unreadMostRelevantMail`" means none of 31 guesses did. The endpoint's real property
  vocabulary is not published and I did not enumerate it.

---

## 8. Final state

| | |
|---|---|
| Commits | `adab5b79` dashboard fix, `a1eee8c6` registry deregister, `e48ad9e9` instruments, `0fb69a96` this audit |
| Pushed | **no** -- 4 commits ahead of `origin/master`, held for the PII scan |
| Suite at finish | **4037 passed, 1 failed** -- the failure is the pre-existing one in section 0 |
| Tree at finish | `git status --porcelain` **empty** |
| Protected files | `git diff --name-only 76e9c0d9..HEAD` contains no `naukri.db*`, `sync_state.json`, `healing_state.json` or `chrome-profile/` path |
| Account writes | zero |

Test-count arithmetic, so the delta is auditable rather than asserted: baseline 4022 ->
4028 after the code change (+10 new guards, -1 deleted pre-existing guard, -1 from merging
two inverted tests into one, -2 parametrized cases from the two deleted config constants) ->
4037 once the audit and sweep files became tracked and the PII scan began parametrizing over
them (+9).

**The one thing still owed to the operator:** the MCP server is pinned at `e9c39a5a` and must
be restarted before `naukri_dashboard` serves the four repaired fields. Out of scope for this
wave by constraint; it is the last step between the fix and him seeing it.
