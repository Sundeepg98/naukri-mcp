# Inbox source fix -- four defects, three of them the same disease

Slice: `naukri-inbox-source-fix`
Repo: `mcp-servers/naukri`, branch `master`, HEAD `0852622`
Files touched: `naukri_server/tools/inbox.py`, `tests/test_inbox_unread_and_scoring.py` (new)
Files NOT touched: `naukri_server/validation.py` (see Defect 2 for the justification), and
none of the profile modules or `tests/test_profile_*`.
Writes fired against naukri.com: **zero**. Two read-only MCP calls
(`naukri_server_info`, `naukri_triage_inbox`) and nothing else.

The disease, stated once: **a field reported on the CALL while the caller read it as
reporting on the RESULT.** `count` reported how many rows this page listed and was read as
an unread total. `pageSize` was handed the caller's row limit. `scored` reported that the
profile read had succeeded and was read as "these rows have scores". Each fix separates the
two measurements and names both.

---

## The running server is stale, deliberately

`naukri_server_info` on the live process:

```
build.code.commit      0852622c9a31   branch master   dirty false   dirty_files 0
build.jobcore.commit   6acc7e6c6949
build.process.pid      15420          uptime 1887s
```

The build stamps are resolved once at import and frozen, so this process holds the code as
of `0852622` and **cannot see any edit made in this slice**. Every live number below is
therefore a BEFORE-state confirmation only. The server was not restarted, killed or started.

---

## Live before-state (read-only, 2026-08-24)

`naukri_triage_inbox(unread_only=True, limit=20)` against the running `0852622`:

| field | value |
|---|---|
| `total_in_inbox` | 67 |
| `unread_in_inbox` | 11 |
| `returned` | **2** |
| `scored` | **true** |
| `scoring_error` | null |
| `fit_score` on every returned row | **null** (2 of 2) |
| `not_listed` | field does not exist |

All three read-side defects reproduce in one call, and the first one is **worse than the
brief measured**: the brief saw 3 of 11, this call saw 2 of 11. Nothing regressed -- the
inbox total moved 66 -> 67 in the interval, so one more READ mail landed at the top of
page 1 and pushed a third unread row off it. That is the failure mode itself: the answer
degrades as ordinary mail arrives, silently, with no field that moves to say so.

No message text, recruiter name, company name, mail id or job id from that response appears
anywhere in this document.

---

## Defect 1 -- `_fetch_inbox` filtered client-side, so every caller under-reported

**Measured.** The REST GET built `pageSize` / `pageNo` / `mailType` and no read-state
parameter, so the server returned a mixed page and the read rows were dropped after the
fetch. On a newest-first inbox that empties the page. There is no parameter to add: probed
from a `pageSize=20&pageNo=1` baseline (20 rows / 3 unread / unreadCount 11 / total 66),
`isUnread`, `unreadOnly`, `unread`, `readStatus` and `isRead` each returned 200 with the
byte-same mixed page, and `isUnRead` returned 404. A previous commit fixed the symptom at
the daily brief only; `naukri_triage_inbox` -- the tool that exists for triage -- still
returned 2 of 11 as shown above.

**Changed.** `_fetch_inbox` now walks pages when `unread_only` is set, and reports the gap.

| what | where |
|---|---|
| page fetch split out, GET-then-POST behaviour preserved verbatim | `inbox.py:118` `_fetch_inbox_page` |
| row parsing split out unchanged | `inbox.py:152` `_parse_inbox_row` |
| the walk | `inbox.py:262` `_fetch_inbox`, loop at `:307` |
| stop: caller's limit met | `inbox.py:330` |
| stop: server's own unread count satisfied | `inbox.py:334` |
| stop: short raw page = end of data | `inbox.py:338` |
| stop: arithmetic exhaustion against `total` | `inbox.py:341` |
| stop: hard bound | `inbox.py:343` |
| `listed` / `not_listed` computed | `inbox.py:348-349`, returned at `:358-359` |
| `_triage_inbox` consumes `next_page` instead of `page + 1` | `inbox.py:524` |
| `_triage_inbox` compares short-page against the WIRE size | `inbox.py:518` |
| `_triage_inbox` reports `unread_returned` / `not_listed` | `inbox.py:634-645` |

**The bound is `INBOX_MAX_PAGES_WALKED = 10`** (`inbox.py:115`) -- ten HTTP round trips per
call, reaching 200 messages at 20 rows a page, three times the 66-67 the account holds. On
reaching it the walk STOPS and reports the shortfall in `not_listed` rather than spinning.

The POST fallback path is untouched in behaviour: it still sends `isUnRead` and still
filters server-side (`inbox.py:143-149`).

