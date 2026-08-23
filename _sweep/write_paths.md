# naukri MCP - read-path write audit

Static source audit at `7e4ebfe` (branch `master`). No server was started, no MCP tool was
called, nothing was executed. Every verdict below is traced from the `@mcp.tool()` definition
through its helpers by an import-aware AST call graph, then confirmed by reading the code at
the cited line.

## Method and its two corrections

The first pass used a name-keyed call graph and produced false positives (`get`, `put`,
`update`, `list` collide across modules). It was rebuilt import-aware. Two blind spots were
found and fixed before any verdict was recorded, and both matter for anyone re-running this:

1. **Tools pass their helper as a bare reference**, e.g.
   `handle_tool_action(_get_history, "mock_interview.history")`. A graph that only follows
   `Call.func` misses almost every tool's real body. `naukri_mock_interview_history` read as
   "no write sinks" until reference edges were added.
2. **Writes do not go through `api_post` by name.** The codebase calls
   `api_client.post/put/delete` (`interfaces.py:144-165`, `NaukriApiClient`), so a sink list
   keyed on `api_post` misses 26 call sites including several in read tools.

`_api_request` (`api.py:284`) has no caller outside `api.py`, and the only other HTTP client in
the package is `tools/ambitionbox_rest.py:83`, which is a `session.get`. So every remote write
in this package is an `api_post` / `api_put` / `api_delete` or their `api_client.*` wrappers -
that enumeration is closed.

## Legend, and one distinction the W1 rule needs

- `SAFE-READ` - no write of any kind.
- `SAFE-READ (POST-as-query)` - reads only, but reaches Naukri over HTTP POST because the
  endpoint is a **query** endpoint (paginated list, batch fetch, filtered search). It is a POST
  on the wire and so trips the literal W1 test, but it changes no account state. It is called
  out separately so a network trace during the sweep does not read as an account mutation.
  Server-side semantics cannot be proven from source; the classification rests on the endpoint
  shape, the request body, and the response being a list.
- `LOCAL-WRITE` - writes `naukri.db` or a local JSON state file.
- `FILE-WRITE` - writes a file on disk.
- `EVENT-WRITE` - emits a domain event. **Every** emit is at minimum a local DB write:
  `EventBus.emit` calls `database.log_event` unconditionally at `naukri_server/events.py:425`,
  so an emit writes an `event_log` row even when no subscriber persists anything.

## Verdicts

