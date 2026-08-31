# Dashboard `properties` sweep -- can the narrow request be widened?

Date: 2026-08-31. Endpoint:
`/cloudgateway-mynaukri/resman-aggregator-services/v1/users/self/dashboard`

Instrument: `naukri_debug` `api_fetch` (REST transport, authenticated GET). Every call in
this sweep returned `outcome: ok` / `endpoint_evidence: true`; no errors, no rate limiting,
no bot checks. Sizes below are LOCAL SERIALIZED SIZE -- `len(json.dumps(dashBoard))` computed
on this box -- not wire size.

Classifier: an UNRECOGNIZED property name is ignored and the endpoint falls back to the FULL
payload, so its key set equals BARE's. A RECOGNIZED name narrows the response to a small
envelope plus that property's contribution.

## Controls

| control | request | keys | bytes | expected | result |
|---|---|---|---|---|---|
| NEGATIVE | `?properties=notARealPropertyCtl20260831` | 46 | 3110 | key set == BARE | **PASS** |
| POSITIVE | `?properties=lookupData` | 4 | 495 | strictly smaller than BARE | **PASS** |

BARE reference: **46 keys, 3110 serialized bytes**.

Six further positive controls are embedded in the Step 3 list (`assessments`, `campusData`,
`incompleteSection`, `userDetails`, `profilePerformance`, `aiInterviewEligibility`). All six
reported RECOGNIZED, so the classifier is not stuck-on-unrecognized. **Controls all pass;
the readings below stand.**

## Step 1 -- BARE vs PRODUCTION

| reading | keys | serialized bytes |
|---|---|---|
| BARE (no parameter) | 46 | 3110 |
| PRODUCTION (10-name list) | 31 | 1883 |

### Is BARE a strict superset of PRODUCTION?

**Yes.** Every PRODUCTION key is in BARE, and BARE has 15 more.

`PRODUCTION - BARE` (keys lost by dropping the parameter): **empty** -- the parameter buys
NOTHING that BARE does not already return.

`BARE - PRODUCTION` (keys gained by dropping the parameter), 15 keys:

- `cem_pcs`
- `cvInfo`
- `desiredRole`
- `expectedCtc`
- `expectedCtcCurrency`
- `lastActiveDate`
- `mr`
- `mrt`
- `newLocationPrefId`
- `predictiveFuncArea`
- `similarCompToFollow`
- `totalPowerNvite`
- `unreadMostRelevantMail`
- `unreadPowerNvite`
- `unseenOthersMailPresent`

### The four fields in question

| field | in BARE | value in BARE | in PRODUCTION |
|---|---|---|---|
| `unreadPowerNvite` | yes | `1` | **no** |
| `totalPowerNvite` | yes | `1` | **no** |
| `unreadMostRelevantMail` | yes | `15` | **no** |
| `mrt` | yes | `73` | **no** |

All four are present in BARE and absent from PRODUCTION.

### The `profilePerformance` question

There is **no** top-level key literally named `profilePerformance` -- not in BARE, not in
PRODUCTION. The name is a valid REQUEST property whose contribution arrives FLATTENED as four
top-level scalars (confirmed directly: `?properties=profilePerformance` returns exactly those
four plus the envelope). All four are present in PRODUCTION:

| field | in BARE | in PRODUCTION |
|---|---|---|
| `profilePerformance` | **no** | **no** |
| `profileViewCount` | yes | yes |
| `recruiterActionsLatestDate` | yes | yes |
| `totalSearchAppearancesCount` | yes | yes |
| `totalSearchAppearancesLatestDate` | yes | yes |

## Step 2 -- stability across two readings

| reading | keys r1 | keys r2 | key sets identical |
|---|---|---|---|
| BARE | 46 | 46 | **yes** |
| PRODUCTION | 31 | 31 | **yes** |

Key sets are identical across both readings for both requests. The shape is stable.

Value changes between readings: exactly one, and it is NESTED, not top-level --
`lookupData.lastLoginTime` moved between BARE r1 and BARE r2 (a session heartbeat; the
`properties=lookupData` positive control taken between them shows the later value). No
top-level scalar changed value in either pair; the PRODUCTION pair was identical throughout.
The four target fields held `1 / 1 / 15 / 73` across both BARE readings.

## Step 3 -- candidate sweep

31 names, one request each. `carries` is only meaningful for RECOGNIZED rows: an unrecognized
name returns the whole payload, so it trivially 'contains' all four without meaning anything.

Envelope note: every narrowed response carries `pc`, `ca` and `eligibleFlagForAIMockInterview`
regardless of the property asked for. That 3-key envelope is the floor.