`_triage_inbox` line 518 is the subtle one and is worth stating: its short-page rule
compared delivered rows against a `page_size` it computed locally as up to 50. Once the wire
size is capped at 20, comparing 20 delivered rows against a locally-guessed 50 makes every
FULL page look short and ends the walk on page one -- the same rows-versus-pages conflation,
one level up. It now compares against `batch["page_size"]`, the size actually sent.

**Control RED** (mutation: `if not unread_only:` -> `if True:`, i.e. the shipped `0852622`
one-page behaviour):

```
E       AssertionError: unread_only returned 3 of the 11 unread the same response counted; page 1 holds only 3 of them
E       assert 3 == 11
tests\test_inbox_unread_and_scoring.py:191: AssertionError
```

**After.** 11 of 11 listed from a 4-page fixture, `not_listed == 0`, `has_more == False`;
triage likewise returns 11 of 11.

---

## Defect 2 -- `pageSize` max is 20, `validate_limit` allowed 50

**Measured.** `naukri_list_inbox(unread_only=True, limit=25)` -> HTTP 424
`"pageSize should be in between 1-20"`. The caller's `limit` went straight to the wire.

Note the 424 reached the CALLER, and the GET's `except` is bare and unconditional, so the
POST fallback did run and rejected 25 as well. Both endpoints enforce the bound. That is why
the fix caps both (`inbox.py:295` feeds `page_size` into the GET params at `:139` and the
POST body at `:145`), and why capping is a strict improvement for the third caller,
`application_service.application_follow_up`, which asks for `limit=50`: on live that request
could only ever have been rejected by both paths.

**Changed.**

| what | where |
|---|---|
| `INBOX_MAX_PAGE_SIZE = 20` with the measurement in its comment | `inbox.py:109` |
| `page_size = min(limit, INBOX_MAX_PAGE_SIZE)` | `inbox.py:295` |
| `page_size` returned so callers can see the wire size | `inbox.py:361` |
| `naukri_list_inbox` docstring corrected | `inbox.py:427` onward |

**Where the cap lives, and why not in `validation.py`.** `validate_limit(value,
max_allowed=50)` already takes the ceiling as a parameter, and it is shared with endpoints
that legitimately serve 50 rows a page. Moving 20 into its default would silently halve every
one of them; adding an inbox-specific branch inside a generic validator would put one
endpoint's API contract in a module that knows nothing about endpoints. 20 is THIS
endpoint's stated bound, so it is a named constant at this call site. `validation.py` is
unmodified.

`limit` and `pageSize` stay separate on purpose: `limit` is still how many rows the caller
gets (clamped to 50 by `validate_limit`), `page_size` is only what goes on the wire.

**Control RED** (mutation: `page_size = min(limit, INBOX_MAX_PAGE_SIZE)` -> `page_size =
limit`), with both endpoints enforcing the real bound so the test stays hermetic:

```
body = {'isUnRead': False, 'mailType': '', 'pageNo': 1, 'pageSize': 25}
E           naukri_server.api.NaukriAPIError: HTTP 424: pageSize should be in between 1-20
tests\test_inbox_unread_and_scoring.py:176: NaukriAPIError
```

That is the measured live failure, verbatim, reproduced from the mutant.

**After.** `limit=25` sends `pageSize=20` and returns 20 rows, status success. `limit=50`
sends 20. `limit=5` still sends 5 -- the cap is a ceiling, not a floor, and there is a
control for that.

---

## Defect 3 -- THE SILENT NULL

**Measured.** `inbox.py:426` (pre-fix) read `fit_obj.to_dict().get("fit_score")`. Running the
real engine in this checkout:

```
KEYS      : ['experience_match', 'overall_score', 'reasons', 'recommendation', 'skill_match']
get(fit_score) -> None
type      : jobcore.fit.FitScore
```

There is no `fit_score` key (`bonuses` appears conditionally; `overall_score` always does).
`.get` returns None for a missing key, raises nothing, so the `except` beside it never fired
and no warning was logged. Confirmed live above: `fit_score` null on every returned row while
`scored` said true.

**Changed.** `inbox.py:584` now reads `fit = fit_obj.overall_score` -- the attribute, not the
dict. That is deliberately stricter than `to_dict()["overall_score"]`: a missing attribute
raises `AttributeError`, which the `except` at `:586` catches and logs, and which now also
leaves `scored_rows` un-incremented so `scored` goes False. A missing dict key would go on
being silent. It also matches the house precedent at `tools/compare.py:158`
(`fit.overall_score`).

### Repo-wide sweep for the defect class

Two sweeps were run over `naukri_server/`, `tests/` and the sibling `jobcore/src/`,
excluding `venv/` and `site-packages/`.

**A. Literal `.get("fit_score")` / `.get('fit_score')` -- 18 sites.**

