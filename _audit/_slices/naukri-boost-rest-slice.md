# Boost REST path: pointed at a route that does not exist

Slice deliverable. Repo `naukri`, branch master, HEAD `412c3eb`.
Date 2026-08-24. Strict ASCII. No personal data: no real headline text, no
`profileId`. ZERO writes fired against naukri.com -- build and unit-test only.
The live MCP server (PID 43228) was neither restarted nor killed; the only
contact with it was a READ-ONLY sqlite open of `naukri.db`
(`file:naukri.db?mode=ro`).

---

## 1. Files changed

| File | Lines | What |
|---|---|---|
| `naukri_server/tools/profile_update.py` | 9-14 | dropped the now-dead `FULLPROFILES_API` import; added `from naukri_server.tools.profile_write import fullprofiles_write` (line 14), matching `profile_sections.py`'s top-level import of the same chokepoint |
| `naukri_server/tools/profile_update.py` | 609-625 | Task 2: replaced the 405-on-GET comment (was 608-610) |
| `naukri_server/tools/profile_update.py` | 742-784 | Task 1: Strategy 1 rewritten onto `fullprofiles_write` |
| `probing/discovery-report.md` | 151 | Task 2: corrected the same misreading (see OWNERSHIP NOTE below) |
| `tests/test_boost_rest_path.py` | new, 320 lines | the 6 controls |
| `naukri_server/scheduler_tasks.py` | UNCHANGED | Task 3 found no defect -- see section 5 |

Post-change `naukri_server/tools/profile_update.py`
sha256 = `b0ddd2aae561d95db0cad9778f3d3b77ce964ba1a2333e5108f8eb7fd0167a58`

Line endings verified with `git ls-files --eol`, not by eye:
`profile_update.py` w/lf (unchanged), `tests/test_boost_rest_path.py` pure LF
(0 CRLF, 320 LF), `probing/discovery-report.md` w/crlf preserved byte-for-byte
(419 CRLF terminators before and after, 0 bare LF introduced). All three edits
were applied as binary-mode byte replacements with a uniqueness assertion per
anchor, precisely so no line ending could drift.

Every byte I wrote is ASCII. The file still holds 6 non-ASCII bytes, all
pre-existing and none of them mine: two em-dashes, at line 1 (module docstring)
and line 546 (a JS comment). Left alone as out of scope.

### OWNERSHIP NOTE -- please read

The brief's "Files you own" section named three files and said "edit only
these three". Task 2 then explicitly directed: "Also correct
`probing/discovery-report.md:151` if it repeats the same misreading". It does
repeat it, so I made that one-line correction and am flagging the tension
rather than silently resolving it. `discovery-report.md` is not on the frozen
list, no other agent's edits appeared in it, and the change is a single
table cell. Revert it if the ownership line was meant strictly.

---

## 2. Task 1 -- `_boost_visibility` Strategy 1

The old loop (`profile_update.py:737-758` at `412c3eb`) built its endpoint from
`FULLPROFILES_API` for `("v0", "v2")` and sent no override header. Neither is a
fullprofiles write route, so it could never have succeeded; it always fell
through to the browser path.

Now (`profile_update.py:764`):

```python
await fullprofiles_write({"profile": {"resumeHeadline": headline}})
```

Called with the section body ALONE. `profile_write.fullprofiles_write` owns the
v1 route, the `X-HTTP-Method-Override: PUT` header, and the resolution,
validation and top-level placement of `profileId`. None of the three is
re-typed at this call site, which is the point of control 1.

Preserved exactly as required:

* The browser fallback (`profile_update.py:785` onward) is untouched -- not a
  line changed. It remains the only route with any chance of having worked.
* The return shape is unchanged: the same five keys
  `{status, action, method, headline_length, message}`. Pinned by an assertion
  in control 1's test.
* `ProfileBoosted` is still emitted, with `method="rest_fullprofiles_v1"` --
  honest about the route actually used, where the old `rest_api_v0` /
  `rest_api_v2` named versions that were never write routes.

