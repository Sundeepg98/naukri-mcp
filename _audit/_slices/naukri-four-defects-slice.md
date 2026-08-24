# Four defects, measured against the live account - 2026-08-24

Slice worked from `D:\workspace\projects\job-hunting\mcp-servers\naukri` at HEAD
`412c3eb` (branch master, unchanged - no commit made by this slice). Zero writes
fired against naukri.com. Live calls were read-only throughout.

| # | Defect | Verdict |
|---|--------|---------|
| 1 | daily brief publishes 0 unread while 11 are unread | **CONFIRMED - fixed** |
| 2 | `naukri_notification_count` returns 0 | **NOT A DEFECT as stated - the latent silent-zero hazard fixed** |
| 3 | `naukri_interview_prep` permanently broken by an ImportError | **CONFIRMED - fixed** |
| 4 | AmbitionBox REST bridge sends no auth headers | **CONFIRMED - fixed; after-state NOT live-verified (403 stop)** |

Controls: 4 new test files, 45 tests, every one shown RED before its fix and
GREEN after, plus a planted mutation per defect shown RED and restored
byte-for-byte with a SHA-256 match.

---

## Defect 1 - the brief said zero while eleven sat unread

### What I measured

Two live read-only calls, minutes apart, on 2026-08-24:

```
naukri_list_inbox(limit=5, unread_only=True)
  -> status=success total=66 count=0 page=1 has_more=true unread=11 messages=[]

naukri_list_inbox(limit=5, unread_only=False)
  -> status=success total=66 count=5 page=1 has_more=true unread=11
     messages=[5 rows, every row is_read=true]
```

The second call is the mechanism proof and it excludes the competing
explanation: the page is not empty, it has five rows, and all five are read.
`INBOX_REST_API` takes `pageSize`/`pageNo`/`mailType` only - it has no unread
filter - so `_fetch_inbox` fetches the page and then drops read rows in Python
(`inbox.py:139`, `if unread_only and is_read: continue`). All five were dropped.

`count` in that return is `len(messages)` - how many rows this page listed. The
brief was reading it as an unread total. The fetch SUCCEEDS, so the "a failed
section is None, never a zero" guard never engaged and `errors[]` named only an
unrelated AmbitionBox 403.

`_triage_inbox` already defends against this exact hazard by publishing
`unread_in_inbox` (authoritative) separately from `returned` (rows in hand). The
fix follows that, rather than inventing a second approach.

### What I changed

- `naukri_server/tools/daily_brief.py:108-160` - new `_unread_messages_section`
  helper. Publishes `count` (authoritative unread total), `listed`,
  `not_listed`, `note`, `messages`. `count` is floored at `listed`, so a
  drifted `unread` can never make the brief report fewer unread than the rows
  it is holding. A payload with no `unread` key at all falls back to the page's
  own count.
- `naukri_server/tools/daily_brief.py:329` - section repointed at the helper
  (was an inline lambda reading `d.get("count", 0)`).
- `naukri_server/services/daily_brief_service.py:168-195` -
  `build_recommended_actions` now names the real total and, when
  `not_listed > 0`, says so and recommends `naukri_triage_inbox()` instead of
  `naukri_list_inbox(unread_only=True)` - the call that just came back empty.

### Control, shown failing

`tests/test_unread_count_honesty.py`, 10 tests. Pre-fix RED against the live
payload shape:

```
    async def test_brief_reports_eleven_not_the_filtered_pages_zero():
        results = _all_good_results()
        results[0] = dict(LIVE_FILTERED_PAGE)
        result = await _run_brief_with_mocked_results(results)
>       assert result["unread_messages"]["count"] == 11
E       assert 0 == 11
```

Mutation planted after the fix (`count = max(unread, listed)` -> `count = listed`
at `daily_brief.py:147`), 3 of 10 went RED:

```
E       assert 0 == 11                      (count)
E       assert 0 == 11                      (not_listed)
E       assert 0 == 1
E        +  where 0 = len([])               (the recommended action vanished)
```

Restored byte-for-byte; `daily_brief.py` SHA-256 back to
`fac10f96845fdde718dc2b8b836735a5c4c6546476c235a4b74063e89f004401`.

### After state

Against the live-shaped payload the brief now reports `count: 11`, `listed: 0`,
`not_listed: 11`, and a note reading "11 unread, could not list 11 of them".
The recommended action reads "Respond to 11 unread recruiter message(s) -- the
unread-only page could not list 11 of them".

