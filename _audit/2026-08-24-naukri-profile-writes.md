# naukri profile-section writes - 2026-08-24

**Commits** `c0f332d` (transport), `92c17f7` (rails), `412c3eb` (ten sections),
`6de0d10` (boost), `11446b0` (four defects), `cdb8c18` (audit), `159b266` (PII).
**Local suite 2979 -> 3214 passed, 8 deselected, zero failures.**
**CI run `32705833680` is a BILLING BLOCK, not a code failure** - see section 7.

---

## 1. The premise was wrong, and that is the finding

The brief said: *"The REST channel is proven: `_boost_visibility` already POSTs live to the same
`fullprofiles` route."* It is not proven. It never was.

Three measurements, none of which required a write:

1. **The repo aims at a route the editor never uses.** Naukri's own shipped profile editor is
   `mnj_v320.min.js` (848718 bytes, `/s/5/105/j/`). Every profile-section save in it goes to the
   **v1** fullprofiles route, declared at byte offset 44671. Counted over the bundle: **1** v1
   fullprofiles reference, **0** v0, **0** v2. The repo's `FULLPROFILES_API` is **v0**, and
   `_boost_visibility`'s version loop tried `("v0", "v2")` - so it never tried the only version that
   exists.
2. **It omits a header every real write carries.** The wire method is `POST` plus
   `X-HTTP-Method-Override: PUT`. Measured across the bundle: **21** override-PUT sites (matching the
   21 write call sites) and **12** override-DELETE sites (matching the 12 per-row delete routes).
   The repo sent no override header at all.
3. **Nothing in the records shows a boost ever succeeding.** `event_log` spans 2026-08-20 to now with
   **7624** rows and holds **0** `ProfileBoosted` and **0** `ProfileUpdated`. `scheduled_runs` holds
   **0** `boost_profile` rows.

The belief traces to one comment, `profile_update.py:608`: *"The fullprofiles endpoint rejects all
external API calls (405 from CDN)."* That generalises a **405 on GET** into a verdict about POST. A
405 on GET says GET is not allowed and nothing more. `probing/discovery-report.md:151` repeated it.
Both are corrected.

Honest limit on the 405: a live GET to all three versions returns 405 with **no `Allow` header** and
a **byte-identical 92-byte body**, so the 405 does not even establish which versions are routed. The
`Allow` header was the cheap instrument this wave expected to use; it does not exist here. The bundle
is what settled the contract.

---

## 2. What is now writable

`naukri_update_profile_section(section, fields, confirm=False)` - **16 sections**, up from 5 fields.

| kind | sections | payload |
|---|---|---|
| SCALAR_BLOCK | `summary`, `resumeHeadline`, `keySkills`, `careerPreferences` | `{"profile": {...}}` |
| SINGLE_ROW | `employments`, `educations`, `schools`, `itskills`, `projects`, `certifications` | one-element array; other rows untouched |
| WHOLE_LIST | `languages`, `onlineProfiles`, `workSamples`, `presentations`, `publications`, `patents` | the **entire** list |

Plus `naukri_list_profile_snapshots` and `naukri_restore_profile_section`. Tool count **125 -> 128**,
measured off a fresh registry import.

**The WHOLE_LIST distinction is the whole safety story.** Naukri's editor puts every row of those six
collections on the wire on every save, so a payload carrying one row DELETES the rest - and the API
still answers 200. The orchestrator reads the live list and merges the caller's row into it. Test
control 3 is the guard: five live rows, one edited, and the assertion is that five rows reach the
wire with four byte-identical to what was read.

**DELTA, not FULL-OBJECT.** An omitted key is not a deletion. Settled by the `strip`/`isDelete` pair
in the editor's deletion tracker: `strip` removes empty keys from the body outright, while `isDelete`
separately names fields to clear. Under FULL-OBJECT semantics `strip` would destroy data on every
blank input and `isDelete` would be unreachable. Both, plus 12 dedicated per-row DELETE routes, are
coherent only under merge. This is **client intent**, not observed server behaviour - see section 6.

---

## 3. Nothing can report a success it did not measure

Every confirmed write: read before -> snapshot -> build -> POST -> **read after** -> `verify_write` +
`rows_lost`.