**Observation for the lead, not acted on.** The boost now performs TWO
identical `PROFILE_API` reads: one in `_boost_visibility` to get the headline,
and one inside `fullprofiles_write` -> `_get_profile_id()`. Passing
`profile_id=profiles[0].get("profileId")` would collapse them, but the brief
specified the one-argument call literally, so I did not deviate. It is a
round-trip, not a correctness issue.

**Stale doc-comment, not mine to edit.** `naukri_server/events.py:134` still
documents the example method value as `"rest_api_v0"`. One word, in a file
outside my slice.

---

## 3. Task 2 -- the comment that caused this

`profile_update.py:608-610` read:

> The fullprofiles endpoint rejects all external API calls (405 from CDN).
> Only the React app's own XHR works.

That generalises a 405 on GET into a verdict about POST. Replaced at
`profile_update.py:609-625` with what is measured, and only that:

* Naukri's shipped editor bundle `mnj_v320.min.js` (848718 bytes) writes to the
  fullprofiles **v1** route at bundle offset 44671, as a POST carrying
  `X-HTTP-Method-Override: PUT`. So an external API call is not refused as
  such.
* What the 405 actually shows: a live GET to all three fullprofiles versions
  returns 405 with **no `Allow` header** and a **byte-identical 92-byte body**.
  It says only that GET is not allowed. The comment explicitly states this does
  not even prove which versions are routed, and says nothing about POST. No
  claim beyond that is written.
* Why this function still uses the browser: its five fields are modal form
  edits (noticePeriod / currentCtc / expectedCtc share one Career Profile
  modal) -- not because the REST route is closed.

### `probing/discovery-report.md:151` -- checked, and it did repeat it

Found verbatim: *"Returns \"Method Not Allowed\" on GET -- confirms it requires
POST"*. Same misreading, same direction. Corrected in place, marked
`CORRECTED 2026-08-24`, naming the no-`Allow`-header / 92-byte-body measurement
and the real v1 write route. The row's `405` status and the endpoint path are
unchanged -- only the interpretation was wrong.

---

## 4. The 6 controls -- `tests/test_boost_rest_path.py`

11 test functions, 11 passing. Transport mocked, `api_client` replaced by a
recorder, browser faked: zero network, zero browser, zero file I/O.

Each control was shown FAILING first, **one deliberate mutation at a time**.
After every mutation the source was restored from pristine bytes and the
SHA-256 re-verified. All six restores returned
`b0ddd2aae561d95db0cad9778f3d3b77ce964ba1a2333e5108f8eb7fd0167a58`, and the
drill reported `controls that failed to go red: 0`.

| # | Control | Mutation | Status |
|---|---|---|---|
| 1 | boost calls `fullprofiles_write` | bypass the chokepoint, hand-roll the POST | RED then GREEN |
| 2 | payload is the headline re-sent unchanged | append `" (boosted)"` to the headline | RED then GREEN |
| 3 | no v0/v2 fullprofiles path | restore the original `("v0","v2")` loop | RED then GREEN |
| 4 | REST failure falls back to browser | delete the browser fallback | RED then GREEN |
| 5 | `ProfileBoosted` with the new method | emit `"rest_api_v0"` instead | RED then GREEN |
| 6 | empty/missing headline sends no write | drop the `if headline:` guard | RED then GREEN |

### RED messages, verbatim

**Control 1** -- `TestBoostUsesTheWriteChokepoint::test_boost_calls_fullprofiles_write_exactly_once`

```
E   AssertionError: the boost must reach the wire through fullprofiles_write -- it is the only place that owns the v1 route and the X-HTTP-Method-Override: PUT header. Transport calls: []
```

**Control 2** -- `TestBoostPayload::test_payload_is_the_headline_resent_unchanged`