Two behaviours were deliberately preserved and are pinned: a FAILED inbox fetch
is still `None`, never a zero; and a fully-listed page still carries no note and
still recommends `naukri_list_inbox(unread_only=True)` (a check that fires every
time certifies nothing).

**Not fixed, and out of this slice's scope:** `naukri_list_inbox` itself still
returns `count=0, messages=[]` for `unread_only=True` on this account. The brief
scoped defect 1 to the daily brief and the sweep filed the list tool as its own
row. Anyone picking that up: the fix there is paging until `limit` unread rows
are collected, not a report-shape change.

---

## Defect 2 - NOT A DEFECT as stated

### What I measured

The sweep document's own caveat says the mechanism for this row was "derived
from reading the code, not from probing the raw endpoint". I probed it.

Read-only GET through the live server (`naukri_debug` `api_fetch`), 2026-08-24
07:24:57 GMT:

```
GET /cloudgateway-mynaukri/notification-center-services/v0
    /naukrinotificationcentre/user/self/count
  -> HTTP 200, content-type application/json, content-length 11
     body: {"count": 8}

naukri_notification_count()  -> {"status": "success", "count": 8}
```

The key is `count`, it is present, and the tool agrees with the endpoint exactly.
There is no key to fix and no drift to repair. "count=0 while siblings see 4 to
11" did not reproduce.

I cannot say what the sweep saw. The endpoint is a live counter and the sweep ran
its tools in alphabetical order, which puts `naukri_mark_all_notifications_read`
immediately before `naukri_notification_count`; if that write ran, a truthful 0
follows. That is a hypothesis, not a measurement, and I did not test it because
it would require a write.

### What I changed anyway

The second half of the brief IS a real latent defect and it is fixed.

`naukri_server/services/notification_service.py:150-191`. Was:

```python
data = await api_client.get(NOTIFICATION_COUNT_API)
return {"status": "success", "count": safe_get(data, "count", default=0)}
```

`safe_get` is the anti-corruption layer and it supports missing-field detection
via `field_name`/`warn`. This call site used neither and defaulted to `0`, so an
absent or drifted key would have rendered as a confident zero under
`status: "success"` with no log line and no error channel.

Now: `default=None, field_name="count", warn=True, context="notifications.count"`,
and a missing/unusable field returns `count: None, counted: False, reason: "..."`.
A numeric string is still coerced to a number, preserving the pre-fix behaviour
for a gateway that stringifies. Same shape as `_triage_inbox`'s
`scored`/`scoring_error` pair.

### Control, shown failing

`tests/test_notification_count_honesty.py`, 8 tests. Pre-fix RED:

```
    async def test_absent_count_key_reports_unknown(self):
        result = await _count_with_payload({})
>       assert result["count"] is None
E       assert 0 is None
```

Mutation planted after the fix (`raw = safe_get(data, "count", default=0)` at
`:171`), 5 of 8 went RED, including the log check:

```
E       assert False
E        +  where False = any(<genexpr>)     (the missing field was not logged)
```

Restored byte-for-byte; `notification_service.py` SHA-256 back to
`ceb5f016b32384eb6dae79c1e1baa94bcf452357eb1743905580f16acaa78d05`.

### After state

`{"count": 8}` -> `count: 8, counted: True`. `{}` -> `count: None,
counted: False, reason: "the count endpoint returned no usable 'count' field
(got NoneType) -- unknown, not zero"`, with a WARNING logged. A genuine
`{"count": 0}` still reports `0` with `counted: True`, pinned as a control - the
fix must not turn every zero into "unknown".

**Contract change worth flagging to the lead:** the tool's return grew a
`counted` key and `count` may now be `null`. `get_notification_count` has no
in-repo programmatic consumer (grep: only the tool wrapper), so nothing
downstream needed updating.

---

## Defect 3 - CONFIRMED, a permanent ImportError

### What I measured

In the repo venv, deterministic, no network:

```
>>> from naukri_server.tools.mock_interview import naukri_mock_interview
ImportError: cannot import name 'naukri_mock_interview' from
'naukri_server.tools.mock_interview'
```

The module exposes `naukri_mock_interview_topics`, `_history`, `_prep`,
`naukri_start_mock_interview`, `naukri_answer_mock_interview` - the atomic tools
the de-consolidation produced. The dispatcher is gone.