| file | sites | dict being read | verdict |
|---|---|---|---|
| `naukri_server/agent.py` | 10 (`:358,362,368,381,385,391,403,407,413,425`) | `naukri_auto_hunt` `ranked_jobs` entries | SAFE -- `services/auto_hunt_service.py:82` sets `"fit_score": fit.overall_score` |
| `naukri_server/tools/smart_apply.py` | 4 (`:277,288,296,329`) | its own `scored` rows | SAFE -- `smart_apply.py:148` sets `"fit_score": fit_score` |
| `naukri_server/domain/agent.py` | 1 (`:98`) | an auto_hunt ranked entry | SAFE -- same producer |
| `naukri_server/tools/agent_tool.py` | 1 (`:169`) | an `agent_decisions` DB row | SAFE -- `database.py:176` `fit_score INTEGER` |
| `naukri_server/services/application_service.py` | 1 (`:139`) | a stored application record | SAFE -- same column |
| `naukri_server/tools/inbox.py` | 1 (`:426` pre-fix) | `FitScore.to_dict()` | **THE DEFECT** |

**B. The wider class -- every `to_dict()` whose result has a key read off it.** Grepped for
`to_dict()` followed by `.get(` or `[`, and separately for every `to_dict()` call site in
source to catch the two-step `d = x.to_dict(); d.get(...)` form. Source-wide there are 16
`to_dict()` call sites; **exactly one** reads a key off the result, and it is `inbox.py:426`.
The rest either splat the dict (`services/profile_service.py:233`, `**report.to_dict()`) or
nest it as a value (`jobcore/config.py`, `jobcore/policy.py`).

**Count: 18 literal `.get("fit_score")` sites repo-wide, of which 1 is this defect class.
17 read dicts that genuinely carry the key. One site fixed.**

**Control RED** (mutation: restore `fit = fit_obj.to_dict().get("fit_score")`):

```
E       AssertionError: fit_score is null on a row the engine scored -- the read is against a key the result does not have
E       assert None is not None
tests\test_inbox_unread_and_scoring.py:406: AssertionError
```

**After.** The row's `fit_score` equals the engine's `overall_score`, pinned by calling the
real `score_job` in the test. No `to_dict()` is mocked anywhere in that class -- the defect
got in because a fixture agreed with the code and both were wrong -- and the expected value
is fetched as `fit.to_dict()["overall_score"]`, so a rename of that key raises `KeyError` in
the test itself.

---

## Defect 4 -- `scored` reported on the profile read, not on the rows

**Measured.** `scored, scoring_error = True, None` was set before any row was scored and
cleared only when the PROFILE read failed. Per-row failures left it True. Live: `scored:
true`, `scoring_error: null`, and `fit_score` null on every row.

**Changed.** The one flag became two measurements.

| what | where |
|---|---|
| the profile-read gate, renamed | `inbox.py:539` `can_score, scoring_error = True, None` |
| the row counters | `inbox.py:540` `scoring_attempted, scored_rows = 0, 0` |
| profile failure sets `can_score` + `scoring_error` only | `inbox.py:550` |
| the row loop gates on `can_score` and counts attempts | `inbox.py:558-559` |
| a successful row increments `scored_rows` | `inbox.py:585` |
| `scored` is now a statement about rows | `inbox.py:650` `"scored": scored_rows > 0` |
| the honest form of the same fact | `inbox.py:651-652` `scored_rows`, `scoring_attempted` |
| `scoring_error` keeps its meaning | `inbox.py:653` |

`scored` cannot be True when zero rows were scored -- it is literally `scored_rows > 0`. The
two causes stay distinguishable by their envelope: a failed profile read gives
`scoring_error` set and `scoring_attempted == 0` (nothing could be attempted); failed rows
give `scoring_error is None` and `scoring_attempted == N`. Both give `scored: false`, which
is correct -- they differ in remedy, not in outcome, and the fields say which.

**Control RED** (mutation: `"scored": scored_rows > 0` -> `"scored": can_score`):

```
E       AssertionError: scored: true beside 3 rows, none of which carries a score
E       assert True is False
tests\test_inbox_unread_and_scoring.py:477: AssertionError
```

**After.** Profile OK + every row raising -> `scored false`, `scored_rows 0`,
`scoring_attempted 3`, `scoring_error null`. Profile failing -> `scored false`,
`scoring_attempted 0`, `scoring_error` naming it. Partial -> `scored true`, `scored_rows 2`
of `scoring_attempted 4`.

---

## The seven controls

Each was shown RED by one deliberate source mutation, run against that control's tests
alone, then restored from a pristine copy and verified byte-for-byte by SHA-256. One
mutation at a time; all file I/O in binary-safe mode (`newline=''`), so the file's CRLF
endings survived every cycle.

