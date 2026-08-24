# Naukri MCP - end-to-end tool sweep against a LIVE session

Run date: 2026-08-24. Supersedes `2026-08-22-e2e-naukri.md`, which ran against an
EXPIRED session and could therefore measure only 34 of the tools it called.

No personal data appears below. Job ids used as probe arguments are written
`<probe-job-id>`; employer names, recruiter names, email, phone, CTC and resume
text are redacted to shape. Counts are kept, because the counts are the
measurement.

---

## 1. What this run did and did not do

| | |
|---|---|
| Live tools on `tools/list` | **125** |
| Tools CALLED and measured | **83** |
| Tools NOT called (safety SKIP) | **42** |
| Tools called that produced no information | **0** |
| Prior run (2026-08-22): tools that produced a real observation | 34 |
| This run: tools that produced a real observation | **83** |
| Net newly measured | **49** |

**A read-only sweep cannot close a write.** The 42 skipped tools are skipped
precisely because exercising them would apply to a job, spend daily quota,
rewrite the live profile, create or delete a job alert, mark messages read
upstream, run the autonomous agent, flip the kill switch, or change config.
Every one of them remains **UNVERIFIED**, and this run did not and could not
change that. Verifying a write requires performing the write. That is out of
scope here by explicit instruction, and nothing in this document should be read
as evidence that any write path works.

### Environment (verified, not assumed)

| Fact | Value | How it was established |
|---|---|---|
| Server process | pid 43228, `127.0.0.1:8321` | `Get-NetTCPConnection -LocalPort 8321 -State Listen` |
| Server build | `1bc55528dec1`, branch master, dirty=false | `naukri_server_info` |
| jobcore build | `6acc7e6c6949`, dirty=false | `naukri_server_info` |
| Repo HEAD | `c3550d3` | `git log`; diff vs `1bc5552` is markdown under `_audit/` and `probing/` only, so the code trees are identical |
| Surface reported by the server | `surface.tools = 125` | `naukri_server_info`, matching the live `tools/list` exactly |
| Auth at sweep start | `logged_in true, verified true, api_confirmed` | `naukri_auth_status` |
| Auth measured live | `authenticated: true` against `GET .../users/self/activityLevel` | `naukri_session_info(verify_live=True)` |

The server was neither restarted nor killed, and no browser was launched
directly.

### Backup taken before the run

Per the harness README (the sweep calls `daily_brief`, which marks pending
notifications delivered, and the sync tools, whose purge step deletes
applications past the retention horizon). Copied to
`mcp-servers/_audit/_data/naukri-db-backup-presweep/`:

| File | Bytes |
|---|---|
| `naukri.db` | 3,297,280 (sha1 `b24b4a6023ab...`, verified identical to source) |
| `naukri.db-wal` | absent at backup time |
| `naukri.db-shm` | absent at backup time |
| `applications.json` | 97,436 |
| `reminders.json` | 12,853 |
| `questions.json` | 5,484 |
| `agent_config.json` | 529 |
| `saved_jobs.json` | 515 |
| `early_access_tracking.json` | 387 |
| `sync_state.json` | 280 |
| `healing_state.json` | 42 |

The JSON state files were not required by the brief; they were copied because
the sync tools rewrite them.

**The purge deleted nothing.** `applications` was 162 before the sweep and 162
after. The only local writes attributed to tools were `sync_state.json`
(rewritten by `sync_applications`, `sync_saved`, `status_changes`) and
`early_access_tracking.json` (rewritten by `daily_brief`).

---

## 2. Spec reconciliation: 125 live vs 120 specced

The brief anticipated four new tools. There were **five**. `spec.py` covered
120 tools (80 CALL + 40 SKIP); the live server registers 125. No tool was
silently skipped: each newcomer was classified against the same standard the
spec already used, and the write path of each was read in source first.

| Tool | Call | Reason |
|---|---|---|
| `naukri_server_info` | **CALL** | Pure read. Returns frozen build stamps and a recomputed uptime; no network, no local write. Also the cheapest independent check that the process is not stale. |
| `naukri_session_info` | **CALL** (`verify_live=True`) | Read. `verify_live` issues one authenticated GET against the activity-level endpoint and mutates nothing. Its docstring states no cookie or token VALUE is ever returned. |
| `naukri_triage_inbox` | **CALL** (`limit=10`) | Read. `_triage_inbox` (`tools/inbox.py:366`) walks the same `_fetch_inbox` pages `naukri_list_inbox` uses - already a CALL-listed tool - and scores each row locally. It never calls `read_message` and never marks anything read upstream. |
| `naukri_logout` | **SKIP** | Writes local state: clears the cached `nauk_at` and deletes the exported `auth_state.json`. Calling it mid-sweep would destroy the exact authenticated state this run exists to measure. Same class as the spec's `R_WRITE`. |
| `naukri_reauth` | **SKIP** | Writes local state (re-mints and replaces the cached credential) and its `browser_restart` stage relaunches the persistent Chrome profile. Browser restarts are out of scope for this run, and a failed renew mid-sweep would invalidate every later measurement. |

