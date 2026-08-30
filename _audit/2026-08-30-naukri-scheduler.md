# Naukri scheduler: five defects, reproduced and resolved

2026-08-30. Scope: the five background-worker defects. Every claim below is
either VERIFIED (an instrument produced it) or labelled otherwise.

The operator's question was not "are the logs healthy". It was:

> *"I'm not able to see what advantage these background workers are providing
> me so far. What value have they provided me?"*
> *"If these are real features, why are they not working?"*

Answer up front: of eleven scheduled tasks, **two had never run once**, one was
**generating 76% of all events in the database and discharging none of them**,
one was **correctly disabled and had never made a single decision**, and one was
**returning the same four bytes 288 times a day**. Four of the five are now
fixed or explained. None of them was fixed by making a log look better.

---

## 0. Instruments and method

All measurement was done against a COPY of the live `naukri.db`, taken while
the server kept running. Nothing in this work opened the live database for
writing, and the server was never stopped, restarted or touched.

| Instrument | What it gave |
|---|---|
| `scheduled_runs` census (2,228 rows, 2026-08-20 to 2026-08-30) | per-task run counts and stored results |
| Uptime reconstruction from the `t2_verify_pending` 300s heartbeat | 26 restarts; longest continuous session 32.58h; only 2 sessions ever reached 24h |
| `event_log` census | which subsystem is actually producing the noise |
| Replay of the measured uptime through old vs new due-logic | before/after run counts on the operator's real history |

The replay is the load-bearing one and is reported in full in section 1.

### The census that started it

```
t2_verify_pending    n=1830   ok=1830  fail=0
reminder_check       n=152    ok=150   fail=2
notification_digest  n=79     ok=71    fail=8
agent_cycle          n=66     ok=66    fail=0
sync_saved_jobs      n=41     ok=39    fail=2
sync_applications    n=30     ok=28    fail=2
stale_check          n=26     ok=26    fail=0
api_discovery        n=2      ok=2     fail=0
retention_sweep      n=2      ok=2     fail=0
daily_brief          ABSENT FROM THE TABLE
boost_profile        ABSENT FROM THE TABLE
```

`daily_brief` and `boost_profile` do not appear at all. Zero rows out of 2,228,
across ten days.

---

## 1. and 2. `daily_brief` and `boost_profile` never fire -- FIXED

These are one defect, not two, so they are treated together.

### Reproduction

VERIFIED. `naukri_scheduler_status` on the live server reports `last_run: null`
for both, and both are absent from all 2,228 `scheduled_runs` rows. They have
never executed.

### The recorded suspicion was wrong

The defect was recorded as *"`catch_up: false` on a fixed-hour task, so a
missed window is never made up."* **Flipping `catch_up` would have changed
nothing.** `catch_up` only shortens the FIRST sleep after startup. The task is
then rejected further down the same loop by the hour gate, which `catch_up`
does not touch:

```python
if task.run_at_hour is not None and not self._is_target_hour(task.run_at_hour):
    continue                      # <- rejects regardless of catch_up
if not self.is_due(task):
    continue
```

Worse, the file's own catch-up policy comment asserted the opposite as settled
fact -- `daily_brief - hour-gated (08:00 IST), not interval-gated` -- which is
exactly backwards: the task was gated by BOTH, and that is the defect.

### The actual mechanism

Two conditions had to hold in the SAME process, and never once did:

1. **The interval clock restarts from zero on every process start.**
   `prime_schedule` seeded every non-`catch_up` task's `_last_started` to
   `now`, so `is_due` demanded 86400s of *continuous uptime*.
2. **The hour gate is an equality test.** The loop also had to be awake inside
   that one specific IST hour.

Measured uptime: **26 restarts in ten days. Only two sessions ever reached 24h**
(25.73h ending 08-24 15:18 IST, 32.58h ending 08-26 23:53 IST). Neither crossed
08:00 or 09:00 IST *after* passing its 24h mark. So condition 2 never coincided
with condition 1.

### The control that isolates the hour gate

`retention_sweep` sits in the SAME 86400s bucket, with the same interval and
the same clock, and differs in exactly one respect: `run_at_hour=None`. It needs
condition 1 only.

It ran **exactly twice**: 08-24 14:00 IST and 08-26 16:00 IST. Those are the
first hourly wakes after the 24h mark of each of the two long sessions, to the
minute. Same bucket, same interval; the only difference is the hour gate, and it
is the difference between two runs and zero.

### The fix

A **day window** replaces "exactly this hour, and only after a full interval of
uptime". A fixed-hour task is due once per IST calendar day, from `run_at_hour`
until `catch_up_window_hours` later, and "already served today" is read from
PERSISTED history.