Pristine SHA-256 of `naukri_server/tools/inbox.py` (post-fix, pre-mutation):
`5f6d9e20c9bf5025a781c385464f2f762c2141c9edce25f44c3027e12213e35f`
Every one of the eight restores returned exactly that digest.

| # | control | mutation | RED (first assertion) | restore |
|---|---|---|---|---|
| 1 | 11 unread across pages are all listed, with a `not_listed` count | walk removed, one page only | `returned 3 of the 11 unread the same response counted` / `assert 3 == 11` | byte-identical |
| 2 | the walk stops when the count is satisfied | satisfied-count stop removed | `the walk asked for pages ['1','2','3','4']; the 11th unread is on page 3` | byte-identical |
| 3a | the honest remainder, not silence | `not_listed = 0` | `7 rows against a count of 11 must report a gap of 4, not silence` / `assert 0 == 4` | byte-identical |
| 3b | the walk terminates when pages never run short | page bound removed | `assert 41 == 10` | byte-identical |
| 4 | `limit=25` is clamped, never a 424 | caller's limit sent to the wire | `NaukriAPIError: HTTP 424: pageSize should be in between 1-20` with `pageSize: 25` | byte-identical |
| 5 | `fit_score` is the real engine's `overall_score` | the silent null restored | `fit_score is null on a row the engine scored` / `assert None is not None` | byte-identical |
| 6 | `scored` is False when no row was scored | `"scored": can_score` | `scored: true beside 3 rows, none of which carries a score` | byte-identical |
| 7 | `scoring_error` still distinguishes the profile read | row failure writes `scoring_error` | `a row-level failure must not be reported as a profile failure` | byte-identical |

Control 4's first attempt went red for a weaker reason: the mutant's 424 fell through to the
POST fallback and hit the real, unpatched client, which raised `NotLoggedInError` before any
network call. That is a genuine consequence but a noisy assertion, so the test was made
hermetic -- both endpoints now enforce the bound in the fixture, matching the measurement
that the live 424 reached the caller -- and re-run. The RED above is from that re-run. No
network call was made in either attempt.

The new file also carries four `CONTROL_`-style negative checks so the new behaviour cannot
fire everywhere: a mixed call still fetches exactly one page; a small `limit` is not clamped
UP to 20; a partial scoring failure reports the partial number rather than collapsing to
false; and the two failure causes are asserted to produce different envelopes.

---

## Suite

`venv\Scripts\python.exe -m pytest tests/ -q`

| | passed | deselected | failed |
|---|---|---|---|
| baseline (given) | 3283 | 8 | 0 |
| after | **3305** | 8 | **0** |

Delta **+22 passed**, which is exactly the 22 tests in `tests/test_inbox_unread_and_scoring.py`.
3283 + 22 = 3305. **Zero regressions; nothing to explain.** Run time 199.91s.

No existing test encoded the old wrong behaviour, and none went red -- which was not a
given. Two came close and are worth naming, because both are about the same
rows-versus-pages distinction:

- `tests/test_inbox_field_paths.py::TestTriage::test_it_does_page_when_a_page_comes_back_full`
  builds a **50-row page** (`full = [_rest_row(mailId="a%d" % i) for i in range(50)]`) and
  asserts `returned == 55` over two pages. A 50-row page is not a shape this endpoint can
  produce -- its bound is 20 -- so that fixture does encode the pre-fix assumption. It stays
  green only because `limit` (rows returned) and `pageSize` (wire) were kept as separate
  numbers: `_fetch_inbox(limit=50)` caps the REQUEST at 20 but still returns whatever the
  response actually carried, up to `limit`. Had the cap been applied to the row limit as
  well, this test would have gone red at `returned == 25`, and it would have been a correct
  finding rather than a regression. Flagging it as a fixture that is impossible against the
  live API.
- `tests/test_inbox_field_paths.py::TestTriage::test_a_short_page_ends_the_walk` asserts
  `get.await_count == 1`. It passes because the walk is scoped to `unread_only=True` and
  that test runs the mixed path. A pager that ran on every call would have broken it.

`tests/test_unread_count_honesty.py` mocks `_fetch_inbox` wholesale, so the daily brief's
own fix is untouched by this one and its `listed` / `not_listed` vocabulary is what this
slice adopted.

---

## What a reviewer should look at first

1. `inbox.py:518` -- the short-page comparison in `_triage_inbox` now uses the wire page
   size. If this is ever reverted to a local `page_size`, triage silently stops paging after
   one page and defect 1 comes back one level up.
2. `inbox.py:584` -- attribute, not `.get`. The whole point is that a wrong name raises here.
3. `inbox.py:343` -- the walk's only unconditional stop. Everything else depends on server
   numbers that have already been observed to drift.
