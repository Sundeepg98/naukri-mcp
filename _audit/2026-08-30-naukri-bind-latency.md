# Naukri MCP -- startup latency: the socket was never the problem

**Date:** 2026-08-30 | **Branch:** `master` | **Commit:** `404f4883`
**Scope:** `D:\Sundeep\projects\job-hunting\mcp-servers\naukri` only. The live scheduled
task was never stopped, Chrome was never launched against `chrome-profile/`, and port 8321
was never touched.

---

## 1. The mechanism, restated after verifying it

I was briefed that `--http` spends 5-14 minutes before the socket binds, because uvicorn's
`Server.startup()` awaits `self.lifespan.startup()` before `loop.create_server(...)`, and
the naukri lifespan launches Chrome. **The first half of that is right about uvicorn and
wrong about which lifespan uvicorn runs.** The correction matters, because it moves the fix.

### What is true

`venv/Lib/site-packages/uvicorn/server.py`, `Server.startup()` does await the ASGI lifespan
first and bind second -- that part of the brief holds and I re-read it.

### What is not

The ASGI lifespan of a FastMCP app is **not** the naukri lifespan. In
`venv/Lib/site-packages/mcp/server/fastmcp/server.py:1044`, `streamable_http_app()` builds:

```python
return Starlette(..., lifespan=lambda app: self.session_manager.run())
```

`StreamableHTTPSessionManager.run()` (`mcp/server/streamable_http_manager.py:87`) only
creates an anyio task group. The naukri `lifespan` is passed to FastMCP as the **lowlevel
MCP Server** lifespan (`fastmcp/server.py:212`), and that one is entered at
`mcp/server/lowlevel/server.py:657`, inside `Server.run(...)` -- which the session manager
starts **once per MCP session** (`_handle_stateful_request` -> `run_server` -> `self.app.run`).

`Server.run()` enters the lifespan **before it reads the first message off the stream.** So
the expensive startup does not block the bind; it blocks the first client's `initialize`.

### Measured, not argued