```
E   AssertionError: a boost re-sends the headline it just read, unchanged, inside the `profile` envelope. Got: {'profile': {'resumeHeadline': 'Fake Headline For Test Only (boosted)'}}
```

**Control 3** -- `TestNoLegacyFullprofilesVersions::test_no_outgoing_call_carries_a_v0_or_v2_fullprofiles_path`

```
E   AssertionError: the boost reached a v0/v2 fullprofiles path. Neither is a write route for the profile editor; that loop is why zero boosts ever landed. Offending paths: ['/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/fullprofiles', '/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self/fullprofiles']
```

Control 3 carries a second, independently mutated assertion --
`test_profile_update_no_longer_references_the_v0_read_constant`, a tripwire on
`hasattr(profile_update, "FULLPROFILES_API")`, since re-importing that constant
here is exactly how the bug returns. Shown RED under a module-level re-import:

```
E   AssertionError: profile_update imported FULLPROFILES_API again -- that constant is the v0 READ path and re-importing it here is how the v0/v2 write loop gets re-introduced. Writes go through profile_write.
```

**Control 4** -- `TestBrowserFallbackSurvives::test_rest_failure_still_falls_back_to_the_browser`

```
E   AssertionError: a REST failure must fall back to the browser, not surface an error. Got: {'status': 'error', 'message': 'REST failed', 'error_code': 'API_ERROR'}
```

**Control 5** -- `TestProfileBoostedEvent::test_rest_success_emits_profileboosted_with_the_new_method`

```
E   AssertionError: the event's method must name the route actually used. Expected 'rest_fullprofiles_v1', got 'rest_api_v0'
```

**Control 6** -- `TestEmptyHeadlineSendsNoWrite::test_no_headline_means_no_write[empty-string]`

```
E   AssertionError: with no headline to re-save there is nothing to write -- sending an empty resumeHeadline would blank the live profile. Transport calls: [({'profile': {'resumeHeadline': ''}}, None)]
```

Control 6 is parametrized over four read shapes: `empty-string`,
`key-missing`, `no-profile-row`, `empty-response`.

### EMIT_CENSUS -- no action needed

`tests/test_read_path_purity.py` passes untouched. No emit was added at a NEW
call site: both `ProfileBoosted` emits still sit inside `_boost_visibility`, and
the census entry `("tools/profile_update.py", "_boost_visibility",
"ProfileBoosted")` (line 120) already covers them. The census is a set of
`(file, function, event)` tuples, so moving an emit within the same function
does not change it. Verified green, not assumed.

---

## 5. Task 3 -- why `boost_profile` has never run

**VERDICT: no defect. Do not change the scheduling behaviour.** Zero rows are
fully explained by the hour gate plus the interval clock plus measured process
lifetime. `scheduler_tasks.py` is unchanged.

### The mechanism

`boost_profile` is registered at `scheduler_tasks.py:137-141` with
`interval_seconds=INTERVAL_DAILY` (86400), `run_at_hour=9`, no `catch_up`.
Two INDEPENDENT gates must both pass at the SAME wake:

1. **The interval clock is primed to PROCESS START, not to persisted history.**
   `TaskScheduler.prime_schedule` (`scheduler.py:172-186`) seeds
   `self._last_started[name] = now` for every non-`catch_up` task.
   `prime_schedule_from_db` (`scheduler.py:204-213`) consults `get_last_run`
   only for `catch_up` tasks -- `if not task.catch_up: continue` at line 212.
   So on every restart, `boost_profile`'s clock resets to zero.

2. **`is_due` then demands a full 86400s of that clock**
   (`scheduler.py:159-170`, gate applied at `scheduler.py:262`).

3. **The hour gate** (`scheduler.py:259`) skips the task at any wake where the
   IST hour is not 9. The `>= 86400` bucket wakes at most hourly
   (`scheduler.py:243-248`, `sleep(min(sleep_time, 3600))`), so exactly ONE
   wake per day can clear it.