- `verified` is reported off the **second read**, never off the 200.
- Not persisted -> `NOT_PERSISTED`, naming the snapshot.
- Persisted but something else moved -> `COLLATERAL_CHANGE`, naming the snapshot and the lost rows.
- `confirm=False` is the default and writes nothing; a test pins that the preview body and the
  confirmed body are **identical**, so reading the preview is worth something.

Two guards exist for failures that would otherwise be **silent**, not loud:
- A controlled-vocabulary field passed as free text is accepted by the API and then ignored. Refused.
- All four scalar sections share the `profile` envelope, so nothing in the payload distinguishes
  them. Without `SCALAR_ALLOWED_FIELDS`, `write_section("summary", {"resumeHeadline": ...})` would
  rewrite the headline, snapshot it under the wrong label, and verify clean.

A measured asymmetry that would have broken a naive implementation: the READ payload spells five
collections **singular** (`onlineProfile`, `patent`, `presentation`, `publication`, `workSample`)
while the WRITE payload spells them **plural**. Same section, two names, depending on direction.

**Restore is deliberately narrow.** Scalar and whole-list sections restore in one call. The six
single-row collections return `unsupported` and NAME the differing row ids, because `write_section`
sends one row per call and a partial restore reporting success is worse than a refusal. A row deleted
on Naukri cannot be recreated with its original id, and the tool says so.

---

## 4. Three defects in my own orchestrator, caught before freeze

Both test slices found them independently and converged:

1. Every error payload read `snapshot["name"]`, but `save_snapshot` returns the filename under
   `"file"`. So all three error paths rendered **"restore from snapshot None"** - at the one moment a
   restore is needed. Four sites.
2. The whole-list refusal named the id field where the collection belonged.
3. An unknown section **raised** where every sibling guard returns a structured error.

The two strict `xfail`s pinning defect 1 went **XPASS the moment it was fixed**, which is what forced
the markers off rather than leaving them behind. They remain as plain regression tests.

---

## 5. The daily boost has never once run, and it is not the REST bug

`boost_profile` is registered daily with `run_at_hour=9` and no `catch_up`. `prime_schedule` seeds
the interval clock to **process start** for non-catch_up tasks (`scheduler.py:186`;
`prime_schedule_from_db` consults history only for catch_up, `:212`). With `INTERVAL_DAILY` plus the
hour gate at `:259`, a boost needs **25-47 hours of continuous uptime**. The longest process lifetime
in four days of `scheduled_runs` is **23.47 hours**, across **19** restarts. The window has never
opened.

**NOT ARMED, deliberately.** The fix is to let `boost_profile` prime from history like catch_up tasks
do - the hour gate would still hold it to one 9am firing per day, so it does not become "writes on
every server start", which the header comment at `:20-35` rightly refuses. But it arms a recurring
write against a live profile. That is the operator's call.

---

## 6. THE ONE THING STILL UNPROVEN, and the exact call that settles it

**No write was fired against the live account. Zero. So persistence is UNVERIFIED.**

Everything above is derived from Naukri's own client code plus live READS. What no amount of reading
can establish is whether the server merges as the client assumes. One write settles it.

**Two preconditions:**
1. **The running server (PID 43228) is built from `1bc5552` and predates all of this.** None of the
   new tools exist in it. It must be restarted first.
2. The section layer is untested against a live response body. Expect the first call to teach us
   something.

**The safest first probe** - `resumeHeadline`, toggling a single trailing period:

```
naukri_update_profile_section(
    section="resumeHeadline",
    fields={"resumeHeadline": "<current headline>."},   # confirm=False first
)
```

Read the previewed body, then re-run with `confirm=True`. It is the right probe because it is a real
byte-level delta (so persistence is provable, which an idempotent re-save is not), it is
cosmetically invisible to a recruiter, it reverses in one call, and it is exactly what the boost
already does. The reply carries `verified`, `diff`, `collateral` and `snapshot`.

**What each outcome means:**
- `status: "updated"`, `verified: true` -> the contract holds; the remaining 15 sections are the same
  transport with a different envelope.
- `NOT_PERSISTED` -> the route or header is wrong for writes even though the editor uses it. The
  snapshot is untouched and nothing is lost.
- `COLLATERAL_CHANGE` -> stop and restore from the named snapshot before anything else.

Do the scalar sections first, `employments` last: it is the section with the most rows and the most
to lose.

---

