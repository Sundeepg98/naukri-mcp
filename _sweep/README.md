# e2e sweep harness

Speaks MCP over the server's streamable-HTTP endpoint and calls every registered
tool with sensible arguments, capturing the result plus an attributed local-state
delta. Built 2026-08-22 for the first end-to-end run of all 120 tools against the
real account.

    python _sweep/run_sweep.py                 # every CALL-listed tool
    python _sweep/run_sweep.py naukri_config   # just these
    python _sweep/analyze.py [verdict]         # counts, then one class
    python _sweep/final_verdicts.py            # verdicts incl. manual overrides

Files: `mcpclient.py` transport, `spec.py` the CALL/SKIP list (reconciles to
exactly the registered tool count), `state.py` the snapshot instrument,
`run_sweep.py` the runner, `analyze.py` / `final_verdicts.py` classification.
`write_paths.md` is the static write-path audit that decided what was safe to call.

## Three things that will bite a re-run

1. **Read the SSE frame whose id matches the request.** Tools taking a `Context`
   emit `notifications/message` progress frames BEFORE the result. Taking the
   first `data:` line makes `sync_applications` and `sync_saved` look like they
   return nothing at all -- a false "silent death" this harness reported until
   the client was fixed.
2. **Attribute state changes by ROW, not by count.** The scheduler and the
   browser watchdog write `notifications` / `event_log` / `scheduled_runs` rows
   on their own clock. During this run the watchdog was minting a `BrowserCrashed`
   notification every ~0.6s, so a bare count delta manufactures a mutation for
   whatever tool happened to be in flight.
3. **A hard 0 is not a measurement.** With the account logged out, several tools
   still emit `count: 0` / `profile_views: 0` for sub-fetches that raised. Check
   the errors channel before believing any zero.

## Safety

`spec.py` SKIPs 40 tools with a reason each -- every apply/quota path, every
profile and settings write, alert create/update/delete, mark-read, the agent
run/approve/reject family, kill switch, set_config, and the two that need the
operator at the keyboard. `naukri_debug` is called only with `browser_snapshot`;
its `api_post` / `api_put` / `api_delete` and `discover_click` actions write
upstream or click a caller-supplied selector on the live logged-in page.

Take a backup before running: the sweep calls `daily_brief`, which marks up to
10 pending notifications delivered per call, and the sync tools, whose purge step
deletes applications past the retention horizon.