| tool | verdict | evidence (file:line + writing call) |
|---|---|---|
| `naukri_read_message(message_id, vcard_id, unique_id)` | ~~EVENT-WRITE~~ **SAFE-READ as of 2026-08-22** | Upstream read is a GET - `tools/inbox.py:153 api_client.get(MESSAGE_API, ...)` - so it does **not** mark the message read on Naukri. But `tools/inbox.py:166-171 event_bus.emit(InboxMessageRead(...))` fires unconditionally, and `subscribers.py:729-746 _on_inbox_message_read -> store_notification(...)` stores unconditionally. Net: 1 `notifications` row + 1 `event_log` row per call. See REMAINING section. |
| `naukri_tailor_resume(job_id)` | SAFE-READ | `tools/resume_tailor.py:99-108` fetches job + profile and returns suggestions only. No POST/PUT reached; it does **not** create or modify a resume on Naukri. |
| `naukri_mock_interview_prep(job_id)` | SAFE-READ | `tools/mock_interview.py:173 _interview_prep`; 19 functions reached, zero write sinks. Does not touch `_start_interview`. |
| `naukri_mock_interview_topics()` | SAFE-READ | `tools/mock_interview.py:24-25 api_client.get` x2. |
| `naukri_mock_interview_history()` | SAFE-READ (POST-as-query) | `tools/mock_interview.py:78 api_client.post(MOCK_INTERVIEW_HISTORY_API + "?detailedView=false", body={"page":1,"pageSize":50})` - pagination query; empty body 500s, per the comment at :74-77. |
| `naukri_draft_follow_up(job_id)` | SAFE-READ | `tools/tracking.py:158`; 8 functions reached, zero write sinks. |
| `naukri_assess_fit(job_id, apply_if_fit=False)` | SAFE-READ | The apply path exists but is gated: `tools/smart_apply.py:469 if apply_if_fit and fit["overall_score"] >= min_fit_score:` -> `:470 _apply_single(...)`. With `apply_if_fit=False` nothing downstream runs. **With `apply_if_fit=True` this is a REMOTE-WRITE**: `tools/apply.py:188,271 api_client.post(APPLY_WORKFLOW_API, ...)` plus `ApplicationSubmitted` at `tools/apply.py:120`. |
| `naukri_score_saved_jobs(min_fit_score=101)` | SAFE-READ | `tools/smart_apply.py:59 _bulk_saved_scoring` reads via `tools/tracking.py::_list_saved_jobs` (local DB), not `saved_jobs_service.list_saved_jobs`, so it never reaches the `SavedJobExpiring` emit. |
| `naukri_interview_prep(job_id)` | SAFE-READ | `services/application_service.py:213-216 _safe_fetch_fit_score` calls `naukri_assess_fit(job_id=job_id)` with `apply_if_fit` left at its `False` default, so the apply path is unreachable here. |
| `naukri_company_intel(company, intel_type)` | SAFE-READ | `tools/ambitionbox.py:788`. Valid `intel_type` = **`salary`**, **`reviews`**, **`interviews`** only (`tools/ambitionbox.py:34 _VALID_INTEL_TYPES`); anything else returns VALIDATION_ERROR at :820. All three are read-only AmbitionBox scrapes plus a best-effort REST enrichment; 22 functions reached, zero write sinks. |
| `naukri_debug(action=...)` | per action | See the `naukri_debug actions` section. `browser_snapshot` is SAFE-READ (`tools/debug/browser_actions.py:8-33`, pure `page.evaluate` DOM read). |
| `naukri_cached_answers(action="list")` | SAFE-READ | `tools/insights.py:137-142` returns from the `list` branch before the `update` (:144) and `delete` (:158) branches. `update`/`delete` are FILE-WRITE (`cache.py:43-45 _save_cache` -> `questions.json`) + EVENT-WRITE (`tools/insights.py:152,166`). |
| `naukri_health_check(include_browser=False)` | SAFE-READ (POST-as-query) | `tools/health.py:124 api_client.post(RECOMMENDED_JOBS_API, body={})` in `_check_recommendations_api`. The other four checks are `api_client.get` (:56, :70, :88, :139). Probes are **not** run: `source="checks"` and the `probe_summary` extra both call `probe_registry.summary()` / `all_results()`, which only read cached `probe.last_result` (`health/framework.py:135,154`). The `ProbeStateChanged` emit at `health/framework.py:304` is inside `_on_result`, reached only when a probe actually runs (scheduled loop), and is change-detected at :301. |
| `naukri_daily_brief()` | **LOCAL-WRITE + FILE-WRITE** | `tools/daily_brief.py:41 mark_notifications_delivered(ids, via="brief")` - UPDATEs up to 10 `notifications` rows, stamping `delivered_via` and `read_at`. Calling it **consumes** pending notifications. Also `tools/early_access.py:54 _save_seen_roles` -> `utils.py:188-190 save_json_atomic` rewrites `early_access_tracking.json` (side effect documented at `tools/early_access.py:35`). Does **not** emit ReminderDue / ApplicationStale - both are now opt-in and the brief does not opt in. |
| `naukri_dashboard()` | SAFE-READ | `tools/profile.py:121`; 11 functions reached, zero write sinks. |
| `naukri_audit_profile()` | **EVENT-WRITE (conditional)** | `services/profile_service.py:228 event_bus.emit(ProfileScoreChanged(...))`, reached only when `:227 if new_score != old_score`. Steady state emits nothing. First call after a fresh DB or after the retention sweep prunes `event_log` reports once from `old_score=0`. Subscriber `subscribers.py:256-270` stores unconditionally, so a real score move banks one notification. Allowlisted with a written reason in `tests/test_read_path_purity.py`. |
| `naukri_profile_targeting()` | SAFE-READ | `tools/profile.py:131`; 6 functions reached, zero write sinks. |
| `naukri_get_recommendations(limit=5)` | SAFE-READ (POST-as-query) | `tools/search.py:159 api_client.post(RECOMMENDED_JOBS_API, body={})`. |
| `naukri_list_early_access()` | SAFE-READ | `tools/early_access.py:176` -> `_list_with_filters` -> `:66 api_client.get(EARLY_ACCESS_API, ...)`. It does **not** call `_detect_new_roles`, so unlike the daily brief it does not write `early_access_tracking.json`. |
| `naukri_recruiter_activity()` | SAFE-READ (POST-as-query) | `services/performance_service.py:174 api_client.post(RECRUITER_ACTIVITY_API, body=body)`. The old read-path `RecruiterEngaged` emit was removed in `aefdde4`; `performance_service.py:245-256` is now a comment explaining why. |
| `naukri_search_impressions()` | SAFE-READ | `tools/performance.py:37`; 5 functions reached, zero write sinks. |
| `naukri_activity_level()` | SAFE-READ | `tools/performance.py:127`; 3 functions reached, zero write sinks. |
| `naukri_visibility()` | SAFE-READ | `tools/settings.py:471`; settings GET only. The `SETTINGS_API` POST at `tools/settings.py:239` is inside `_update_settings` and is not reached. |
| `naukri_check_email()` | SAFE-READ | `tools/settings.py:461`; 6 functions reached, zero write sinks. |
| `naukri_subscription_status()` | SAFE-READ | `tools/settings.py:495`; 6 functions reached, zero write sinks. |
| `naukri_blocked_companies()` | SAFE-READ | `tools/settings.py:451`; 6 functions reached, zero write sinks. |
| `naukri_notification_prefs()` | SAFE-READ | `tools/settings.py:484`; 6 functions reached, zero write sinks. |
| `naukri_get_settings()` | SAFE-READ | `tools/settings.py:402`; 6 functions reached, zero write sinks. |
| `naukri_resume_info()` | SAFE-READ | `tools/resume_photo.py:431`; 3 functions reached, zero write sinks. |
| `naukri_photo_info()` | SAFE-READ | `tools/resume_photo.py:478`; 2 functions reached, zero write sinks. |
| `naukri_resume_templates()` | SAFE-READ | `tools/resume_builder.py:77` -> `_get_templates`; GET only. |
| `naukri_resume_builder_status()` | SAFE-READ | `tools/resume_builder.py:87` -> `_get_status`; GET only. |
| `naukri_taxonomy()` | SAFE-READ | `tools/insights.py:382`; 8 functions reached, zero write sinks. |
| `naukri_profile_prompts()` | SAFE-READ | `tools/insights.py:398`; 9 functions reached, zero write sinks. |
| `naukri_skill_gap(...)` | SAFE-READ | `tools/insights.py:315`; 6 functions reached, zero write sinks. |
| `naukri_salary_benchmark(...)` | SAFE-READ | `tools/insights.py:348`; 6 functions reached, zero write sinks. |
| `naukri_salary_position()` | SAFE-READ | `tools/insights.py:231`; 8 functions reached, zero write sinks. |
| `naukri_research_company(keyword="Infosys")` | SAFE-READ | `tools/companies.py:126`; 32 functions reached, zero write sinks. The `api_client.post` at `services/company_service.py:380` is in `follow_company` and is not reached. |
| `naukri_agent_status()` | SAFE-READ | `tools/agent_tool.py:199`; 9 functions reached, zero write sinks. |
| `naukri_agent_config()` | SAFE-READ | `tools/agent_tool.py:210`; reads config only. The writer is the separate `naukri_agent_update_config` (:220). |
| `naukri_agent_history()` | SAFE-READ | `tools/agent_tool.py:288`; 8 functions reached, zero write sinks. |
| `naukri_agent_decisions(cycle_id)` | SAFE-READ | `tools/agent_tool.py:304`; 8 functions reached, zero write sinks. |
| `naukri_scheduler_status()` | SAFE-READ | `tools/scheduler_tool.py:81`; reads registry state. The `ScheduledTaskCompleted` emit at `scheduler.py:350,382` is in `_execute_task` and is reached only by the scheduler loop / `naukri_run_task_now`, not by this tool. |
| `naukri_task_history()` | SAFE-READ | `tools/scheduler_tool.py:149`; 8 functions reached, zero write sinks. |
| `naukri_list_reminders()` | SAFE-READ | `services/reminder_service.py:157 event_bus.emit(ReminderDue(...))` sits inside `:154 if emit_events:`, and the tool does not pass it. Fixed by `1dbcd1b`. |
| `naukri_list_interview_rounds()` | SAFE-READ | `tools/tracking.py:240`; local DB read. |
| `naukri_recruiter_history()` | SAFE-READ | `tools/tracking.py:178`; 8 functions reached, zero write sinks. |
| `naukri_stale_applications()` | SAFE-READ | `services/application_service.py:431 emit(ApplicationStale)` sits inside `:410 if emit_events:`; tool does not pass it. Fixed by `aefdde4`. |
| `naukri_follow_up_priority()` | SAFE-READ (POST-as-query) | Same `emit_events` guard as above. Also reaches `tools/inbox.py:50 api_client.post(INBOX_API, body=body)` via `application_service.application_follow_up` - inbox listing query. |
| `naukri_config(section=None)` | SAFE-READ | `tools/config_tool.py:71` -> `_config(section)`; reports effective policy, no write sinks. |
| `naukri_export_data(data_type="applications", output_path=...)` | **FILE-WRITE** | `tools/export.py:97` and `:106 file_path.write_text(...)`. Also `:81 _EXPORTS_DIR.mkdir(exist_ok=True)` creates `exports/` in the repo root even when `output_path` points elsewhere, and `:92 file_path.parent.mkdir(parents=True, exist_ok=True)`. No remote or DB write. |
| `naukri_download_resume(save_path=...)` | **FILE-WRITE** | `tools/resume_photo.py:91 save.parent.mkdir(parents=True, exist_ok=True)` and `:92 save.write_bytes(data)`. Remote side is `session.get` (:82), a GET. |
| `naukri_sync_applications()` | **LOCAL-WRITE + EVENT-WRITE + FILE-WRITE** | `tools/sync.py:482 upsert_application`, `:484 delete_applications_before(cutoff)` (retention purge of local rows), `:489 emit(ApplicationStatusChanged)` per change, `:533 emit(SyncCompleted)`, `:51 _save_sync_state` -> `sync_state.json`. |
| `naukri_sync_saved()` | **LOCAL-WRITE + EVENT-WRITE + FILE-WRITE** | `tools/sync.py:627 upsert_saved_job`, `:631 emit(SyncCompleted)`, `:51 _save_sync_state` -> `sync_state.json`. |
| `naukri_sync_saved_jobs()` | **LOCAL-WRITE** | `services/saved_jobs_service.py:167 upsert_saved_job` via `sync_saved_jobs_from_naukri`. No event emitted on this path. |
| `naukri_list_notifications()` | SAFE-READ | `tools/notifications.py:58`; read only. The delivery UPDATE lives in `naukri_daily_brief` and in `naukri_mark_*_read`, not here. |
| `naukri_notification_count()` | SAFE-READ | `tools/notifications.py:80`; 7 functions reached, zero write sinks. |
| `naukri_notification_summary()` | SAFE-READ | `tools/notifications.py:128`; 7 functions reached, zero write sinks. Does not mark anything read. |
| `naukri_list_inbox()` | SAFE-READ (POST-as-query) | `tools/inbox.py:50 api_client.post(INBOX_API, body=body)` - paginated inbox listing. |
| `naukri_list_alerts()` | SAFE-READ | `tools/alerts.py:295`; 6 functions reached, zero write sinks. |
| `naukri_alert_detail(alert_id)` | SAFE-READ | `tools/alerts.py:307`; 6 functions reached, zero write sinks. |
| `naukri_get_job(job_id)` | SAFE-READ | `tools/jobs.py:308` -> `_get_job`; 20 functions reached, zero write sinks. The POST at `tools/jobs.py:209` is `_report_fraud` and is not reached. |
| `naukri_job_detail_v1(job_id)` | SAFE-READ | `tools/jobs.py:385`; 8 functions reached, zero write sinks. |
| `naukri_bulk_fetch_jobs(job_ids)` | SAFE-READ (POST-as-query) | `tools/jobs.py:230 api_client.post(BULK_JOBS_API, {"jobIds": job_ids})` - batch fetch, capped at 20 ids (:227). |
| `naukri_similar_jobs(job_id)` | SAFE-READ | `tools/search.py:181 api_client.get(SIMILAR_JOBS_API + job_id, ...)`. |
| `naukri_compare_jobs(job_ids)` | SAFE-READ | `tools/jobs.py:340`; 20 functions reached, zero write sinks. |
| `naukri_compare_offers(job_ids)` | SAFE-READ | `tools/tracking.py:258`; 9 functions reached, zero write sinks. |
| `naukri_search_jobs(keywords)` | SAFE-READ | `tools/search.py:90 api_client.get(SEARCH_API, params=rest_params)` REST-first. On REST failure it falls back to a browser intercept (`tools/search.py:118-130`) which navigates the operator's logged-in page to a search URL - a navigation, not a write, but see Caveats. |
| `naukri_search_companies(keyword)` | SAFE-READ | `tools/companies.py:49`; 12 functions reached, zero write sinks. |
| `naukri_company_jobs(group_id)` | SAFE-READ | `tools/companies.py:73`; 15 functions reached, zero write sinks. |
| `naukri_company_slug(group_id)` | SAFE-READ | `tools/companies.py:97`; 11 functions reached, zero write sinks. |
| `naukri_follow_status(group_id)` | SAFE-READ (POST-as-query) | `services/company_service.py:316 api_client.post(BATCH_FOLLOW_STATUS_API, ...)` - batch status lookup. The mutating `follow_company` POST is at `:380` and is not reached. |
| `naukri_application_insights()` | SAFE-READ | `tools/insights.py:212`; 9 functions reached, zero write sinks. |
| `naukri_match_analytics()` | SAFE-READ | `tools/insights.py:277`; 6 functions reached, zero write sinks. |
| `naukri_match_quality()` | SAFE-READ | `tools/insights.py:296`; 6 functions reached, zero write sinks. |
| `naukri_conversion_funnel()` | SAFE-READ | `tools/insights.py:414`; 9 functions reached, zero write sinks. |
| `naukri_status_changes()` | **LOCAL-WRITE + EVENT-WRITE + FILE-WRITE** | Runs a **full application sync**: `services/insights_service.py:430 sync_result = await _sync_applications(days_back=days_back)`, unconditional. That reaches `tools/sync.py:482 upsert_application`, `:484 delete_applications_before`, `:489 emit(ApplicationStatusChanged)`, `:533 emit(SyncCompleted)`, and `:51 _save_sync_state` -> `sync_state.json`. Declared in the docstring, but the tool name and its five analytics siblings all read as pure reads. See REMAINING section. |
| `naukri_get_profile()` | SAFE-READ | `tools/profile.py:66`; 18 functions reached, zero write sinks. |
| `naukri_get_application(job_id)` | SAFE-READ | `tools/tracking.py:74`; local DB read. |
| `naukri_list_applications()` | SAFE-READ | `tools/tracking.py:46`; local DB read. |
| `naukri_list_saved_jobs()` | SAFE-READ | `services/saved_jobs_service.py:60 emit(SavedJobExpiring)` sits inside `:51 if emit_events:`; tool does not pass it. Fixed by `aefdde4`. |