Changed in `naukri_server/scheduler.py`:
- `ScheduledTask.catch_up_window_hours` (new field, default 6.0)
- `is_due` branches on `run_at_hour` into `_window_is_open_and_unserved`
- `_window_start` decides which day a window belongs to (windows may cross midnight)
- `prime_schedule` no longer seeds fixed-hour tasks with `now` -- that seeding was
  itself the blocker: it put `_last_started` inside the current window, so the
  window read as already served
- `prime_schedule_from_db` now consults persisted history for fixed-hour tasks,
  and **fails CLOSED** if the read raises
- the `_is_target_hour` gate is removed from `_run_interval` (folded into `is_due`)
- the startup grace now fires for any due task, not only `catch_up` ones

`_is_target_hour` itself was DELETED. After the change nothing called it, and its
only remaining reference was a test that exercised it directly -- a test of a
method the product no longer uses certifies nothing. It is replaced in
`tests/test_scheduler.py` by `test_window_start_is_the_most_recent_opening`,
which tests the piece that now does the deciding.

`naukri_server/framework/registry.py`: the `@scheduled_task` decorator had an
explicit signature and silently could not pass the new field. Widened.

### Why this is NOT `catch_up`, which matters for `boost_profile`

`boost_profile` rewrites the headline on the operator's **real Naukri profile**.
`catch_up` means "once per server START" -- with 26 restarts in ten days that
would be 26 profile writes. The day window means "once per IST DAY", decided
from the persisted last run, so a restart storm produces one run.

`tests/test_scheduler_cadence.py::test_only_non_mutating_tasks_opt_into_catch_up`
already forbids `boost_profile` from opting into `catch_up`. **That test was not
touched and still passes** -- the two mechanisms are deliberately separate, and a
new test pins that they stay separate.

### Evidence the fix works, on the operator's real history

Replaying the measured uptime sessions through both rules:

```
daily_brief     OLD fires=0   NEW fires=11
boost_profile   OLD fires=0   NEW fires=11
                NEW distinct IST days served: 11, one run per day, no day twice
```

The old rule reproduces the observed reality exactly -- **0, matching the
measured 0** -- which is what validates the model. The new rule serves all 11
days, exactly once each.

### Red-then-green

`tests/test_scheduler_day_window.py` (new, 15 tests). Before the fix: **9 failed,
6 passed**. The reproduction of the live defect:

```
E   AssertionError: boost_profile fired 0 times across 26 restarts in one day;
    it rewrites the live Naukri profile headline
E   assert 0 == 1
```

```
E   AssertionError: assert [] == ['daily_brief']
E     Right contains one more item: 'daily_brief'
```

After: all 15 pass, and `test_scheduler.py` + `test_scheduler_cadence.py` (50
pre-existing tests) stay green.

### Was the feature itself any good?

Fixing a trigger on a broken feature would be no fix. `naukri_daily_brief` was
called live (a read) and returns real content -- 72KB of it:

| | |
|---|---|
| unread recruiter messages | **14** |
| recruiter activity events | **364** |
| new recommendations | 5 |
| stale applications | 151 |
| recruiter searches surfacing him | 7,345 |
| recommended actions | 6 |

That is what he has been missing every morning. `boost_profile`'s body is
test-covered but has never run live; its first live run will be the first
observation, and that is stated rather than claimed.

---

## 3. `reminder_check` re-reports the same 50 reminders forever -- FIXED

### Reproduction

VERIFIED, and worse than recorded.

- `reminders` holds exactly **50 rows**, all created 2026-03-03, all due
  2026-03-17. As of today that is **166 days overdue**, not 161.
- `event_log` holds **7,571 `ReminderDue` events** over ten days. That is the
  **largest event type in the database** -- more than `ScheduledTaskCompleted`
  (2,230) and `BrowserCrashed` (2,368). About 757 a day, from 50 rows.
- Stored `reminder_check` results are identical: `due_count=50 total=50`, on
  every readable run.

### Mechanism

Two halves, both confirmed in code:

1. **No memory.** `reminder_service.list_reminders(emit_events=True)` emitted a
   `ReminderDue` for *every* past-due row on *every* call. The `is_due` column in
   the schema is dead -- `INTEGER DEFAULT 0`, 0 in all 50 rows, written by
   nothing.
2. **No discharge.** `tools/reminders.py` exposed exactly two MCP tools, `list`
   and `set`. `database.delete_reminder` has existed since the SQLite port with
   **no caller** outside the uncalled `ReminderRepository.delete`. A reminder
   could be created and re-dated but never closed.