After these additions `spec.py` reconciles exactly: **83 CALL + 42 SKIP = 125**,
with zero registered-but-unspecced tools and zero specced-but-unregistered
tools.

### One SKIP whose stated precondition has lapsed - reported, not acted on

`naukri_alert_detail` is skipped with the reason *"needs a real alert_id;
list_alerts is auth-blocked so none can be sourced. Never invent an id."* That
precondition no longer holds: in this run `naukri_list_alerts` succeeded and
returned 3 alerts with real ids, so a genuine id is now sourceable and
`alert_detail` is a read. **It was still not called.** Removing a SKIP was
outside this run's authority. It is the single highest-value candidate to
promote to CALL in the next sweep, and doing so would take the measured count
from 83 to 84.

By contrast `naukri_agent_decisions` keeps its SKIP legitimately: it needs a
real `cycle_id`, and `agent_runs` is still empty (0 rows), so the stated
precondition is unchanged.

---

## 3. Results

Auto-classification (`analyze.py`) over the 83 called: 48 `success-data`, 31
`success-scalar`, 4 `errored`. That classifier is blunt - it reads status codes
and payload shapes - so every non-clean result and every zero was then checked
by hand against `naukri.db` and against sibling tools, per README trap 3.

Final verdicts (`final_verdicts.py`), over all 125:

| Verdict | Count |
|---|---|
| correct | 78 |
| wrong-fields | 4 |
| empty-unclear | 1 |
| errored | 0 |
| unverified-auth-blocked | 0 |
| skipped (never called) | 42 |

Compare 2026-08-22 over 120: correct 22, wrong-fields 2, empty-unclear 2,
errored 8, **unverified-auth-blocked 46**, skipped 40. The 46 auth-blocked
tools are the gap this run closed, and the 8 `errored` there were all
`AttributeError: 'NoneType'.acquire` page_pool crashes that do not reproduce
now.

### A harness change was required to report this honestly