## REMAINING read-path mutations

`emit_events=True` appears at exactly three call sites in the whole package, all in
`scheduler_tasks.py` (`:88`, `:132`, `:176`). The three fixed read paths are therefore clean,
and the guard pinning that is real.

### 1. `naukri_read_message` - the 1dbcd1b/aefdde4 shape ~~still live~~ **CLOSED 2026-08-22**

> **CLOSED 2026-08-22.** Everything below was true when written and is now history: the
> emit, the `_on_inbox_message_read` subscriber and the `InboxMessageRead` dataclass were all
> removed, and the allowlist entry with them. Removed rather than gated for the same reason
> `RecruiterEngaged` was: there is no scheduled producer for an inbox read, so an
> `emit_events` flag would have been a parameter nothing ever sets. The line and file
> references in this section no longer resolve - do not go looking for them.

This is the instance `aefdde4` recorded in its own allowlist as "KNOWN DEFECT, reported not
fixed". It was still present at `7e4ebfe`, and it was worse than the allowlist entry suggested
because the second half was never applied either:

- **Half one, unconditional emit:** `naukri_server/tools/inbox.py:166-171`
  ```
  from naukri_server.events import event_bus, InboxMessageRead
  await event_bus.emit(InboxMessageRead(
      thread_id=str(conversation_id or ""),
      message_id=str(message_id or ""),
  ))
  ```
  No `emit_events` parameter exists on `_read_message` at all.