## 7. CI is billing-blocked, and that is not a code result

Run **`32705833680`** on `159b266`: all three matrix jobs `failure`, **0 steps, 2 seconds**. The
check-run annotation reads: *"The job was not started because recent account payments have failed or
your spending limit needs to be increased."* The repo is **private**, so Actions minutes are metered.

Last green run: **`c3550d3`, 2026-08-23T11:54Z**. The block arose between that and this wave and has
nothing to do with the code. It needs a billing action on the operator's account; nothing here
touched it. Local evidence in its place: **3214 passed, 8 deselected, 0 failed**, plus a
CI-shaped replication in `_audit/_slices/naukri-ci-local-replication.md`.

---

## 8. Not built, and why

**Auto-apply consent - REFUSED, and it is the right refusal.** It hands applying to Naukri's own
system, which runs **outside** the kill switch, outside `MIN_AGENT_FIT_FLOOR`, and outside the
approval cycle. Every control this server has for keeping applying deliberate is bypassed by it in
one setting. The value it offers - more applications - is precisely the thing the controls exist to
meter. Building it would not add a feature; it would create a second, ungoverned apply path beside
the governed one.

**Tier-C agent-arming keys - stay refused.** The config layer refuses them by design and that refusal
is load-bearing: it is what stops a config edit from arming the agent. A refusal that can be
configured away is not one.

**AmbitionBox community endpoints - build 2 of 44, not 36.** No probing report names 36 of anything:
`community-services` is literally **4** endpoints, the canonical dedup table is **42**, the union is
**44**, and the reports' own self-totals disagree with each other (35+, 40+, 42). Of 44: **15**
duplicate what `naukri_company_intel` or `naukri_research_company` already ship, **27** work but
change no decision a job seeker would make, **0** are dead, **2** are worth building
(`review/popular/{id}?jobProfile=` and `review/filters/{id}`). If the question was strictly the
community gateway, the answer is **build nothing**. Volume was not value here.

---

## 9. Also found

- **The daily brief told him nothing was waiting while eleven recruiters were.** Measured live:
  `unread_only=True` returns `count=0, unread=11, messages=[]`. The REST inbox endpoint has no
  unread filter, so the fetch filters client-side and empties the page while still reporting
  `unread: 11`. The fetch SUCCEEDS, so the "a failed fetch is None, never a zero" guard never
  engaged. Fixed.
- **`naukri_interview_prep` was permanently dead** - `ImportError: cannot import name
  'naukri_mock_interview'`, residue from the dispatcher de-consolidation. Fixed and pinned by an
  `ast` walk over every lazily-imported tool name.
- **The AmbitionBox REST bridge sent no auth headers at all.** The gateway answers 400 without
  `appid`/`systemid`. Headers added. Two corrections to the report that raised it: the live failure
  is **403**, not 400, so it IS inside the existing retry branch; and AB traffic was stopped on that
  403 per the pacing rule, so whether headers alone clear it is **unverified**.
- **`naukri_notification_count` is NOT broken** - a raw GET returns `{"count": 8}` and the tool
  returns 8. The sweep's row for it was derived, not probed. The latent silent-zero hazard it named
  was real and is fixed.
- **A live `profileId` was sitting in a tracked file** (`probing/profile-perf-settings-report.md`)
  and survived the 2026-08-23 PII scrub - it reads like an opaque hash until you notice it is the
  account's primary key on every write route. Redacted in the working tree. **It is also in history
  at `7b3b2e1`**; the repo is private, so a second history rewrite was not taken unilaterally. If
  this repo is ever flipped public, that must be scrubbed first.

## 10. Sweep

**83 of 125 tools called and measured**, up from 34 on the expired-session run, **zero auth-blocked**.
42 remain unmeasured, each with a reason: 24 never-call, 14 write-unsafe, 2 operator-gated, 2
argument-blocked. `spec.py` reconciles exactly at 83 + 42 = 125.

**A read-only sweep cannot close a write.** The **38** write-capable tools remain unverified by
construction, and the report says so rather than implying coverage. (The brief's "47" is not
supported by `write_paths.md`, which is a *read-path* audit; the counted figure is 38.)
`final_verdicts.py` had the logged-out run's OVERRIDE table hardcoded and would have manufactured
false verdicts against live data; it now selects by a flag derived from the data.