A reminder that cannot be discharged is not a feature. It is also actively
harmful here: the live brief's `recommended_actions` currently reads *"Follow up
on 50 due reminder(s)"*, so the litter is crowding out his real top actions.

### The fix

Not deletion -- the feature is worth having, it just had no end.

- `reminders.notified_at TEXT`, added via the existing migration ledger
  (`reminders_notified_at_v1`) and to the `CREATE TABLE` for fresh databases.
- `ReminderDue` is emitted only while `notified_at IS NULL`, and stamped
  immediately after a successful emit. The stamp is assigned **only after the
  write lands**, so a failed stamp leaves the row re-emittable (noisy) rather
  than reporting a notification the database does not record (silent).
- `set_reminder` clears `notified_at`, so re-dating genuinely re-arms. Without
  this, snoozing would produce a reminder that never notifies again -- a silent
  failure worse than the storm.
- `naukri_dismiss_reminder(job_id)` is the discharge path.
  `mark_reminder_notified` is a targeted UPDATE, not an upsert, so it cannot
  clobber a concurrent `set_reminder`.

Net effect: ~757 events/day becomes at most one per reminder per due-transition.

### The guard that caught it

`delete_reminder` is on `test_destructive_reachability.py`'s `DESTRUCTIVE_SINKS`.
The new tool reaches it, and the census failed as designed before the tool was
registered with a reason:

```
AssertionError: 1 tool(s) can reach a destructive sink without being declared:
  naukri_dismiss_reminder -> dismiss_reminder -> delete_reminder
```

That is the guard working. It is now declared with an honest reason: dismissing
IS the point of the tool, it removes one local row and touches nothing on
Naukri, and the reminder is recreatable via `naukri_set_reminder`.

### Red-then-green

`tests/test_reminder_lifecycle.py` (new, 6 tests), all red before and green
after, including the storm in miniature: ten consecutive
`list_reminders(emit_events=True)` calls over one due reminder must emit
**once**.

### Known, and stated because it will be visible

The fix is not live until the server restarts. All 50 existing rows migrate in
with `notified_at = NULL`, so the **first `reminder_check` tick after restart
will emit one last ~50-event burst**, and then go quiet permanently.

---

## 4. `agent_cycle` is off -- INVESTIGATED, DELIBERATELY NOT CHANGED

Nothing here was modified. `naukri_server/agent.py` is untouched.

### What is actually true

The claim "switched off in config" is true but mis-located. **The scheduler task
is ON and running normally** -- 66 runs, 66 completed, every 2 hours, ~4ms each.
What it returns every time is:

```json
{"status": "skipped", "reason": "Agent is disabled in config"}
```

The AGENT is off, not the task.

### Why it is off

VERIFIED: it is the **shipped default**, not something anyone switched off.

- `agent.py:44` -- `"enabled": False,  # Must be explicitly enabled`
- `agent_config.json` mirrors that default exactly, and is **gitignored**
  (`.gitignore:73`), so no git history for it exists or ever could
- `naukri_agent_config` reports `config_sources: {}` -- the shared
  `config/jobhunt.json` supplies nothing here
- `agent_runs` = **0 rows**. `agent_decisions` = **0 rows**

That last line settles it: the agent has never run a cycle, never made a
decision, and **has never applied to a single job**. It was never turned off,
because it was never on.

So the honest answer to "did someone disable it and record why" is: **no one
disabled it. It has been off since it was built, by design.** The 2026-08-24
live end-to-end audit independently records the agent write paths as deliberately
UNVERIFIED for the same reason.

### The four protections -- verified present and intact

All four were confirmed in the current tree. **The line numbers in the brief are
stale**; the real locations are recorded here so the next reader does not hunt:

| Protection | Where it actually is |
|---|---|
| `MIN_AGENT_FIT_FLOOR` | `agent.py:146` (`_min_agent_fit_floor`, falls back to the literal 60 if the import fails), applied `agent.py:491` as `max(configured, floor)` |
| Config-independent kill switch | `agent.py:794-801`, re-checked **inside** the auto-apply loop, so a trip mid-batch halts the rest |
| Daily quota before `_act` | `agent.py:454` computes `daily_remaining`; `agent.py:621` stops adding candidates in `_decide`, upstream of `_act` |
| Tier-B ratchet | `policy.py:498-520`, `confirm_widen` on the write path |

### What would have to be true for it to be safe to enable

This is the operator's decision and nobody else's. Recording the preconditions,
not a recommendation:

1. **Applications cannot be withdrawn.** Nothing in this codebase or on Naukri
   provides a withdraw. Every application is permanent and carries his real name
   to a real employer.
2. **The apply path has never been executed by the agent.** `agent_runs` = 0.
   The four guards are code-reviewed and unit-tested, but the end-to-end
   `_decide -> _act -> _apply_single` chain has never run live. Enabling it is
   the first live test of the write path, on his real account.
3. **`mode: "auto"` is the second switch, and it is separate.** Current mode is
   `dry_run`. Setting `enabled: true` alone gives observation without applying.
   That is the safe first step if he wants any of this, and it is reversible.
4. **`agent.py`'s own comment names the escalation:** five config writes take the
   system from "disabled, dry_run, threshold 70" to fifteen real applications a
   day at no threshold. Guard 2 (the forced approval cycle on a policy-hash
   change) explicitly does **not** cover the agent block -- the file says so
   directly. A file-armed agent is bounded by guards 1, 3 and 4 only.
5. **The account already carries 7 captcha blocks.** Automated apply volume is
   not a neutral act on it.

A defensible sequence, if he ever wants it: `enabled: true` + `mode: dry_run`
for a week, read `naukri_agent_decisions` to see what it *would* have applied to,
and only then consider `auto`. Not done, not recommended, not my call.

---

## 5. `t2_verify_pending` returns zeros 288x/day -- CONTROL BUILT, TASK KEPT

### Reproduction

VERIFIED and stronger than recorded. **1,830 runs, byte-identical result on all
1,830**:

```json
{"status": "completed", "confirmed": 0, "reverted": 0, "revert_failed": 0}
```

`auto_fix_pending` has **0 rows and has never had one**.

### Mechanism -- and this one is not a bug

Drift IS happening: 324 `EndpointDriftDetected` events (RECOMMENDED_JOBS_API
266, DASHBOARD_API 33, APPLIED_JOBS_API 25). All three are tier **T2**, so the
healing router did reach the T2 branch. It declined all 324, and the reason is
recorded in the notification it banked:

> `API drift on RECOMMENDED_JOBS_API (no verified alias mapping to apply (would
> be a guess))`

The drift was `severity=added` -- an additive schema change (`ambitionBoxData`
appeared) with nothing to remap. **The healer correctly refused to guess.**

So the zeros are not a broken probe. They are a probe correctly reporting "no
provisional fix is outstanding", on a queue that has never had an entry because
the gate one link upstream has never legitimately opened.

### The control

`tests/test_t2_verify_control.py` (new) drives
`scheduler_tasks._task_t2_verify_pending` itself -- the exact coroutine the
scheduler invokes -- through three cases:

1. **CONFIRM** -- due row, endpoint re-validates healthy -> `confirmed == 1`.
   The case production has never produced.
2. **REVERT** -- due row, endpoint still drifts -> `reverted == 1`.
3. **NOT-YET-DUE** -- `verify_at` in the future -> exactly the production zeros,
   and the revalidation callback `assert_not_awaited()`. That assertion is the
   point: it proves the **dueness filter** produced the zeros, distinguishing a
   working probe from one that silently does nothing.

The only seam patched inside `_revalidate_endpoint` is the HTTP boundary. CONFIRM
and REVERT have **identical patching and differ only in the JSON bytes returned**,
so the real `check_drift` does the deciding. Every conservative branch (constant
missing, no snapshot, fetch raised) stays armed.

Each case was then shown RED under a targeted break in `t2_autofix.py`, e.g.
removing the dueness filter:

```
E   AssertionError: a not-yet-due row must produce exactly the bytes production
    has returned 1,830 times, got {'status': 'completed', 'confirmed': 1,
    'reverted': 0, 'revert_failed': 0}
```

All breaks were reverted; `naukri_server/` is unmodified by this slice, proven
by `git hash-object` matching the HEAD blob.

### Verdict: KEPT, not deleted

`t2_verify_pending` is a real safety mechanism that has never been needed. It is
the revert half of the T2 snapshot-and-revert contract; delete it and a bad
auto-edit to a decision-feeding endpoint sits unverified forever. Its cost is one
indexed SELECT against an empty table every five minutes.

But it is not *proven in production*, and the honest framing is: a seatbelt in a
car that has never been driven. These controls are the bench test showing the
buckle latches and releases. Two limits stated rather than glossed: the
`revert_failed` branch remains unproven-reachable, and the mechanism's value stays
hostage to whether the router's "would be a guess" gate ever opens.

**The next measurement on this thread is the router, not the task.**

### One cost that IS real