- **Half two, unconditional store:** `naukri_server/subscribers.py:736-745`
  `_on_inbox_message_read` calls `store_notification(...)` with **no**
  `has_pending_notification` check - unlike `_on_reminder_due` (`:232`),
  `_on_application_stale` (`:108`) and `_on_saved_job_expiring` (`:281`), which all got the
  dedupe predicate.

Cost per call: one `notifications` row (`priority: "low"`) plus one `event_log` row. It is
linear in calls, not in reminders, so it cannot burst 50-at-once the way ReminderDue did - but
it never self-limits either, and re-reading one message N times banks N rows. The row count is
0 today only because the tool has not been used; **the sweep calling it is exactly the event
that starts the count.**

### 2. `naukri_status_changes` - a full sync behind an analytics-shaped name (CONFIRMED)

`services/insights_service.py:430` calls `_sync_applications` unconditionally. This is not
the notification-storm shape and I am not claiming it is: both subscribers are
change-conditional, so a repeat call on a stable account banks nothing.
`_on_sync_completed` (`subscribers.py:176`) returns early when
`status_changes_count == 0 and new_added == 0`, and `_on_status_change` only fires per real
transition. What it *does* do on every call is upsert every application row, run
`delete_applications_before(cutoff)` (`tools/sync.py:484`), rewrite `sync_state.json`, and
write `event_log` rows.