`final_verdicts.py` carried a hardcoded `OVERRIDE` table of the 2026-08-22
observations - including *"naukri_auth_status: logged_in false + reason
no_token"* and a family of *"page_pool AttributeError. FIXED"* entries. Applied
to live data those would have manufactured verdicts that contradict what was
actually measured. The table is now split into `OVERRIDE_20260822` (retained as
the record of that run, not applied) and `OVERRIDE_LIVE`, selected by a `RUN`
flag **derived from the data** (`results.json`'s observed `logged_in`), never
hardcoded, so a future re-run cannot silently inherit the wrong table. The
`assert len(out) == 120` was also changed to compare against the live spec size.
These are the only harness edits beyond the five spec additions.

---

## 4. Defects and surprises

### 4.1 `naukri_daily_brief` reports zero unread recruiter messages when there are 11

The most important finding of the run, and one only a live session could
produce.

```
naukri_daily_brief  ->  "unread_messages": {"count": 0, "messages": []}
naukri_list_inbox   ->  {"total": 62, "count": 5, "unread": 11, ...}
naukri_triage_inbox ->  {"total_in_inbox": 62, "unread_in_inbox": 11, ...}
```

All three in the same sweep, on the same account, minutes apart. The brief's
`errors[]` names exactly one failure, and it is not this one:

```
"errors": ["AB applied insights: ClientResponseError: 403, message='Forbidden',
 url='https://www.ambitionbox.com/servicegateway-ambitionbox/insights-services/v0/insights/niAppliedJobs'"]
```

Mechanism, verified by instrument rather than inferred. `daily_brief.py:171`
fetches the section with `_fetch_inbox(limit=5, unread_only=True)` and
`daily_brief.py:273` publishes `d.get("count", 0)`. Calling `naukri_list_inbox`
directly, twice, post-sweep:

```
unread_only=True   ->  total=62  count=0  unread=11  messages_len=0  has_more=True
unread_only=False  ->  total=62  count=5  unread=11  messages_len=5  has_more=True
```

So the `unread_only=True` page returns nothing while *its own response body*
reports `unread: 11`. That is a self-contradictory response, which is why both
`naukri_list_inbox` and `naukri_daily_brief` are marked `wrong-fields`.

Why the existing guard does not catch it: the 2026-08-22 fix installed
`_section(source, build)` so that *"A SECTION WHOSE FETCH FAILED IS None, NEVER
A ZERO"*. Here the fetch **succeeds** - it returns a well-formed dict - so the
None-guard never engages and the zero is published as a measurement. The
truthful field, `unread`, is sitting in the same payload and is not read.

The `_triage_inbox` implementation already documents this exact hazard in a
comment about *"a filtered `unread_only` page"* whose totals disagree with the
rows delivered, and defends against it. `daily_brief` does not.

Impact: `naukri_daily_brief` is the flagship one-call morning tool, and its
`recommended_actions` block is built from these sections. A user driving off it
sees zero recruiter messages needing an answer while 11 sit unread.

### 4.2 `naukri_notification_count` returns 0 while its siblings see 4 to 11

```
naukri_notification_count   -> {"status": "success", "count": 0}
naukri_notification_summary -> new_count=4; categories: appStatus count=11, rmj count=11
naukri_list_notifications   -> total=5, count=5, every row is_read=false
```

Mechanism: `notification_service.py:150-153` is

```python
async def get_notification_count() -> dict:
    data = await api_client.get(NOTIFICATION_COUNT_API)
    return {"status": "success", "count": safe_get(data, "count", default=0)}
```

`safe_get` is the project's anti-corruption-layer accessor and it supports
missing-field detection via `warn=True, field_name=...`. This call site uses
neither, so an absent or drifted `count` key becomes a confident `0` with
`status: "success"`, no log line, and no error channel. The endpoint is
`/cloudgateway-mynaukri/notification-center-services/v0/naukrinotificationcentre/user/self/count`.

Classified `wrong-fields` on the contradiction, which is measured. The
`default=0` line is the most likely mechanism; that part is derived from
reading the code, not from probing the raw endpoint, because probing it would
require `naukri_debug api_fetch`, which was out of scope for this run.

### 4.3 `naukri_interview_prep` has a permanently broken sub-fetch

Verbatim, from the tool's own `errors[]`:

```
"Mock topics: Mock topics: ImportError: cannot import name 'naukri_mock_interview'
 from 'naukri_server.tools.mock_interview' (naukri_server\tools\mock_interview.py)"
```

`application_service.py:261-262` still does

```python
from naukri_server.tools.mock_interview import naukri_mock_interview
return await naukri_mock_interview(action="topics")
```

That consolidated `action=`-style dispatcher no longer exists. The module now
exposes `naukri_mock_interview_topics`, `naukri_mock_interview_history`,
`naukri_start_mock_interview`, `naukri_mock_interview_prep` and
`naukri_answer_mock_interview` as separate tools. This is residue of the
deliberate reversal of the 26 consolidated action-parameter tools: the
de-consolidation removed the dispatcher and left this caller pointing at it.

It fails for every job, on every call, permanently - not transiently. It is
better behaved than 4.1 and 4.2 because it **is** surfaced in `errors[]` and the
tool honestly returns `partial_success` rather than `success`. Secondary nit
visible in the quote: the label is doubled, `"Mock topics: Mock topics: ..."`.

### 4.4 Things that looked like defects and are not

Recorded because each one cost a check, and a future run should not re-spend it.

- **`naukri_auth_status` returned `logged_in: false` immediately after the
  sweep** with `reason: "check_failed: NaukriAPIError http 503"`, while the
  credential was present and unexpired. This is the documented contract, not a
  bug: `auth_service.get_login_status` maps *anything that is not a 401/403 or
  an explicit denial* to `logged_in False, verified **False**`, and the
  docstring states `verified=False` means *"could NOT check"*, which is not the
  same claim as *"proven logged out"*. Three probes moments later all returned
  `logged_in true, verified true, api_confirmed`, so the 503 was a transient
  upstream blip. It occurred **after** the last tool call; the session held for
  the entire sweep. Worth one observation: `naukri_session_info` answers the
  same question with `authenticated: null` plus a `why_not`, which is harder to
  misread than `false` - a caller doing `if auth_status()["logged_in"]` treats
  an upstream 5xx as a logout.
- **`naukri_company_slug` -> `NOT_FOUND`**: *"Could not determine AmbitionBox
  slug for group_id '4058675'. The company page may not have AmbitionBox
  integration."* True and specific. Correct.
- **`naukri_mock_interview_topics` -> `API_ERROR`**: *"HTTP 400: No Role
  Activated for this user"*, with `http_status` preserved. That is Naukri
  refusing on account state, passed through honestly. Correct.
- **`naukri_read_message` -> `VALIDATION_ERROR`**: *"message_id, vcard_id, and
  unique_id are all required."* Called deliberately with no arguments to check
  it refuses rather than guesses. Correct. (Note carried forward from the prior
  audit: with real ids it mints a notification row per call. Not exercised.)
- **`naukri_salary_position` -> `NOT_FOUND`**: *"No salary data found in 162
  applications."* 162 matches `count(*)` on the applications table exactly.
  Correct.
- **`naukri_follow_up_priority` -> `partial_success`** naming *"Inbox fetch
  failed - recruiter messages unavailable"* in `errors[]`. The inbox answered
  normally 15 calls later, so this was transient, and the tool degraded
  honestly. Correct.
- **Scheduler noise, per README trap 2.** `db.scheduled_runs` and `db.event_log`
  incremented during `naukri_get_job` and `naukri_salary_benchmark`, and one
  notification row appeared during `salary_benchmark`. Row-level attribution
  shows it is `ProbeStateChanged`, minted by the health probe on its own clock.
  A count-level diff would have blamed `salary_benchmark` for a mutation it did
  not make.

### 4.5 One prior defect confirmed FIXED by live data

`naukri_recruiter_history` returned `total_companies = 115`, which equals
`count(distinct company)` on the applications table exactly. On 2026-08-22 it
returned 20 - a SQL `LIMIT 20` that had leaked into the total. The fix holds
against real data.

---

## 5. Verdict table - all 83 tools CALLED

Ordered by verdict, then name. `s` is wall-clock seconds for the call.

| # | Tool | Verdict | s | Evidence |
|---|------|---------|---|----------|
| 1 | `naukri_daily_brief` | wrong-fields | 2.56 | unread_messages.count=0 while list_inbox and triage_inbox both report unread=11 in the same sweep. Sourced from _fetch_inbox(unread_only=True), whose page returns count=0/messages=[] beside its own unread=11; the fetch SUCCEEDS so the 08-22 None-guard never engages, and errors[] names only the Ambit |
| 2 | `naukri_interview_prep` | wrong-fields | 24.48 | partial_success; errors[] carries ImportError: cannot import name 'naukri_mock_interview'. application_service.py:261 still calls the consolidated dispatcher that the de-consolidation removed, so the mock-topics section can never populate for any job. Surfaced, not silent -- and the label is doubled |
| 3 | `naukri_list_inbox` | wrong-fields | 0.13 | unread_only=True returns count=0/messages=[] while the SAME response says unread=11 and total=62; unread_only=False returns count=5. Measured twice, post-sweep |
| 4 | `naukri_notification_count` | wrong-fields | 0.1 | count=0 while notification_summary reports new_count=4 (appStatus 11, rmj 11) and list_notifications returns 5 rows all is_read=false. safe_get(data,'count',default=0) is called without warn/field_name, so a missing or drifted key becomes a confident 0 |
| 5 | `naukri_score_saved_jobs` | empty-unclear | 0.15 | total_saved=2, scored_count=0, scored_jobs=[] and no channel saying why neither scored. Both saved rows are TEST DATA (J600/J700), so this is likely correct behaviour reported without its reason |
| 6 | `naukri_activity_level` | correct | 0.09 | scalars: {"level": "HIGH", "logged_in": true, "resume_updated": true, "profile_updated": true} |
| 7 | `naukri_agent_config` | correct | 0.01 | config.searches=1 |
| 8 | `naukri_agent_history` | correct | 0.09 | total=0 == DB agent_runs (empty) -- corroborated empty |
| 9 | `naukri_agent_status` | correct | 0.04 | scalars: {"enabled": false, "mode": "dry_run", "max_daily": 15, "min_fit_score": 70, "searches": 1, "blocklist_companie |
| 10 | `naukri_application_insights` | correct | 0.05 | top_companies aggregated over the 162 stored applications |
| 11 | `naukri_assess_fit` | correct | 9.35 | returned a fit verdict with explanation for the probe job; applied=false |
| 12 | `naukri_audit_profile` | correct | 0.9 | returns a populated per-section profile audit (redacted here) |
| 13 | `naukri_auth_status` | correct | 0.21 | logged_in true + verified true + api_confirmed; matches the unexpired nauk_at and session_info's live endpoint check |
| 14 | `naukri_blocked_companies` | correct | 0.12 | returned the blocked-company list |
| 15 | `naukri_bulk_fetch_jobs` | correct | 0.16 | fetched both probe job ids |
| 16 | `naukri_cached_answers` | correct | 0.08 | returned the cached screening answers (text redacted here) |
| 17 | `naukri_check_email` | correct | 0.08 | is_email_verified=true, is_mobile_verified=true; email/mobile returned as values (redacted here) |
| 18 | `naukri_company_intel` | correct | 17.69 | returned reviews for the probe company |
| 19 | `naukri_company_jobs` | correct | 2.78 | returned job rows for the probe group_id |
| 20 | `naukri_company_slug` | correct | 13.0 | NOT_FOUND naming the true cause: that group_id has no AmbitionBox integration |
| 21 | `naukri_compare_jobs` | correct | 11.21 | compared the two probe job ids |
| 22 | `naukri_compare_offers` | correct | 0.06 | offers=2 for the two probe job ids |
| 23 | `naukri_config` | correct | 0.02 | not_loadable_here=6 |
| 24 | `naukri_conversion_funnel` | correct | 0.03 | total_applied=11 == DB applied_at>=30d exactly |
| 25 | `naukri_dashboard` | correct | 1.28 | profile_views=343, experience_years and ctc_lpa populated (CTC redacted here) |
| 26 | `naukri_debug` | correct | 0.05 | browser_snapshot returned a live DOM with 5 [data-job-id] nodes |
| 27 | `naukri_download_resume` | correct | 0.24 | wrote a 64628-byte PDF to the scratchpad path |
| 28 | `naukri_draft_follow_up` | correct | 0.04 | company/title/days_since_applied all match the DB row for the probe job (redacted here) |
| 29 | `naukri_export_data` | correct | 0.06 | record_count=162 == DB count(*) exactly |
| 30 | `naukri_follow_status` | correct | 0.13 | returned follow state for the probe group_id |
| 31 | `naukri_follow_up_priority` | correct | 0.2 | partial_success and names the inbox sub-fetch in errors[]; stale_applications=5 is real. The inbox worked 15 calls later, so that sub-fetch was transient |
| 32 | `naukri_get_application` | correct | 0.15 | returns the probe job's stored application row (redacted here) |
| 33 | `naukri_get_job` | correct | 14.53 | returned the probe job's full detail |
| 34 | `naukri_get_profile` | correct | 0.25 | full profile object returned, all major sections populated (redacted here) |
| 35 | `naukri_get_recommendations` | correct | 0.19 | count=5 of total=68 |
| 36 | `naukri_get_settings` | correct | 0.15 | settings=19 |
| 37 | `naukri_health_check` | correct | 9.17 | 5 checks, 21 probes, 3 degraded, api_metrics.errors=27 -- real counters, not placeholders |
| 38 | `naukri_job_detail_v1` | correct | 0.15 | returned the v1 detail shape for the probe job |
| 39 | `naukri_list_alerts` | correct | 0.2 | count=3 alerts returned with ids and filters (ids redacted here) |
| 40 | `naukri_list_applications` | correct | 0.07 | total=162 == DB applications count(*) exactly |
| 41 | `naukri_list_early_access` | correct | 0.35 | total=5, count=5 |
| 42 | `naukri_list_interview_rounds` | correct | 0.12 | total_rounds=0 == DB interview_rounds (empty) |
| 43 | `naukri_list_notifications` | correct | 0.14 | total=5, count=5, all is_read=false |
| 44 | `naukri_list_reminders` | correct | 0.77 | total=50 == DB reminders exactly |
| 45 | `naukri_list_saved_jobs` | correct | 0.09 | total=2 == DB saved_jobs exactly (both rows are TEST DATA) |
| 46 | `naukri_match_analytics` | correct | 0.1 | total_applies=3, field_breakdown over 6 fields, plus a user_details block (redacted here) |
| 47 | `naukri_match_quality` | correct | 0.09 | total_applies=3; payload identical to match_analytics minus user_details |
| 48 | `naukri_mock_interview_history` | correct | 0.1 | scalars: {"total": 0, "count": 0, "page": 1, "has_more": false, "interview_count": 0} |
| 49 | `naukri_mock_interview_prep` | correct | 33.38 | returned a prep payload for the probe job |
| 50 | `naukri_mock_interview_topics` | correct | 0.12 | passes Naukri's own HTTP 400 'No Role Activated for this user' through with http_status intact -- an account-state refusal, not a server defect |
| 51 | `naukri_notification_prefs` | correct | 0.15 | scalars: {"hint": "Use naukri_update_settings to toggle recruiter_notification, promotional_notification, etc."} |
| 52 | `naukri_notification_summary` | correct | 0.15 | new_count=4; categories appStatus=11, rmj=11 (total_count 62) |
| 53 | `naukri_photo_info` | correct | 0.21 | has_photo=true, photo_url populated (redacted here) |
| 54 | `naukri_profile_prompts` | correct | 1.77 | scalars: {"source": "ccs_widget", "pending_count": 0, "completed_count": 0, "cache_ttl_seconds": 56786, "widget_section |
| 55 | `naukri_profile_targeting` | correct | 0.08 | returns the targeting block (redacted here) |
| 56 | `naukri_read_message` | correct | 0.01 | VALIDATION_ERROR naming all three required ids. NOTE: with real ids it mints a notification row per call (known, unfixed, not exercised here) |
| 57 | `naukri_recruiter_activity` | correct | 0.12 | returns recruiter action rows incl. recruiter names (redacted here) |
| 58 | `naukri_recruiter_history` | correct | 0.04 | total_companies=115 == DB distinct companies exactly -- the 08-22 'LIMIT 20 became the total' bug is FIXED and this run is the live confirmation |
| 59 | `naukri_research_company` | correct | 35.29 | returned reviews for the probe company |
| 60 | `naukri_resume_builder_status` | correct | 0.27 | scalars: {"attempts_left": 3, "is_paid": false, "show_genai_features": true, "show_rewrite": false, "experiment_variant |
| 61 | `naukri_resume_info` | correct | 0.26 | resume_headline, resume filename, last-updated all populated (text redacted here) |
| 62 | `naukri_resume_templates` | correct | 0.25 | templates=20 |
| 63 | `naukri_salary_benchmark` | correct | 10.91 | partial_success with jobs_sampled=5 / jobs_with_salary=0 -- Naukri hides salary on most posts and the tool says which half it got |
| 64 | `naukri_salary_position` | correct | 0.1 | NOT_FOUND: 'No salary data found in 162 applications' -- 162 == DB count(*) exactly |
| 65 | `naukri_scheduler_status` | correct | 0.29 | scalars: {"running": true, "total_tasks": 11, "enabled_tasks": 11, "disabled_tasks": 0} |
| 66 | `naukri_search_companies` | correct | 0.2 | returned company rows for the probe keyword |
| 67 | `naukri_search_impressions` | correct | 0.16 | total_appearances_all_time=6120, recruiter_actions=343, windowed timeline present |
| 68 | `naukri_search_jobs` | correct | 13.49 | total=19412, count=20 (Naukri caps a page at 20) |
| 69 | `naukri_server_info` | correct | 0.03 | commit 1bc55528dec1 / pid 43228 / surface.tools=125 -- matches the process and the live tools/list exactly |
| 70 | `naukri_session_info` | correct | 0.12 | authenticated=true measured against the activityLevel endpoint; nauk_rt/nauk_sid/nauk_cs correctly report present=null with the locked-cookie-jar reason rather than false. NOTE: it is the only tool with no top-level `status` key |
| 71 | `naukri_similar_jobs` | correct | 0.13 | returned similar-job rows for the probe job |
| 72 | `naukri_skill_gap` | correct | 0.27 | returns present/missing skill sets over a 5-job sample (skill list redacted here) |
| 73 | `naukri_stale_applications` | correct | 0.07 | total=151 of 162, count/page/has_more consistent |
| 74 | `naukri_status_changes` | correct | 0.57 | scalars: {"total_changes": 0, "positive_changes": 0, "sync_method": "rest_api", "last_sync": "2026-08-24T02:47:16.31960 |
| 75 | `naukri_subscription_status` | correct | 0.75 | scalars: {"is_paid": false, "has_active_subscription": false, "is_jobseeker_agent_eligible": false, "promo_code": "FAST |
| 76 | `naukri_sync_applications` | correct | 1.6 | applications=20 |
| 77 | `naukri_sync_saved` | correct | 2.14 | saga_steps=3 |
| 78 | `naukri_sync_saved_jobs` | correct | 0.38 | scalars: {"total_remote": 0, "new_added": 0, "already_local": 0, "total_local": 2} |
| 79 | `naukri_tailor_resume` | correct | 12.33 | returned a tailored resume payload for the probe job (redacted here) |
| 80 | `naukri_task_history` | correct | 0.05 | runs=5 |
| 81 | `naukri_taxonomy` | correct | 0.18 | returned the role taxonomy |
| 82 | `naukri_triage_inbox` | correct | 0.25 | total_in_inbox=62, unread_in_inbox=11, returned=10, scored=true |
| 83 | `naukri_visibility` | correct | 0.14 | scalars: {"visibility": "active", "hint": "Visibility returned as a simple status string."} |

---

## 5b. The 42 tools NOT called, with the reason recorded per tool

| # | Tool | Reason it was not called |
|---|------|--------------------------|
| 1 | `naukri_accept_nvite` | on the never-call list - irreversible account/quota/reputation write |
| 2 | `naukri_add_interview_round` | writes account or local state; not on the never-call list but unsafe to exercise - inserts an interview_rounds row |
| 3 | `naukri_agent_approve` | on the never-call list - irreversible account/quota/reputation write |
| 4 | `naukri_agent_decisions` | needs a real cycle_id; the agent_runs table is empty so none exists. Never invent one. |
| 5 | `naukri_agent_reject` | on the never-call list - irreversible account/quota/reputation write |
| 6 | `naukri_agent_run_now` | on the never-call list - irreversible account/quota/reputation write |
| 7 | `naukri_agent_update_config` | on the never-call list - irreversible account/quota/reputation write |
| 8 | `naukri_alert_detail` | needs a real alert_id; list_alerts is auth-blocked so none can be sourced. Never invent an id. |
| 9 | `naukri_answer_mock_interview` | writes account or local state; not on the never-call list but unsafe to exercise - submits a real answer |
| 10 | `naukri_apply` | on the never-call list - irreversible account/quota/reputation write |
| 11 | `naukri_apply_top_fits` | on the never-call list - irreversible account/quota/reputation write |
| 12 | `naukri_auto_hunt` | on the never-call list - irreversible account/quota/reputation write |
| 13 | `naukri_batch_apply` | on the never-call list - irreversible account/quota/reputation write |
| 14 | `naukri_boost_profile` | on the never-call list - irreversible account/quota/reputation write - rewrites his live headline |
| 15 | `naukri_create_alert` | writes account or local state; not on the never-call list but unsafe to exercise - creates a real job alert |
| 16 | `naukri_delete_alert` | on the never-call list - irreversible account/quota/reputation write |
| 17 | `naukri_delete_photo` | on the never-call list - irreversible account/quota/reputation write |
| 18 | `naukri_disable_task` | writes account or local state; not on the never-call list but unsafe to exercise - mutates scheduler config |
| 19 | `naukri_enable_task` | writes account or local state; not on the never-call list but unsafe to exercise - mutates scheduler config |
| 20 | `naukri_follow_company` | on the never-call list - irreversible account/quota/reputation write |
| 21 | `naukri_kill_switch` | on the never-call list - irreversible account/quota/reputation write |
| 22 | `naukri_login` | requires the operator at the keyboard (password/2FA/OTP) - opens a real sign-in flow |
| 23 | `naukri_logout` | writes account or local state; not on the never-call list but unsafe to exercise - deletes the cached nauk_at and auth_state.json, destroying mid-sweep the exact authenticated state this run exists to measure |
| 24 | `naukri_mark_all_notifications_read` | on the never-call list - irreversible account/quota/reputation write |
| 25 | `naukri_mark_interested` | on the never-call list - irreversible account/quota/reputation write |
| 26 | `naukri_mark_notification_read` | writes account or local state; not on the never-call list but unsafe to exercise - marks one read upstream |
| 27 | `naukri_purge_applications` | on the never-call list - irreversible account/quota/reputation write - DELETEs application history |
| 28 | `naukri_reauth` | writes account or local state; not on the never-call list but unsafe to exercise - mutates the cached credential and its browser_restart stage relaunches the persistent Chrome profile; browser restarts are out of scope |
| 29 | `naukri_report_fraud` | on the never-call list - irreversible account/quota/reputation write |
| 30 | `naukri_run_task_now` | writes account or local state; not on the never-call list but unsafe to exercise - can run the apply/agent task and spend quota |
| 31 | `naukri_save_job` | writes account or local state; not on the never-call list but unsafe to exercise - sync_to_naukri can push a bookmark upstream |
| 32 | `naukri_set_config` | on the never-call list - irreversible account/quota/reputation write |
| 33 | `naukri_set_reminder` | writes account or local state; not on the never-call list but unsafe to exercise - inserts a reminders row |
| 34 | `naukri_share_early_access` | on the never-call list - irreversible account/quota/reputation write |
| 35 | `naukri_start_mock_interview` | writes account or local state; not on the never-call list but unsafe to exercise - starts a real assessment on his account |
| 36 | `naukri_unsave_job` | writes account or local state; not on the never-call list but unsafe to exercise - removes one of his saved jobs |
| 37 | `naukri_update_alert` | writes account or local state; not on the never-call list but unsafe to exercise |
| 38 | `naukri_update_profile` | on the never-call list - irreversible account/quota/reputation write |
| 39 | `naukri_update_settings` | on the never-call list - irreversible account/quota/reputation write |
| 40 | `naukri_upload_photo` | on the never-call list - irreversible account/quota/reputation write |
| 41 | `naukri_upload_resume` | on the never-call list - irreversible account/quota/reputation write |
| 42 | `naukri_verify_otp` | requires the operator at the keyboard (password/2FA/OTP) - consumes a one-time code |
---

## 6. Tools still UNMEASURED, and why each one is

42 of the 125 registered tools were never called. None was skipped by accident;
each carries a reason. Grouped by why.

**On the never-call list - irreversible account, quota or reputation write
(24).** `naukri_accept_nvite`, `naukri_agent_approve`, `naukri_agent_reject`,
`naukri_agent_run_now`, `naukri_agent_update_config`, `naukri_apply`,
`naukri_apply_top_fits`, `naukri_auto_hunt`, `naukri_batch_apply`,
`naukri_boost_profile`, `naukri_delete_alert`, `naukri_delete_photo`,
`naukri_follow_company`, `naukri_kill_switch`,
`naukri_mark_all_notifications_read`, `naukri_mark_interested`,
`naukri_purge_applications`, `naukri_report_fraud`, `naukri_set_config`,
`naukri_share_early_access`, `naukri_update_profile`, `naukri_update_settings`,
`naukri_upload_photo`, `naukri_upload_resume`.

**Writes account or local state; not on the never-call list but unsafe to
exercise (14).** `naukri_add_interview_round`, `naukri_answer_mock_interview`,
`naukri_create_alert`, `naukri_disable_task`, `naukri_enable_task`,
`naukri_logout`, `naukri_mark_notification_read`, `naukri_reauth`,
`naukri_run_task_now`, `naukri_save_job`, `naukri_set_reminder`,
`naukri_start_mock_interview`, `naukri_unsave_job`, `naukri_update_alert`. The
last two additions to this group are `naukri_logout` and `naukri_reauth`, newly
classified this run.

**Requires the operator at the keyboard (2).** `naukri_login` opens a real
sign-in flow; `naukri_verify_otp` consumes a one-time code.

**Cannot be given a valid argument without inventing one (2).**
`naukri_alert_detail` - see section 2, its precondition has now lapsed and it
should be promoted next run. `naukri_agent_decisions` - needs a real
`cycle_id`, and `agent_runs` is still empty (0 rows).

24 + 14 + 2 + 2 = 42.

`naukri_debug` was called, but only with `action="browser_snapshot"`. Its
`api_post`, `api_put`, `api_delete` and `discover_click` actions were not
exercised: the first three write upstream and the fourth clicks a
caller-supplied selector on the live signed-in page. So `naukri_debug` is
measured on one action out of a larger surface.

### The exact shape of what remains unverified

For all 42: what is unverified is not merely "the fields are right" but whether
the tool performs its action at all. A skip yields **zero** information - it is
not a weaker form of a pass.

**38 of the 42 are write-capable** (24 never-call + 14 write-unsafe). This run
exercised none of them against the live account, so every write path this server
exposes remains unverified exactly as it was before the run. The companion
document `_sweep/write_paths.md` is a *read-path* audit - it establishes which
READ tools have hidden write sinks, which is what made this sweep safe to run -
and it is not a verification of the write tools themselves.

---

## 7. Closing: what remains unverified

This run closed the read side and only the read side. 83 of 125 tools were
called against a genuinely authenticated session, and all 83 returned a real
observation - zero came back auth-blocked, against 46 in the previous run. That
moves the count of tools with a real behavioural measurement from 34 to 83, a
net 49. Of those 83, 78 behaved correctly and were cross-checked against
`naukri.db` where a local number existed to check against - application totals,
distinct company counts, reminder and saved-job counts, agent and interview-round
emptiness all matched exactly.

The remaining 42 tools are unverified, and this sweep is not evidence about them
in either direction. They are every apply and quota path, every profile and
settings write, alert create/update/delete, mark-read, the agent
run/approve/reject family, the kill switch, `set_config`, the two tools needing
the operator at the keyboard, and two that cannot be given a valid argument
without inventing one. Verifying any of them requires performing the write it
names, against the operator's real account, and that was explicitly out of
scope. A read-only sweep cannot close a write, and nothing above should be cited
as though it had. The same applies within `naukri_debug`: one of its actions was
measured, four were not.

Three defects were found, all of them invisible to a logged-out sweep. The
serious one is `naukri_daily_brief` publishing zero unread recruiter messages
while eleven sit unread, because it reads the `count` of an `unread_only=True`
page that comes back empty beside its own `unread: 11` - a fetch that succeeds
with wrong content, which is the one case the existing "a failed fetch is None,
never a zero" guard was not built to catch. `naukri_notification_count` fails
the same way through a different door, a `safe_get(..., default=0)` with the
anti-corruption layer's own missing-field warning switched off.
`naukri_interview_prep` carries a permanent `ImportError` left behind when the
consolidated action-parameter dispatchers were reversed; that one at least
announces itself in `errors[]`.

Two caveats on this document's own confidence. The mechanism given for 4.2 is
derived from reading the code, not from probing the raw endpoint, because that
would need `naukri_debug api_fetch`; the contradiction itself is measured. And
the session was verified live at the start of the sweep and observed to hold
through the final call, but a transient upstream 503 immediately afterwards is a
reminder that any single measurement here is a sample with a timestamp.