`application_service._safe_fetch_mock_topics` still named the dispatcher. Its
`except Exception` caught the ImportError and dressed it as an API error, so
EVERY `naukri_interview_prep` call returned `partial_success` with `mock_topics`
listed in `missing_sections`, for every job, permanently.

### What I changed

`naukri_server/services/application_service.py:262-278` - the lazy import now
names `naukri_mock_interview_topics`, which wraps the same `_get_topics` through
`handle_tool_action` and returns the same `{status, total, count, topics, roles}`
shape the caller already reads.

### Control, shown failing

`tests/test_interview_prep_call_sites.py`, 7 tests. Pre-fix RED, 5 of 7. The
rename-proofing one is the load-bearing message:

```
    def test_every_lazily_imported_tool_resolves(self):
        ...
>       assert not broken, "\n".join(broken)
E       AssertionError: _safe_fetch_mock_topics imports naukri_mock_interview
E       from naukri_server.tools.mock_interview, which does not exist
```

and end to end:

```
>       assert prep["status"] == "success"
E       AssertionError: assert 'partial_success' == 'success'
>       prep["mock_topics"]
E       KeyError: 'mock_topics'
```

That last test is the pin the brief asked for: it walks all three
`_safe_fetch_*` helpers with `ast`, resolves every `from X import Y` they
perform lazily, and asserts the name exists. All three helpers hide their
imports inside `try/except Exception`, so a wrong name is invisible until it
reaches a live call and then arrives dressed as somebody else's failure. A
scanner-coverage control asserts it actually sees all three helpers.

Mutation planted after the fix (renamed the import target to
`naukri_mock_interview_TYPO`), 5 of 7 went RED with:

```
E       assert not ['_safe_fetch_mock_topics imports naukri_mock_interview_TYPO
E       from naukri_server.tools.mock_interview, which does not exist']
```

Restored byte-for-byte; `application_service.py` SHA-256 back to
`5eac896bc3a1fd6cb82f95344bcbdd4b1a276c39718d03c292487706ffc491b1`.

### After state

`interview_prep` returns `status: "success"` with `mock_topics` populated and no
`errors`/`missing_sections`. A genuinely broken tool still surfaces as an error
dict naming the exception - pinned as a control.

---

## Defect 4 - CONFIRMED, but the mechanism differs from the brief

### What I measured

**Instrument 1 - the gateway itself.** Two direct read-only GETs to the same url
`ab_get_work_culture` builds (review distribution, company id 1), spaced more
than two seconds apart, no cookies:

```
no headers                 -> HTTP 400, 29 bytes
                              body: "AppId or SystemId not present"
appid + systemid headers   -> HTTP 200, 687 bytes, JSON carrying reviewsCount,
                              summaryText, workMonitorLabels, workMonitorSeries
                              - the exact fields the helper reads
```

`_ab_rest_get` sent NO headers at all (`ambitionbox_rest.py:82-83` pre-fix), and
no AmbitionBox appid constant existed anywhere in the repo. Confirmed.

**Instrument 2 - end to end.** ONE read-only `naukri_company_intel` call on a
public company, `intel_type="interviews"`:

```
status              -> "success"
ab_rest             -> ABSENT
_enrichment_errors  -> work_culture: ClientResponseError: 403, 'Forbidden'
                       benefits:     ClientResponseError: 403, 'Forbidden'
                       competitors:  ClientResponseError: 403, 'Forbidden'
```

So the consequence the brief derived is real: all seven REST helpers are dead,
`ab_rest` never appears, and the tool still reports success.

**Where the brief's mechanism needs correcting - three points.**

1. The live failure is **403, not 400**. 403 IS inside the existing retry branch
   at `:84`, so the bridge does not skip the retry as predicted - it burns a
   browser navigation refreshing cookies, retries, gets 403 again, and raises.
   The 400 only appears on the cookieless path my curl took. Same outcome, one
   extra browser navigation per helper per call.
2. `_enrich_with_rest` does not fully swallow the failure - it does record
   `_enrichment_errors` and that key IS returned to the caller. What it does is
   worse in a subtler way: it logs at `logger.debug`, keeps
   `status: "success"`, and parks the reason under an underscore-prefixed key
   that reads as an internal detail.