It is flagged because of **where the guard cannot see it**: `tests/test_read_path_purity.py`
only inspects functions whose name (after stripping underscores) starts with
`get_ list_ fetch_ read_ audit_ search_ find_ load_`. `detect_status_changes` and
`_sync_applications` match none of those, so no amount of read-path mutation inside them will
ever fail that test. The same blind spot covers `_cached_answers`, `_on_result`,
`_execute_task`, `api_validator_probe` and `discover_config_audit`. None of those currently
storms - I checked each - but the guard is not what is keeping them honest.

### 3. Not defects, recorded so they are not re-reported

- `naukri_audit_profile` emits `ProfileScoreChanged` only on a real score move
  (`services/profile_service.py:227`). Bounded and allowlisted with a reason.
- `naukri_daily_brief` UPDATEs up to 10 notification rows to delivered
  (`tools/daily_brief.py:41`) and rewrites `early_access_tracking.json`
  (`tools/early_access.py:54`). Both are the tool's job, but a sweep that calls the brief
  **consumes** pending notifications and changes what a later brief shows.
- `browser_watchdog.py:119,175` emits `BrowserCrashed` / `BrowserRecovered`, each of which
  banks a notification (`subscribers.py:354,371`). These fire from the background monitor loop,
  not from any tool call, so they are ambient during a sweep rather than attributable to a
  tool - but a sweep that wedges the browser will produce them.

