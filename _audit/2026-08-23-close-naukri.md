# naukri close - 2026-08-23

**Commits** `fb6c48b` (alerts + interviews + debug docs + auth bridge), `dba9df4` (read-back),
`33fd25c` (external-apply fields). **CI green**, run `32625682803` on `33fd25c`.
**2930 -> 2979 passed, 8 deselected.**

1. **`naukri_create_alert` WAS BROKEN, in three ways, and the worst was that it could not
   tell.** It returned `{"status": "success"}` and emitted `AlertCreated` for any response
   its POST did not raise on - measured red on an empty body with an empty alert list. Its
   endpoint `/alertapi/v2/ssa` entered in `863e9a4` from **webpack analysis** and was never
   watched creating anything; its response parser read `totalRes` at the top level, where the
   measured payload never carries it (`info.totalRes`), so `total_matched` was always None.
   Four converging measurements settle the endpoint: a same-minute GET differential
   (`/alertapi/v1/cja` -> **405** app JSON code 4051; a bogus sibling -> **404** code 4041;
   `/alertapi/v2/ssa` -> **"Failed to fetch"**, no response); all **3** live alerts are
   `alertType: "cja"` and none is `ssa`; the one alert with `keywords: null` carries exactly
   the defect the v1/cja create probe logged; and v1/cja is the only path ever seen issuing a
   `searchId`. Now: endpoint switched, `info.totalRes` parsed, both `keyword` and `keywords`
   sent, and success **asserted** on a server-issued id or a read-back showing an id that was
   **not there before** - name-matching alone would have called a pre-existing alert a
   creation, and his account already carries `"Test Alert Probe"`. Neither -> `API_ERROR`
   naming what was checked, and **no event**. It now works or refuses; it cannot lie.
   *Caveat: I never executed a create. POST probes were blocked by this session's classifier,
   so the verdict rests on read-only live probes plus code and archived measurements - the
   verification gate is what makes a wrong endpoint refuse instead of lie.*
2. **Interview scheduling is local-only, confirmed independently, and the tools now say so.**
   I read a live application myself: its timeline is exactly Applied / Application Sent /
   Application Viewed (`status_id` 1, 2, 4 - id 3 never observed, and the server passes
   `statusValue` through with no map to consult). Both docstrings now open by saying nothing
   reaches Naukri, both payloads carry `scope: "local"`, and
   `tests/test_interview_surface_absent.py` pins the absence (4 of its 9 red against the old
   source). It also pins the correction that "local write" is exact while "no network call"
   would be false - the emitted event fetches AmbitionBox over HTTP.
3. **OVERTURNED: crawled jobs exist and now have their apply fields.** Over **N = 124**
   distinct jobs from the five surfaces that answered 200 (recommendations 63, early-access
   50, applied history 11): `mode` = jp 54 | resdexSearch 50 | airex 5 | **crawled 4** |
   absent 11; `companyApplyJob` = false 58 | **TRUE 5** | absent 61; `clientCareersUrl` **9
   non-null**. All 4 crawled jobs carry `companyApplyJob: true`. Raw search 406'd again and
   **all 26** `/jobapi/v3/job/{id}` GETs 406'd - the detail endpoint is bot-gated over REST
   entirely, which is why the previous pass could not reach a crawled job. `clientCareersUrl`
   is **not** an external site: `seedling-labs-jobs-careers-25659` resolves on naukri.com and
   redirects to the *posting consultancy's* page. Surfaced as `listing_mode`,
   `is_company_apply`, `client_careers_url`; `is_company_apply` gets **no False default**
   because 61 of 124 payloads carry no such key. 12 tests, all red first, verified end to end
   on the captured payload.
4. **Both flagged hazards closed.** `naukri_debug`'s `discover_click` docs were backwards and
   the parser was right, so the docs moved; the parse order is now pinned behaviourally (3 of
   5 red against the old docstring). And an **independent recount** of the leak census returns
   **99 total / 24 platform-dependent / 0 without a platform-independent sibling** - the
   headline holds, reconciled member-by-member against the baseline's own rows (the 75 -> 99
   drift is `d824b41`'s +7 plus the walker's own meta-tests).
5. **Found in flight: the cross-process auth bridge was shut 91.7% of the time.**
   `get_auth_state` rejected any `auth_state.json` older than 300s, but that file is rewritten
   only on a token refresh and the token lives 3600s - measured at 2127s old holding a token
   with 1469s left, working. It now gates on the token's own `exp` with a 120s margin, keeping
   file age only where the claim will not decode. This is the mechanism every future
   cross-process agent depends on to avoid fighting for the Chrome profile lock.
6. **Not run, deliberately:** no apply / batch_apply / apply_top_fits / auto_hunt /
   agent_run_now / accept_nvite / purge / report_fraud, no profile, settings, resume or photo
   write, and **no alert created, updated or deleted**. Exactly one POST all round - an
   empty-body read to `/jobapi/v2/search/recom-jobs`, gated on the repo's own
   `_POST_ONLY_PATHS` allowlist and identical to what `naukri_get_recommendations` already
   issues. Browser access serialised throughout; no image-wide kills.
7. **The running MCP server is now STALE** - it holds `5942c7e` while disk is `33fd25c`.
   `naukri_create_alert`, the interview docstrings and the three new job fields are not live
   in it until it is restarted.