Both together: the first eligible firing is the first 09:00 IST wake that is at
least 24h after process start. That is **25h of continuous uptime if the
process starts before 08:00 IST, and up to 47h if it starts just after 09:00**.

### The measurement

Read-only from `naukri.db`. `scheduled_runs` holds 1125 rows, 2026-08-20T06:56
to 2026-08-24T07:22 UTC:

| task | interval | rows |
|---|---|---|
| t2_verify_pending | 5 min | 928 |
| reminder_check | 1h | 76 |
| notification_digest | 2h | 40 |
| agent_cycle | 2h | 33 |
| sync_saved_jobs | 4h | 20 |
| sync_applications | 4h | 14 |
| stale_check | 6h | 12 |
| api_discovery | weekly | 2 |
| **daily_brief** | **daily** | **0** |
| **boost_profile** | **daily** | **0** |
| **retention_sweep** | **daily** | **0** |

The split is exactly at the daily boundary. Every task with an interval of 6h
or less has rows; all three `INTERVAL_DAILY` tasks have none -- including
`retention_sweep`, which has NO `run_at_hour` at all. A task with no hour gate
and zero rows rules the hour gate out as the sole cause and points at the
24h interval clock, which is common to all three.

Process lifetimes were derived from `t2_verify_pending`'s 300s cadence: any
inter-row gap outside 270-330s is a loop restart. That yields **19 process
starts in 4 days**, and:

```
LONGEST CONTINUOUS UPTIME: 23.47 h   (2026-08-23 13:29 -> 2026-08-24 12:57 IST)
```

**No process has ever lived 24 hours.** `is_due` has therefore never returned
True for any daily task. Zero rows is fully explained.

A methodological note, because it changed the answer: a first pass using a
coarser 15-minute gap threshold reported a 28.90h segment, which would have
made this a REAL BUG (the gates would have been satisfiable and still nothing
fired). The finer phase analysis showed that segment actually breaks at 10:59
and 13:25 on 08-23. The premise was wrong, not the code.

### Confirmed by simulation, not by reading

A virtual-clock harness drove `TaskScheduler._run_interval(86400, ...)` with
the three real daily tasks, patching `time.time`, `datetime.now` and
`asyncio.sleep`, executions stubbed. No network, no DB.

* Real current process start (2026-08-23 13:29:23 IST), 22.5h simulated:
  **FIRINGS: 0** -- matching the live record exactly.
* Same start, 47.5h simulated: `retention_sweep` 08-24 14:00,
  `daily_brief` 08-25 08:00, **`boost_profile` 08-25 09:00 IST**.

So the code fires correctly the moment the process lives long enough. The
current process would need to survive to 2026-08-25 09:00 IST -- 43.5h of
continuous uptime -- for the first boost to land.

### Left for the lead

Not done, deliberately. Flipping `catch_up` on for a task that writes to the
live profile is the lead's call, and the header comment at
`scheduler_tasks.py:15-35` already argues against it.

Worth naming because it is a genuinely DIFFERENT lever than `catch_up`:
`prime_schedule_from_db` could seed the interval clock from persisted history
for ALL tasks, not just `catch_up` ones (`scheduler.py:212`). That would make
the clock survive restarts without meaning "runs on every server start" -- the
specific property the header comment objects to. It would, however, make
`boost_profile` due at the very next 09:00 IST wake after any restart, which
for a live-profile write is still a policy decision, not an implementer's.
Also note it would change behaviour for `daily_brief`, `retention_sweep`,
`sync_applications`, `agent_cycle` and `api_discovery` simultaneously, since
they share the same code path.

---

## 6. Suite

```
venv\Scripts\python.exe -m pytest tests/ -q
3 failed, 3184 passed, 8 deselected in 277.42s (0:04:37)
```

Baseline was 3158 passed / 8 deselected / 0 failed.