## naukri_debug actions

16 actions, from `_HANDLERS` at `naukri_server/tools/debug/__init__.py:37-56`. An unknown
action returns an error without touching anything (`:97-100`). Note that **every** action
acquires a page from the pool (`:102`), and every `browser_*` action with a non-empty `url`
navigates the operator's real logged-in session (`:105 page_goto`).

| action | mutates? | evidence |
|---|---|---|
| `browser_snapshot` (default) | no | `browser_actions.py:8-33`; `page.evaluate` DOM read only |
| `browser_screenshot` | **yes - FILE-WRITE** | `browser_actions.py:38-39` writes/overwrites `debug.png` in the repo root |
| `browser_scan` | no | `browser_actions.py:43`; DOM read |
| `browser_deepscan` | no | `browser_actions.py:101`; DOM read incl. hidden/iframes |
| `browser_explore` | no | `browser_actions.py:207`; DOM + JS data read |
| `browser_notif_explore` | no | `browser_actions.py:442`; DOM read, no click in the module |
| `api_fetch` | no | `api_actions.py:11`; `fetch` with default GET |
| `api_post` | **yes - REMOTE-WRITE** | `api_actions.py:51 method: 'POST'` to an arbitrary caller-supplied path and body |
| `api_put` | **yes - REMOTE-WRITE** | `api_actions.py:90 method: 'PUT'` |
| `api_delete` | **yes - REMOTE-WRITE** | `api_actions.py:129 method: 'DELETE'` |
| `api_fetch_widget` | no | `api_actions.py:164`; GET with widget headers |
| `api_settings` | no | `api_actions.py:196-201 api_get` |
| `discover_pages` | no | `discovery_actions.py:28`; navigate + passively capture JSON responses |
| `discover_statuses` | no | `discovery_actions.py:87`; navigate + scroll + capture statuses |
| `discover_intercept` | no | `discovery_actions.py:137`; passive listener on outgoing POST bodies |
| `discover_click` | **yes - BROWSER MUTATION** | `discovery_actions.py:205 await page.click(selector, timeout=5000)` on the live logged-in account. The selector is caller-supplied, so this can hit Apply / Save / Delete. Not an HTTP write from our code, so it is invisible to a W1 grep - and it is the single most dangerous action here. |