1,830 of 2,228 `scheduled_runs` rows -- **82%** -- are this task's zeros, plus a
matching `ScheduledTaskCompleted` event each. That is ~576 rows/day of pure
bookkeeping. It is not fixed here (it is a separate change to where the scheduler
writes run rows) but it is the reason the run table is hard to read, and
`retention_sweep`, the task meant to prune it, has itself run twice in ten days.

---

## 6. Findings outside the five

Same disease as defects 1-2, not in the brief, not fixed -- flagged for a decision:

- **`retention_sweep`** (daily, `run_at_hour=None`): 2 runs in ten days. Needs 24h
  continuous uptime; only two sessions ever had it. The retention policy for
  `event_log` therefore effectively does not run, while `event_log` grows ~1,200
  rows/day.
- **`api_discovery`** (weekly): 2 runs, both on 2026-08-20 -- and those two were
  the double-fire incident a previous fix addressed. It needs 7 days of
  continuous uptime and the longest session measured is 32.58h. **It can never
  run again as configured.**

Both are interval tasks with no `run_at_hour`, so the day window does not apply
to them. The clean fix is the same idea generalised (persist the interval clock
across restarts rather than reseeding it to `now`). Deliberately not done here:
it changes cadence for `sync_applications`, whose purge step deletes rows, and
that decision deserves its own change rather than riding along inside this one.

### Not mine, but you will see it

`tests/test_no_committed_identity.py` fails on
`_audit/2026-08-30-naukri-bind-latency.md` -- a tracked audit file containing a
drive-root path, committed in `e9c39a5a` by the concurrent agent. It was failing
before I started, is in that agent's lane, and I left it alone.

---

## 7. Test results

| | |
|---|---|
| Baseline before this work | **3993 passed, 1 failed** |
| After | see final run below |

The brief's "3991" was already stale when I started; 3993 is the measured
baseline. The 1 failure is the other agent's audit file, described above.

New tests added by this work: 15 (`test_scheduler_day_window.py`) + 6
(`test_reminder_lifecycle.py`) + 3 (`test_t2_verify_control.py`) = **24**, plus
one replaced in `test_scheduler.py`.

Live-state files verified unchanged (git-clean) at finish: `naukri.db`,
`sync_state.json`, `healing_state.json`, `agent_config.json`. The server was not
restarted, port 8321 was not touched, Chrome was not launched.

---

## 8. For the operator, in plain language

**What these workers have actually given you so far: almost nothing, and you
were right to ask.**

Two of them -- your morning brief and your profile boost -- had **never run a
single time**, not once in the ten days of history in the database. Not
misconfigured, not intermittent: zero. The reason was a scheduling rule that
required the server to stay up for 24 unbroken hours *and* to be awake during
one exact hour of the morning. Your server restarted 26 times in those ten days
and stayed up 24h only twice, neither time in the morning. Both are now fixed,
and replaying your real uptime through the new rule gives 11 briefs and 11
boosts across those same 11 days, one per day.

That brief is worth having. When run just now it surfaced **14 unread recruiter
messages you have not seen**, 364 recruiter activity events, 5 new
recommendations, 151 stale applications, and the fact that recruiters have
surfaced your profile 7,345 times. You have been getting none of that, every
morning.

The reminder checker was the opposite problem: it worked too well and never
stopped. Fifty reminders from March, all long past due, re-announced roughly 757
times a day -- 7,571 events in ten days, more than any other thing in your
database, and there was **no way to close one**. There is now: reminders notify
once and can be dismissed. This also cleans up your brief, which currently wastes
one of its six recommended actions telling you to follow up on those 50.

The autonomous agent is off, and it is off **by design, not by accident**. Nobody
switched it off; that is how it shipped. It has never made a decision or applied
to anything. I left it exactly as it is, because turning it on means real,
permanent applications to real employers under your name, and that is your call.
Section 4 lists what I would want to be true first -- the short version is that
`enabled: true` with `mode: dry_run` would let you watch what it *would* do for a
week without it doing anything, and that is the only step I would consider
reversible.

The fifth one, the five-minute verifier, is the odd case: it returns zeros 288
times a day and that is **correct**. It is the safety catch on an automatic
code-repair feature, and it reports "nothing to check" because the repair feature
has correctly refused to guess at every change it has seen. It was worth keeping,
but nobody had ever shown it could return anything other than zero, so I built
the test that proves it can -- confirm, revert, and a genuine "nothing due". It
is a seatbelt in a car that has not been driven yet.

**Honest summary: two features you were never receiving now work; one that was
shouting at you now has an off switch; one is correctly idle; one was correctly
off and stays off until you say otherwise.**