3. **Live AmbitionBox probing STOPPED at that 403** under this wave's rate rule.
   I fired no further AB requests. That means whether the headers ALONE clear
   the 403 path is **NOT VERIFIED**. Two mechanisms remain open and I cannot
   separate them without another live request:
   - M1: missing appid/systemid, with the gateway's rejection code varying by
     request shape (400 cookieless, 403 with browser cookies attached).
   - M2: the browser cookies themselves draw the 403, independent of headers.
   An IP-level block is ruled out: the same box got a 200 from the same
   endpoint seconds earlier once headers were attached.
   The headers are required under either mechanism - that part is measured -
   so adding them is correct regardless. **This is the residual for the lead.**

### What I changed

- `naukri_server/tools/ambitionbox_rest.py:41-77` - new `AB_REST_HEADERS`
  (`appid` + `systemid`) and `AbRestGatewayError(RuntimeError)`. The comment
  states the provenance plainly: these are NOT discovered constants, no AB appid
  exists in this repo, and the gateway was measured to check only that the two
  headers are PRESENT. The exact pair recorded there is the pair I measured
  returning 200.
- `naukri_server/tools/ambitionbox_rest.py:128` - every request now sends them.
- `naukri_server/tools/ambitionbox_rest.py:130-141` - a 400 raises
  `AbRestGatewayError` carrying the gateway's own message, instead of falling
  into the cookie-refresh retry. Cookies cannot fix a missing header and the
  refresh costs a real browser navigation. `AbRestGatewayError` is a
  `RuntimeError`, deliberately NOT an `aiohttp.ClientError`, so it cannot be
  caught by that retry branch. The 401/403 cookie retry is untouched.
- `naukri_server/tools/ambitionbox.py:791-822` - `_enrich_with_rest` now always
  sets `ab_rest_status` ("ok" | "degraded" | "failed") when a company id was
  found, publishes `ab_rest_errors` as a readable non-underscore channel
  (`_enrichment_errors` kept for back-compat), logs at WARNING instead of DEBUG,
  and records a non-success dict result that previously fell through the loop
  with no trace at all.
- `naukri_server/tools/ambitionbox.py:747` and `:846-850` - both docstrings
  stopped advertising the swallowing ("Failures are silently swallowed",
  "failures are silent").

**Design decision, stated for review.** `status` is deliberately NOT demoted to
`partial_success` on a dead bridge. `application_service._safe_fetch_company_intel`
gates on `status == "success"`, and `interview_prep._unpack` discards a
non-success section wholesale - so demoting would throw away the
browser-scraped company rating and sample questions because a documented
"supplementary, best-effort" enrichment failed. That trades one silently-wrong
answer for another. `ab_rest_status: "failed"` is a top-level field that cannot
be misread as a measurement, which is the same shape used for `counted` in
defect 2 and `listed`/`not_listed` in defect 1. If the lead wants the status
demotion instead, `_unpack` needs widening in the same change.

### Control, shown failing

`tests/test_ab_rest_bridge_auth.py`, 20 tests, all offline against a fake
aiohttp transport - they fire zero live AmbitionBox traffic. Pre-fix RED, 12 of
18 collected at that point:

```
    async def test_appid_and_systemid_are_sent(self):
        await _run_get([(200, {"data": {}})])
        assert len(_FakeSession.captured) == 1
>       assert not _missing_auth_headers(_FakeSession.captured[0])
E       AssertionError: assert not True
E        +  where True = _missing_auth_headers({'cookies': {}, 'headers': None,
E            'params': None, 'url': '...'})
```

`headers: None` on the wire is the defect stated as a measurement.

Mutation planted after the fix (the `headers=dict(AB_REST_HEADERS)` argument
deleted from the `session.get` call at `:128`), 2 tests went RED with the
identical `headers: None` message above.

Restored byte-for-byte; `ambitionbox_rest.py` SHA-256 back to
`14142eea7a85f59e56c78e9114fbf50235c56fbcdea72a70ac69a3a1479f37d3`.

Three of the tests are instrument controls: `_missing_auth_headers` is shown
flagging a header-free request, flagging a HALF-authenticated one, and staying
silent on a good one. Four more are behaviour controls: a 403 must STILL retry
with fresh cookies, a healthy bridge must report `"ok"` with no error key, a
result with no company id must be `skipped` rather than `failed` (never having
tried is a different claim from having failed), and the docstring scanner is
shown both catching the original wording and allowing prose that merely
describes the defect.

### After state

Verified offline: the request carries `appid` and `systemid`, cookies still ride
alongside, a 400 raises with the gateway's message after exactly one cookie
fetch, a 403 still gets its retry, and a dead bridge reports
`ab_rest_status: "failed"` with `ab_rest_errors` and a WARNING log.