Safe subset for a sweep: `browser_snapshot`, `browser_scan`, `browser_deepscan`,
`browser_explore`, `browser_notif_explore`, `api_fetch`, `api_fetch_widget`, `api_settings`,
`discover_pages`, `discover_statuses`, `discover_intercept`.

## Caveats and UNKNOWNs

- **Server-side semantics of the POST-as-query endpoints are not provable from source.**
  `RECOMMENDED_JOBS_API`, `INBOX_API`, `BULK_JOBS_API`, `RECRUITER_ACTIVITY_API`,
  `BATCH_FOLLOW_STATUS_API`, `MOCK_INTERVIEW_HISTORY_API`, `SEARCH_API`. All are shaped as
  queries and return lists; none carries a mutation payload. Classified SAFE on that basis.
- **UNKNOWN: whether Naukri records server-side side effects from reads.** Fetching a job,
  running a search, or opening an inbox message may update "recently viewed", "recent searches"
  or a message-seen flag on Naukri's side. Nothing in this repo can answer that, and
  `naukri_search_jobs`' browser-intercept fallback (`tools/search.py:118-130`) drives the real
  logged-in session. Not claimed either way.
- **Any browser-touching tool takes the cross-process profile lock**, which writes a lock file
  (`profile_lock.py:157 _write_lock`). Infrastructure, not user data; noted for completeness.
- The `_backup/`, `exports/` and `debug.png` paths written by `export_data` and
  `browser_screenshot` land in the repo working tree.

## Reproducing this

The import-aware call graph used here is a throwaway script in the session scratchpad, not
harvested into the repo. The durable instrument for this defect class is already in the repo:
`tests/test_read_path_purity.py::emit_sites` is an AST census of every event-bus emit, and it
is shipped with controls that show it both detecting and not detecting. Its one gap is
`READ_PREFIXES` (`tests/test_read_path_purity.py:58-60`) - widening that matcher, or keying the
guard on "reachable from an `@mcp.tool()` whose docstring does not declare a write" instead of
on the function's name, would close finding 2.