| measurement | result |
|---|---|
| `import naukri_server` (warm) | **9.2 s** |
| `naukri.py --http` on spare port 8399, time to TCP accept | **8.6 s** -- and no Chrome, no Playwright driver process |
| scheduled task definition | `venv\Scripts\python.exe -u naukri.py --http`, cwd = repo -- so `--http`, confirmed |
| live server PID 15952 (`python3.13.exe`, re-exec'd by the Store-Python shim PID 14016) | started **10:01:32** |
| `chrome-profile.lock` mtime (written by `profile_lock.acquire()`, first statement of `browser.start()`) | **10:09** |
| `chrome-profile/Default/IndexedDB/https_www.naukri.com_0.indexeddb.leveldb` | **10:11:22** |
| `mcp-remote http://127.0.0.1:8321/mcp` client process | started **10:16:12** |

`browser.start()` began ~7.5 minutes after process start, which is impossible if it ran
before the bind -- nothing would have been listening for a client to connect to. It is
exactly what "the lifespan runs on the first session" predicts.

### So the defect is

> Under `--http`, the first MCP session's `initialize` request sits unread in the transport
> for the entire duration of Chrome launch + JSON->SQLite migration + watchdog + 21 health
> probes + 10 scheduled tasks + a startup health check. The client times out and drops all
> 120 tools. The server is listening, healthy, and unable to answer the one request that
> has to be answered first.

The user-visible failure the brief describes is real and this is its cause. The fix has the
same shape the brief asked for -- **answer first, warm up after** -- applied one layer in.

### End-to-end proof of the defect and the fix

A real `--http` server on port 8399, `browser.start()` replaced by `await asyncio.sleep(60)`
(no Chrome, `NAUKRI_DATA_DIR` in temp), driven with a genuine JSON-RPC `initialize`:

| mode | seconds to TCP accept | first bytes of response | **`initialize` RESULT arrives** |
|---|---|---|---|
| **defect** (warm-up awaited inline) | 3.69 | SSE keep-alive `: ping` at 15.0 s | **60.06 s** -- i.e. the full browser-start duration |
| **fixed** (warm-up detached) | 3.07 | the result itself | **0.02 s** |

The 15-second ping under the defect is the honest detail: a client does not even get an
error, it gets keep-alives until it gives up.

---

## 2. Rulings

### 2.1 Wait vs refuse -- **bounded wait of 20 s, then refuse**

`naukri_server/readiness.py`:

```python
WARMUP_TOOL_WAIT_SECONDS = 20.0
```

Three reasons, in the code as a comment:

1. **A warm start is the common case.** Profile in the page cache, Chrome up in single-digit
   seconds. Refusing instantly turns every ordinary restart into a burst of errors a human
   has to interpret, to save a few seconds of patience.
2. **A cold start is not coverable by any client-safe wait.** Measured today on the live
   server: `browser.start()` ran 10:09 -> 10:11:22, over two minutes. Nothing short enough
   to be safe covers that, so past the bound the only honest answer is
   `SERVER_WARMING_UP, phase=browser, elapsed=Ns`.
3. **The ceiling is the client's, not ours.** A client that stops waiting mid-call turns our
   patience into a dropped tool -- the exact failure being fixed. 20 s sits under the 30 s
   floor MCP clients conventionally allow a request, and far under this server's own
   `TOOL_WATCHDOG_TIMEOUT` (600 s).

It is a **pre-flight** wait, not a retry-after-failure: `handle_tool_action` and `api_tool`
both wait *before* running the tool body. A retry would re-execute whatever the tool already
did before its first browser touch, and in this server that can include an application write
or a saga step. Waiting first is the only version safe to do unconditionally.

It waits for the **browser phase only**, not for full readiness -- probes and the scheduler
come up behind it and no tool call is blocked on them.

**Accepted cost, stated plainly:** browser-free tools (config reads, DB reads, scheduler
status) also pay this wait during the startup window. There is no per-tool "needs browser"
declaration in this codebase to key off; inventing one across 120 tools buys a few seconds
of latency at the price of a classification that would rot. The window is seconds and the
tools still succeed afterwards.

### 2.2 Eager vs lazy browser start -- **eager, in the background**

1. `BrowserWatchdog` and the 21 health probes are built to observe a *running* browser.
   Lazy start makes "browser down" indistinguishable from "browser never asked for" for the
   whole idle period, and the watchdog's restart logic would fight the lazy start.
2. **The problem lazy would solve is already solved.** `browser.py` has an idle REAPER that
   SUSPENDS the context (commit `d8483721`, "it launched once and never came down"), and
   `PagePool.acquire` is already the single resume door. Lazy start would be a second,
   redundant mechanism competing with the reaper for the same job.
3. The 10 scheduled tasks and the autonomous agent run with **no client attached**. They need
   the browser without anyone calling a tool.
4. Eager-in-background costs nothing on the critical path, which is the entire point.

### 2.3 The startup health check -- **removed, not relocated**

It ran `naukri_health_check(include_browser=False)` and did nothing with the result but log
it. Deleted because:

1. **Nothing consumed it.** No branch, no gate, no readiness signal -- one log line, on a
   server whose stderr goes to a scheduled task's discarded console.
2. **Redundant by construction.** `HealthProbeScheduler` starts 21 probes two statements
   earlier; a one-shot sample at t=0 says nothing the first probe sweep does not.
3. **It is the shape of the 2026-08-20 wedge already documented in `__init__.py`** -- a stuck
   `naukri_health_check` took an unrelated tool down with it. Putting that one known
   wedge-prone handler between the server and its own readiness is the wrong trade.

`naukri_health_check` remains an on-demand tool, which is where a health check someone
actually reads belongs.

---

## 3. Not-ready is now said out loud

Two doors into the browser, both previously giving a **wrong answer that looked right**:

| door | before, during warm-up | after |
|---|---|---|
| `_NoPagePool.acquire()` | `BROWSER_ERROR: "Browser is not running -- no page pool. Call naukri_login..."` -- there is nothing to log in to, Chrome is mid-launch | `SERVER_WARMING_UP` + phase + elapsed |
| `TokenManager.ensure_token()` | `NotLoggedInError("Not logged in -- call naukri_login first")` -- the stored session is fine; acting on this starts a login that fights the launch for the profile lock | `SERVER_WARMING_UP` |

`ServerWarmingUpError` **subclasses `BrowserUnavailableError`** so ~30 existing catch sites
and `error_handler._BROWSER_ERRORS` stay correct; the two dispatch seams test for it first.

Both seams are covered, not one: `handle_tool_action` **and** `api.api_tool`. Without the
second, a REST tool during warm-up answered `API_ERROR` -- naming the wrong subsystem.

`naukri_health_check` short-circuits during warm-up: five API checks all blocked on the same
unstarted browser produced five separate failures, which reads as an API outage. One cause,
one answer.

**No ETA is published.** `snapshot()` returns `eta_seconds: None` and a `retry_after_seconds`
hint. A first warm-up after a process start has no history to predict from, and that is
precisely the case that matters. A fabricated ETA is the same class of lie as an empty list.

Error payload shape:

```json
{"status": "error", "error_code": "SERVER_WARMING_UP",
 "message": "Server is still warming up (phase=browser, elapsed=6s). The browser is not
             available yet. This is NOT a login failure and NOT an empty result -- retry in ~20s.",
 "readiness": {"state": "warming", "phase": "browser", "elapsed_seconds": 6.0,
               "browser_ready": false, "retry_after_seconds": 20,
               "eta_seconds": null, "error": null}}
```

---

## 4. `--dual` and `--stdio`

**This change is NOT `--http`-only, and saying so would be false.** The lifespan is shared by
all three transports, so all three now answer `initialize` immediately and warm up behind it.
`naukri.py` itself is **unmodified** (`git diff` touches no file outside `naukri_server/` and
`tests/`).

`_lifespan_refs` / `_lifespan_lock` semantics are unchanged and one property improves: the
lock is now held for milliseconds instead of across the whole Chrome launch, so in `--dual`
session 2 no longer queues behind session 1's browser start. Pinned by
`test_second_session_does_not_block_behind_the_first_warm_up`, which also asserts a second
entry does not start a second warm-up and that refs return 2 -> 1 -> 0.

Shutdown waits `WARMUP_SHUTDOWN_GRACE_SECONDS = 30.0` for an in-flight warm-up before tearing
down, so `stop()` never runs against a half-launched Playwright context. On cancellation the
profile lock is still released, by `NaukriBrowser.stop()`'s own `_holds_profile_lock` branch.

---

## 5. Every check shown failing at the mutation it catches

Run in a throwaway git worktree at `HEAD` with the fix's files copied in, one mutation at a
time, then reverted. Harness:
`...\scratchpad\mutate.py`; full output: `...\scratchpad\mutations.txt`.

**11 mutations, 11 reds. Unmutated: `11 passed in 2.97s`.**

```
M1  the defect: warm-up awaited inline on the critical path
>       await asyncio.wait_for(cm.__aenter__(), timeout=2.0)
E                   TimeoutError

M1b same defect, second-session symptom
>       await asyncio.wait_for(first.__aenter__(), timeout=2.0)
E                   TimeoutError

M1c the startup health check put back on the startup path
>           assert stub_warm_up["health_check_calls"] == 0
E           assert 1 == 0

M2  _NoPagePool.acquire raises one unconditional error again
E       AssertionError: {'error_code': 'BROWSER_ERROR', 'message': 'Browser unavailable:
        BrowserUnavailableError: Browser is not running - no page pool. Call naukri_login
        to start a session, ...'}
E       assert 'BROWSER_ERROR' == 'SERVER_WARMING_UP'

M3  ensure_token loses its warm-up guard
>           raise NotLoggedInError("Not logged in - call naukri_login first")
E           naukri_server.browser.NotLoggedInError: Not logged in - call naukri_login first

M4  handle_tool_action loses the pre-flight wait
E       AssertionError: {'error_code': 'INTERNAL_ERROR',
        'message': 'Internal error: AssertionError: body ran before the browser was up'}
        != {'status': 'success'}

M5  wait_for_browser waits forever instead of to a bound
>       out = await asyncio.wait_for(handle_tool_action(tool_body, "test.bounded"), timeout=5.0)
E                   TimeoutError

M6  a failed warm-up stops releasing its waiters
>       assert released is True, "the parked caller was left waiting on a dead warm-up"
E       AssertionError: the parked caller was left waiting on a dead warm-up
E       assert False is True

M7  api_tool loses its SERVER_WARMING_UP branch
E       AssertionError: {'error_code': 'API_ERROR', 'message': 'Fetch something failed:
        ServerWarmingUpError: Server is still warming up ...'}
E       assert 'API_ERROR' == 'SERVER_WARMING_UP'

M9  naukri_health_check loses its warm-up short-circuit
>       assert out["error_code"] == "SERVER_WARMING_UP", out
E       KeyError: 'error_code'
        (it ran all five API checks and returned the ordinary success-shaped
         payload instead, with no error_code at all)

M8  browser_pending becomes true for a never-started server
>       assert readiness.browser_pending is False
E       assert True is False
```

### One check was admitted only after it was rejected

`test_failed_warm_up_releases_waiters_immediately`, as first written, **PASSED under its own
mutation** -- `1 passed in 1.59s` with `browser_up()` deleted from `Readiness.failed`. It
tested a caller arriving *after* the failure, which short-circuits on
`_state != STATE_WARMING` and never looks at the event. It certified nothing. Rewritten as
`test_failed_warm_up_releases_a_caller_already_waiting`, which parks a waiter inside the
wait first -- that is the only caller the event matters to. It now fails as shown at M6.

---

## 6. Test counts

| run | result |
|---|---|
| **BEFORE** -- pristine worktree at `90ed4618` | **3977 passed, 3 skipped, 8 deselected** in 177.54 s |
| **BEFORE** -- same tree in place (before edits) | **3980 passed, 8 deselected** in 165.84 s |
| **AFTER** -- with the fix + 11 new tests | **3991 passed, 8 deselected** in 204.65 s |

3980 -> 3991 is exactly +11, the new file. The 3-vs-0 skip difference is the worktree lacking
the repo's data files; both runs are green either way.

**The brief's "~2537 tests" is stale -- the measured baseline is 3980.** I report what I
measured. Note that `pytest.ini` carries `addopts = -m "not e2e"`, which is where the 8
deselected come from.

---

## 7. Files

| file | change |
|---|---|
| `naukri_server/readiness.py` | **NEW** (270 lines). Dependency-free state machine: phases, `browser_pending`, bounded `wait_for_browser`, `snapshot()`, `describe()`. Both timing constants live here with their reasoning. |
| `naukri_server/__init__.py` | `lifespan` keeps only the ref-count + `init_db()` (0.099 s measured) and detaches `_warm_up()`. `_warm_up()` holds the old body, phase-annotated. Startup health check deleted. Shutdown awaits the warm-up. Readiness instance imported **aliased** as `_readiness` -- the bare name shadows the submodule, the same trap already documented for `browser` two imports above. |
| `naukri_server/browser.py` | `ServerWarmingUpError(BrowserUnavailableError)`; `_NoPagePool.acquire()` and `TokenManager.ensure_token()` say "warming up" instead of "call naukri_login". |
| `naukri_server/error_handler.py` | Pre-flight wait; `except ServerWarmingUpError` **before** the browser/auth branches. |
| `naukri_server/api.py` | Same pre-flight and branch on the `@api_tool` seam. |
| `naukri_server/tools/health.py` | Warm-up short-circuit; `readiness` in the normal payload. |
| `tests/test_startup_warmup.py` | **NEW** (439 lines), 11 tests. |

Deliberate non-change: `naukri.py`. Nothing in the transport wiring needed touching.

### `git status --porcelain` at finish

```
 M naukri_server/__init__.py
 M naukri_server/api.py
 M naukri_server/browser.py
 M naukri_server/error_handler.py
 M naukri_server/tools/health.py
?? naukri_server/readiness.py
?? tests/test_startup_warmup.py
```

No `naukri.db`, `naukri.db-wal`, `naukri.db-shm`, `sync_state.json`, `healing_state.json` or
`chrome-profile/` entry. The temporary worktree used for the baseline and the mutation runs
was removed (`git worktree list` shows only the repo).

### Live server left alone

`PID 15952` alive, started 10:01:32, still LISTENING on `127.0.0.1:8321`. Probe port 8399
free. No Chrome process against `chrome-profile/`. All probe subprocesses were killed by
exact PID -- never an image-wide kill.

---

## 8. Live confirmation for the operator (after a restart)

**Not run by me.** The change only takes effect on a restart, which is the operator's call.

```powershell
# 1. Where it stands now, before anything is restarted.
Get-ScheduledTask -TaskName "Naukri MCP Server" | Select-Object TaskName,State
Get-NetTCPConnection -LocalPort 8321 -State Listen |
    Select-Object LocalAddress,LocalPort,OwningProcess

# 2. Restart, and time BOTH milestones from one t0.
Stop-ScheduledTask -TaskName "Naukri MCP Server"
Start-Sleep -Seconds 3
$t0 = Get-Date
Start-ScheduledTask -TaskName "Naukri MCP Server"

#    (a) time to accept a TCP connection
$bound = $null
while (((Get-Date) - $t0).TotalSeconds -lt 300) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", 8321); $c.Close()
        $bound = ((Get-Date) - $t0).TotalSeconds; break
    } catch { Start-Sleep -Milliseconds 250 }
}
"seconds_to_bind = {0:N2}" -f $bound      # EXPECT ~9-15s (module import); unchanged by this fix

#    (b) THE NUMBER THIS FIX IS ABOUT: time to an initialize RESULT.
$body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
$t1 = Get-Date
$r = Invoke-WebRequest -Uri "http://127.0.0.1:8321/mcp" -Method Post -Body $body `
        -ContentType "application/json" `
        -Headers @{ Accept = "application/json, text/event-stream" } `
        -TimeoutSec 120
"seconds_to_initialize = {0:N2}" -f ((Get-Date) - $t1).TotalSeconds
$r.Content.Substring(0, [Math]::Min(300, $r.Content.Length))
```

**Expected:** `seconds_to_initialize` well under a second, and the body containing
`"result":{"protocolVersion"...,"serverInfo":{"name":"naukri"...`. Before this fix that call
returned only SSE `: ping` lines until the browser finished launching.

```powershell
# 3. Watch the warm-up finish, without provoking an error.
#    While warming this returns error_code SERVER_WARMING_UP with a phase that ADVANCES
#    (browser -> migration -> watchdog -> probes -> scheduler); when done, state "ready".
#    Run it a few times over the first minute.
#    (Through any MCP client attached to the server:)   naukri_health_check
```

**What would mean it did NOT work:** a `naukri_health_check` whose `readiness.phase` never
changes, or `state: "failed"` with an `error` -- that is a warm-up dying, and the `error`
field names where.

---

**Commit:** `404f4883` -- `fix(startup): initialize waited behind a Chrome launch, so clients dropped every tool`
(this audit file lands in the follow-up commit, so that it can name the sha above)