**The baseline no longer describes this tree.** Three other agents' untracked
test files and four modified source files landed during this slice
(`naukri_server/services/application_service.py`,
`services/daily_brief_service.py`, `services/notification_service.py`,
`tools/daily_brief.py`, plus `tests/test_interview_prep_call_sites.py`,
`tests/test_notification_count_honesty.py`,
`tests/test_unread_count_honesty.py`). Collected count moved 3166 -> 3202, and
the +36 is fully accounted for: 11 (mine) + 7 + 8 + 10 (theirs).

### The 3 failures, each attributed

**1. `test_profile_deep.py::TestProfileInvalidationOnUpdate::test_profile_invalidation_on_update` -- MINE.**

```
E   AssertionError: assert 'error' == 'success'
WARNING naukri:profile_update.py:783 REST refresh failed, falling back to browser:
        Profile read returned profile[0] with no 'profileId' key -- cannot resolve profileId.
```

Cause, and it is the intended contract change rather than a bug: routing the
boost through the chokepoint means the boost now REQUIRES a validated
`profileId`, because `fullprofiles_write` refuses to write without one by
design. That test's fixture (line ~274) mocks the profile read as
`{"profile": [{"resumeHeadline": "My Headline"}]}` -- no `profileId` -- so
`_get_profile_id()` raises, the REST path falls back to the browser, the
browser is not mocked in that test, and the result is an error. The fixture
predates the contract. On the live account the top-level `profileId` is a
64-character string, so this does not affect production.

I do not own `tests/test_profile_deep.py` and did not touch it. **The fix is
one line**, adding the key to that same fixture row:

```python
mock_api_get.return_value = {
    "profile": [{"resumeHeadline": "My Headline", "profileId": "p" * 64}],
}
```

VERIFIED BY INSTRUMENT, not guessed: a scratch reproduction outside the repo
ran the identical scenario both ways. Without the key, `status == 'error'`.
With it: `status = success`, `action = refreshed`,
`method = rest_fullprofiles_v1`, and `_profile_ttl_cache._data is None` -- all
three of that test's assertions pass. The test's existing
`patch(...api_client.post)` still covers the write, because as its own
docstring notes the `api_client` singleton is shared across both modules.

**2 and 3. `test_suite_integrity.py::TestEveryModuleContributes` (both tests) -- NOT MINE.**

```
E   AssertionError: test module(s) on disk contributed ZERO collected tests. Either they are
    broken or they are deselected - both make the suite smaller than it looks:
E       test_interview_prep_call_sites.py
```

`tests/test_interview_prep_call_sites.py` is another agent's untracked file,
created 13:15 today (this session began 12:47). It collects 7 tests standalone
but contributes 0 in the full run, which is what trips the integrity check. It
contains **zero** references to `profile_update`, `boost` or `fullprofiles`.

Shown, not asserted: `pytest tests/test_interview_prep_call_sites.py
tests/test_suite_integrity.py -q` gives **19 passed, 2 skipped** -- the
integrity check passes in isolation. It also passes in a 10-module run
alongside my whole slice (below).

### Every suite adjacent to my slice

```
pytest tests/test_boost_rest_path.py tests/test_profile_deep.py
       tests/test_profile_sections.py tests/test_profile_write_transport.py
       tests/test_profile_restore.py tests/test_read_path_purity.py
       tests/test_scheduler.py tests/test_scheduler_cadence.py
       tests/test_tier_registry.py tests/test_suite_integrity.py -q

1 failed, 286 passed, 2 skipped in 12.40s
```

The single failure is the attributed one above. `test_read_path_purity`
(EMIT_CENSUS), `test_profile_sections`, `test_profile_write_transport`,
`test_profile_restore`, `test_scheduler`, `test_scheduler_cadence` and
`test_suite_integrity` are all green.

### Net, once that one fixture line lands

Suite returns to 0 failures attributable to this slice: **3185 passed**, with
my 11 new tests included, and the two remaining `test_suite_integrity` failures
belonging to whoever owns `test_interview_prep_call_sites.py`.