**NOT verified: the live after-state.** The MCP server (PID 43228) holds the
code it started with and must not be restarted, so no post-fix live
`naukri_company_intel` is reachable from this session; and AB probing is stopped
on the 403 regardless. Someone should re-run one `naukri_company_intel` call
after the server restarts and check for `ab_rest` / `ab_rest_status`. If it
still says `failed` with 403s, mechanism M2 is the live one and the cookie path
is the next thing to look at.

---

## Test suite

```
venv\Scripts\python.exe -m pytest tests/ -q
3213 passed, 1 failed, 8 deselected in 215.55s
```

Baseline was 3158 passed, 8 deselected, 0 failed. Reconciled exactly:

| source | passed | failed |
|---|---|---|
| baseline | 3158 | 0 |
| this slice - 4 new control files | +45 | 0 |
| sibling agent - `tests/test_boost_rest_path.py` (untracked, not mine) | +11 | 0 |
| sibling agent - in-flight `naukri_server/tools/profile_update.py` edit | -1 | +1 |
| **total** | **3213** | **1** |

3158 + 45 + 11 = 3214 run; 3213 pass, 1 fail.

**The one failure is not mine.**
`tests/test_profile_deep.py::TestProfileInvalidationOnUpdate::test_profile_invalidation_on_update`
fails inside `naukri_boost_profile`, whose log line comes from
`naukri_server/tools/profile_update.py:783` - a file this slice was explicitly
forbidden to touch and which the sibling agent has 48 added / 25 removed lines
in, uncommitted. It fails in ISOLATION as well as in the full run, so it is not
an interaction with anything here. Flagging it to the lead, not fixing it.

`tests/test_read_path_purity.py`'s frozen `EMIT_CENSUS` is GREEN - no emitter
was added, moved or removed by this slice, so no tuple is needed.

The 8 deselected are unchanged: `addopts = -m "not e2e"` in `pytest.ini`. No
live-network test was added to the suite; the AB after-state proof was
deliberately left out of it rather than parked behind an `e2e` marker.

---

## Files changed

Source (6, all within this slice's ownership; none of the frozen or
sibling-owned files were touched):

```
naukri_server/tools/daily_brief.py                 +57  -4
naukri_server/services/daily_brief_service.py      +21  -3
naukri_server/services/notification_service.py     +40  -2
naukri_server/services/application_service.py      +14  -2
naukri_server/tools/ambitionbox_rest.py            +61  -4
naukri_server/tools/ambitionbox.py                 +52  -4
```

Tests (4 new files, 45 tests):

```
tests/test_unread_count_honesty.py         10 tests
tests/test_notification_count_honesty.py    8 tests
tests/test_interview_prep_call_sites.py     7 tests
tests/test_ab_rest_bridge_auth.py          20 tests
```

Also present in `git status` and NOT from this slice: `_sweep/final_verdicts.py`,
`_sweep/spec.py`, `naukri_server/tools/profile_update.py`,
`probing/discovery-report.md`, `tests/test_boost_rest_path.py`.

## Line endings

Worth recording because the brief's LF instruction reads differently once you
look at the repo. `core.autocrlf=true` is set globally, there is no
`.gitattributes`, and EVERY blob in HEAD is LF-only - so a CRLF working tree is
what git itself checked out, not corruption. Four of the six files I edited were
already CRLF on disk at HEAD (`daily_brief.py`, `daily_brief_service.py`,
`ambitionbox.py`, `ambitionbox_rest.py`) and two were LF
(`notification_service.py`, `application_service.py`).

All edits were made byte-level through a helper that detects each file's own
dominant EOL and matches it, so no file changed convention. Verified by
`git diff --numstat`: every changed file shows only its intended line counts,
with no whole-file churn. All new test files are LF and strict ASCII; every
replacement snippet written into a source file was ASCII-checked before it was
applied.

## Zero writes

No write fired against naukri.com. The live calls made were:
`naukri_auth_status`, `naukri_notification_count`, `naukri_list_inbox` x2,
`naukri_debug api_fetch` (GET), `naukri_company_intel` x1, and two direct
read-only GETs to the AmbitionBox gateway spaced more than two seconds apart.
AmbitionBox probing stopped on the first 403 and did not resume. The live MCP
server was neither restarted nor killed.
