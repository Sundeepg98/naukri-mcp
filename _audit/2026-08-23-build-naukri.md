# naukri build - 2026-08-23

**Commits** `d824b41` (leak walker), `3566ab3` (inbox). **CI green**, run
`32619014979`, conclusion `success` on `3566ab3`. **2896 -> 2930 passed, 8 deselected.**

1. **LEAK WALKER: fixed for the blindness, and it had bought a false positive I fixed too.**
   All 13 planted leak shapes are caught, including every base64url-fragment class - detection
   is genuinely repaired. But 2 of the 4 needles carried no secret: the header
   `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9`, identical in every HS256 JWT, and a signature
   hardcoded to `c2lnbmF0dXJl`. Measured: `_find_leak` reported a leak of our credential when
   handed a *different* one. Over-matching does not break detection, it breaks discrimination -
   which is why round 2's control never saw it. Header excluded, signature marker-derived,
   `test_CONTROL_the_walker_is_quiet_on_a_foreign_token` pins both directions;
   `scripts/public_constant_needle_control.py` restores the old build and that control is the
   single red under it (1 failed, 123 passed).
2. **A census of all 75 leak assertions found 24 that cannot fire on ubuntu-latest**, the only
   runner gating a merge, because they hunt a Windows drive letter. 10 had no
   platform-independent sibling, so the property was unproven on CI; each now has an exact-needle
   assertion, every one shown red under a neutralised scrubber. Also: `_build_jar` planted
   `b"sealed"` in `encrypted_value` - the column a real Chrome profile actually uses - so the jar
   needle was blind to the only leak that matters there; and the 5 jar assertions, which guard a
   working login, had never been watched failing and now ship a control.
3. **A RECRUITER REPLY PREVIEW RETURNS NOTHING, BECAUSE THERE IS NO REPLY SURFACE.** Naukri
   exposes no reply, compose or thread channel to a jobseeker. GET on `/v1/inbox/users/self/reply`,
   `/v0/.../reply`, `/v1/.../mail/reply` -> **404**, against a positive control
   (`/v0/.../markInterested`, a route that does exist) -> **405** on the same verb.
   `/conversations` and `/chat` were already 404. Zero reply/compose/thread keys across 19 row +
   10 vCard + 13 job + 15 misc keys. The UI offers exactly **Apply** and **Not interested**, and
   opening a message fires **0** API calls. I did not build the tool - it would be a tool that
   cannot work - and a test pins the absence so the next person must re-run the probes first.
4. **Item 2 shipped, and the gap was 8 fields read from the wrong keys, not a platform limit.**
   Live, 20 rows, before -> after: `preview` 0->20 (now the recruiter's own opening line),
   `recruiter_name` 0->19, `experience_min/max` 0->20, `job_description` 0->20, `salary` 0->13,
   `has_screening_questions` 0->14 - **14 of 20 need answers, previously invisible**.
   **Recruiter identity IS available over REST**: the audit's 401/404 was the recruiter *profile*
   endpoint; the name sits in the list payload under `vCardInfo.vname`, a key nobody read.
   New `naukri_triage_inbox`: one call, pages, scores every row through the same jobcore engine,
   orders unread-then-fit-then-recency, reports needs_answers / already_applied /
   from_consultancies, carries no full JD, and says `scored: false` with a named error rather
   than showing null fits when the profile cannot be read.
5. **Item 3 REFUTED - `naukri_create_alert` is not broken and I did not rewrite it.** POST `{}` to
   `/alertapi/v2/ssa` returns **400 Validation Error, "Custom Job Alert name is blank."** The
   route is alive, reachable, accepts our POST, and validates the body - Akamai is not blocking
   it and the tool's field name matches what the API asks for. Verified without creating anything.
6. **Item 4 LOCAL-ONLY IS CORRECT.** No jobseeker interview surface: 4 candidate paths nonexistent
   against family-local controls, 3 live applications expose only Applied / Application Sent /
   Application Viewed, nav has no interview tab, `interview_rounds` holds 0 rows. One correction:
   `naukri_add_interview_round` is 9 functions deep with **0 network sinks**, but its emitted
   event is awaited inline and reaches AmbitionBox over HTTP - "local-only write" is true,
   "no network call" is false.
7. **Item 5 NOT EXPOSED, not built - size estimate instead.** No external/company-site URL in any
   live payload. The premise needs correcting twice: `isCrawled` is *not* dropped on the applied
   path (it is persisted to `applications.extra`; 2 of 162 rows, both false), and it is *absent
   from the search payload entirely* (0 of 20). What is really dropped is `mode`,
   `companyApplyJob` and `clientCareersUrl`, all at the 37-key whitelist
   `naukri_server/tools/job_parsing.py:30`. Surfacing them is **1 file, 3 keys, ~4 tests** - but
   `companyApplyJob` was `false` on 20 of 20 and no `mode: "crawled"` job was obtainable (raw
   search is 406 recaptcha-gated), so I would be adding three constant fields whose meaningful
   values I have never observed. Overturn it by finding one job with `companyApplyJob: true`.
8. **Two defects found by their own tests going red**: the inbox pager trusted `has_more`
   (arithmetic on the server's `total`) and re-collected one message 7 times over a 62-count
   inbox - a short page now ends the walk, with a control proving a full page still paginates;
   and the `vcard_id` lookup warned once per message on every healthy call.
9. **Not run, deliberately:** no apply / batch_apply / apply_top_fits / auto_hunt / agent_run_now
   / accept_nvite / purge / report_fraud, no alert create-update-delete, no profile write. No
   reply was sent because none can be. 4 non-mutating POSTs total, all validation probes.
10. **Two hazards for the fleet.** A bash heredoc collapsed `[\\/]` to `[\/]` in a child's regex,
    silently producing a forward-slash-only matcher - the exact trap pinned at
    `tests/test_apply_patch_path_leak.py:57`; use Write/Edit for backslash-bearing code.
    And `naukri_debug`'s docstring for `discover_click` says `"PAGE_URL|CSS_SELECTOR"` while
    `discovery_actions.py:172` parses `"CSS_SELECTOR|NAV_URL"` - the docs are backwards.
11. **Browser contention is real:** a child's `browser_snapshot` returned *my* page because we
    shared the one Chrome profile. Serialise browser access across a wave, or accept junk reads.
12. **Stale-process trap caught at the start:** the running MCP server holds `59412e72` and
    reports 121 tools while disk HEAD had 124. `inbox.py` and `config.py` were byte-identical
    between the two, which is the only reason the live inbox measurements above are admissible.
    **The server needs a restart before `naukri_triage_inbox` is callable.**