| candidate | keys | recognized? | carries which of the four |
|---|---|---|---|
| `nvite` | 46 | no (ignored -> full payload) | n/a |
| `powerNvite` | 46 | no (ignored -> full payload) | n/a |
| `nviteDetails` | 46 | no (ignored -> full payload) | n/a |
| `inbox` | 7 | **YES** | `unreadPowerNvite`, `totalPowerNvite`, `mrt` |
| `inboxDetails` | 46 | no (ignored -> full payload) | n/a |
| `mail` | 46 | no (ignored -> full payload) | n/a |
| `mails` | 46 | no (ignored -> full payload) | n/a |
| `mailDetails` | 46 | no (ignored -> full payload) | n/a |
| `mostRelevantMail` | 46 | no (ignored -> full payload) | n/a |
| `recruiterMails` | 46 | no (ignored -> full payload) | n/a |
| `communication` | 46 | no (ignored -> full payload) | n/a |
| `messages` | 46 | no (ignored -> full payload) | n/a |
| `messageDetails` | 46 | no (ignored -> full payload) | n/a |
| `jobseekerMail` | 46 | no (ignored -> full payload) | n/a |
| `unreadMail` | 46 | no (ignored -> full payload) | n/a |
| `mailCount` | 46 | no (ignored -> full payload) | n/a |
| `notification` | 46 | no (ignored -> full payload) | n/a |
| `notifications` | 46 | no (ignored -> full payload) | n/a |
| `careerProfile` | 46 | no (ignored -> full payload) | n/a |
| `desiredProfile` | 46 | no (ignored -> full payload) | n/a |
| `preferences` | 7 | **YES** | none |
| `jobPreferences` | 46 | no (ignored -> full payload) | n/a |
| `desiredJobDetails` | 46 | no (ignored -> full payload) | n/a |
| `cvInfo` | 4 | **YES** | none |
| `resumeDetails` | 46 | no (ignored -> full payload) | n/a |
| `assessments` | 3 | **YES** *(control)* | none |
| `campusData` | 3 | **YES** *(control)* | none |
| `incompleteSection` | 3 | **YES** *(control)* | none |
| `userDetails` | 21 | **YES** *(control)* | none |
| `profilePerformance` | 7 | **YES** *(control)* | none |
| `aiInterviewEligibility` | 3 | **YES** *(control)* | none |

Recognized: **9 of 31** -- `inbox`, `preferences`, `cvInfo`, `assessments`, `campusData`, `incompleteSection`, `userDetails`, `profilePerformance`, `aiInterviewEligibility`.
Unrecognized: 22 of 31.

## Answer to the question the sweep was run to settle

**One name, `inbox`, is recognized and carries the target fields -- and it carries three of
the four, not four.**

`?properties=inbox` returns 7 keys: the 3-key envelope plus `mr`, `mrt`, `unreadPowerNvite`,
`totalPowerNvite`. So adding `inbox` to the production property list would recover `mrt`,
`unreadPowerNvite` and `totalPowerNvite` (and `mr` as a bonus -- also currently lost).

**`unreadMostRelevantMail` was recovered by NO name in this sweep.** Fourteen mail- and
message-shaped guesses (`mail`, `mails`, `mailDetails`, `mostRelevantMail`, `recruiterMails`,
`unreadMail`, `mailCount`, `jobseekerMail`, `messages`, `messageDetails`, `communication`,
`notification`, `notifications`, `inboxDetails`) were all ignored. On this evidence the only
route that returns `unreadMostRelevantMail` is the BARE request. Widening the narrow call
with `inbox` is therefore a partial fix, not a complete one; dropping the parameter entirely
remains the only measured way to get all four.

Two smaller findings worth carrying:

- `assessments`, `campusData` and `incompleteSection` are recognized but contribute NOTHING
  for this account -- they narrow to the bare 3-key envelope. Recognized-but-empty is not the
  same as unrecognized, and only the key-set test distinguishes them.
- `eligibleFlagForAIMockInterview` reads `false` in every narrowed response EXCEPT
  `?properties=aiInterviewEligibility` (and BARE), where it reads `true`. The envelope copy
  is a default, not a real value -- do not read it from a narrowed response that did not ask
  for that property. PRODUCTION does ask for it, and reads `true`.

## Files

- `_sweep/dashboard-property-sweep.md` -- this write-up
- `_sweep/dashboard-payloads.json` -- per-request key lists, key counts, serialized sizes, and
  the four target-field values for BARE and PRODUCTION. Payload values are deliberately NOT
  duplicated: the response carries personal data.
