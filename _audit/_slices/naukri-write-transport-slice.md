# Naukri profile-write TRANSPORT -- implementation slice

Date: 2026-08-24. Repo HEAD at start: `c3550d3` (branch master).
Contract implemented: `_audit/_slices/naukri-profile-write-contract.md`.

**ZERO writes were issued against naukri.com.** Every test in this slice mocks the
transport; the suite makes no network and no browser calls. Nothing was committed --
no `git commit`, `git add`, `git stash` or any history-changing command was run.
No personal data appears below: the profileId is referred to only as a 64-character
string and the tests use the synthetic stand-in `"a" * 64`.

---

## 1. Files changed

| # | File | Lines | Change |
|---|------|-------|--------|
| 1 | `naukri_server/config.py` | 173-178 (new) | Added `FULLPROFILES_WRITE_API` (the v1 path) with a 5-line provenance comment: bundle `mnj_v320.min.js` offset 44671, POST + `X-HTTP-Method-Override: PUT`, `profileId` at the body's top level, and the statement that v0 is NOT a write route for the profile editor. `FULLPROFILES_API` at :172 was **left untouched** -- its two readers (`healing/tier_registry.py:153`, `tests/test_tier_registry.py:74`) still see the v0 value they name. |
| 2 | `naukri_server/api.py` | 510-529 | `api_post`, `api_put`, `api_delete` each gained `extra_headers: Optional[dict] = None` and now forward it to `_api_request`. `_api_request` (:284) already accepted and merged it and already forwards it through all five retry recursions (:401, :445, :472, :481, :494) -- no change needed there. The new parameter sits BEFORE `_attempt`, which is safe: `_attempt` is never passed positionally anywhere (the only callers of these three wrappers are `interfaces.py`, keyword-only). |
| 3 | `naukri_server/interfaces.py` | 36, 40, 44-45 (abstract); 155, 159, 163-164 (concrete) | `post` / `put` / `delete` gained `extra_headers: Optional[dict] = None` on both the `ApiClient` ABC and `NaukriApiClient`, forwarding to `api_post` / `api_put` / `api_delete`. Styled to match the existing `get` exactly: the keyword is always forwarded, never conditionally. |
| 4 | `naukri_server/tools/profile_write.py` | NEW, 157 lines | The transport chokepoint. See section 3. |
| 5 | `tests/test_profile_write_transport.py` | NEW, 204 lines, 10 tests | The six controls plus three supporting cases. See section 4. |
| 6 | `tests/test_interfaces.py` | 58, 74, 90, 102 | Four exact-call-arg assertions updated to expect the new keyword. See section 2. |

---

## 2. ApiClient fakes -- exhaustive search

**Count of fakes/stubs implementing `ApiClient` that needed touching: 0.**
**Count of test call-arg assertions that needed touching: 4, in 1 file.**

The search was exhaustive, not a spot check:

* `grep -rn "ApiClient" --include=*.py .` (excluding `venv/`) returns **five** sites
  total: `interfaces.py:28` (the ABC), `:144` (the concrete class), `:259` (the
  singleton), `tests/test_interfaces.py` (delegation tests), and
  `tests/test_lsp_contracts.py:63` (an `issubclass` assertion). No subclass exists
  anywhere else.
* Duck-typed fakes were searched for separately by method definition:
  `grep -rn "def post(" / "def put(" / "def delete(" / "def get(self"` across the
  whole repo returns, outside `venv/`, only `interfaces.py` itself plus two unrelated
  `database.py` helpers. **No class in `tests/` or in any non-test helper defines
  `post`/`put`/`delete`.**
* Every whole-object substitution of the singleton is a `MagicMock` / `AsyncMock`
  (`test_api_probes_honesty.py:59-60`, `test_anti_detection.py:546`,
  `test_event_saga_wiring.py:139,176`, `test_integration.py:43,76`). Mocks do not
  enforce signatures, so a new defaulted keyword cannot break them.
* `tests/conftest.py:202` `mock_api_client` is a `MagicMock` with `AsyncMock`
  attributes -- same, unaffected.

The four assertions that DID break are in `tests/test_interfaces.py`. They pin the
delegation call args exactly, so forwarding the new keyword unconditionally (which is
what `get` already does) changes the recorded call:

```
tests/test_interfaces.py:58   ("/apply",    body={"job_id": "123"})       -> + extra_headers=None
tests/test_interfaces.py:74   ("/profile",  body={"name": "Test"})        -> + extra_headers=None
tests/test_interfaces.py:90   ("/item/456", body={"reason": "test"})      -> + extra_headers=None
tests/test_interfaces.py:102  ("/item/789", body=None)                    -> + extra_headers=None
```

These now match the existing `get` assertions at :30 and :41, which have always
included `extra_headers=None`.

---

## 3. The new module

`naukri_server/tools/profile_write.py` -- no section-specific logic, no MCP tool, no
event emission, no imports from `jobcore`.

| Symbol | Line | What it is |
|---|---|---|
| `METHOD_OVERRIDE_HEADER` | 30 | `{"X-HTTP-Method-Override": "PUT"}`, verbatim |
| `WHOLE_LIST_COLLECTIONS` | 35 | the six data-loss-hazard collections, named so the section layer can import rather than retype them |
| `_validate_profile_id(value, source)` | 45 | rejects non-string and blank; used by BOTH the read path and the caller-supplied path |
| `_get_profile_id()` | 65 | `PROFILE_API` with `expand_level=4`, returns `profile[0]["profileId"]`, raises `ValueError` on a missing list, a non-dict row, a missing key, or an unusable value |
| `fullprofiles_write(section_body, profile_id=None)` | 100 | the chokepoint |

The docstring on `fullprofiles_write` states the DELTA/merge semantics explicitly
(omitted key is not a deletion; clearing a scalar is an explicit `""`; clearing tracked
fields is an explicit `isDelete` array; row deletion is a different route) and carries
the WHOLE-LIST warning naming all six collections and saying in terms that the merge is
the SECTION layer's job, deliberately not done here.

**One additive guard beyond the six specified controls, flagged for the lead:**
a caller-supplied `profile_id` is run through the same `_validate_profile_id` as the
read path (`profile_write.py:154`). The brief only required validating the value coming
back from the read. I added the caller-side check because it is the same invariant at
the same chokepoint and cannot affect any specified behaviour -- a valid 64-character
string passes through untouched. Say the word and it comes out in one line.

Two deliberate non-choices, both to stay inside the brief:
* `_get_profile_id` does NOT accept the dict-shaped `profile` fallback that
  `resume_photo.py:40-44` tolerates. The brief specifies `profile[0]`; an unexpected
  shape raises with the shape named rather than being silently coerced.
* The module is not registered in `naukri_server/__init__.py`. It defines no
  `@mcp.tool()`, so there is nothing to register.

---

## 4. The six controls, each SHOWN RED before it was allowed to count

Method: one mutation at a time was applied to the source, only the test that control
owns was run, the one-line traceback was captured, and the file was restored from an
in-memory snapshot in a `finally` block with a post-restore byte-equality assertion.
The tree was verified clean afterwards (`git diff` shows only the six intended changes).

| # | Control (test) | Mutation applied | Observed RED |
|---|---|---|---|
| 1 | URL is the v1 path (`test_write_goes_to_the_v1_fullprofiles_route`) | `FULLPROFILES_WRITE_API` repointed to the v0 path in `config.py` | `AssertionError: the profile editor writes to the v1 fullprofiles route (bundle mnj_v320.min.js offset 44671). Got: '/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/fullprofiles'` (test file :69) |
| 2 | Override header present (`test_write_carries_the_method_override_put_header`) | `extra_headers=dict(METHOD_OVERRIDE_HEADER)` dropped from the `api_client.post` call | `AssertionError: no extra_headers reached the transport -- the override header is mandatory on every fullprofiles write (21/21 bundle call sites)` / `assert None is not None` (test file :88) |
| 3 | `profileId` at TOP level (`test_profile_id_is_top_level_and_not_nested_in_the_section`) | body build changed to nest the id inside each dict-valued section instead of placing it at the top level | `KeyError: 'profileId'` (test file :109) |
| 4 | Body-borne `profileId` rejected (`test_section_body_carrying_profile_id_is_rejected`) | guard `if "profileId" in section_body:` neutered to `if False:` | `Failed: DID NOT RAISE <class 'ValueError'>` (test file :130) |
| 5 | Empty `section_body` rejected (`test_empty_section_body_is_rejected`) | guard `if not section_body:` neutered to `if False:` | `Failed: DID NOT RAISE <class 'ValueError'>` (test file :146) |
| 6 | Unusable `profileId` from the read raises rather than sends (`test_unusable_profile_id_from_the_read_raises_before_sending`, 3 params) | the missing-key guard and the `_validate_profile_id` call in `_get_profile_id` replaced with `return first.get("profileId", "")` | `Failed: DID NOT RAISE <class 'ValueError'>` **x3** -- `[profile_row0-profileId key absent]`, `[profile_row1-profileId empty string]`, `[profile_row2-profileId non-string]` (test file :172) |

All six restored to GREEN afterwards; the whole file is 10 passed.

Supporting cases in the same file (not among the six, but load-bearing):
* `test_profile_read_with_no_profile_list_raises` -- a read with no `profile` key aborts.
* `test_get_profile_id_returns_the_id_when_the_read_is_healthy` -- the CONTROL for #6.
  Without it, an implementation that raised unconditionally would pass all three
  parametrized cases. It also pins `params={"expand_level": "4"}`, since the profile
  endpoint is a hard HTTP 400 without it.
* Control #1 retypes the v1 URL as a literal in the test file rather than importing
  `FULLPROFILES_WRITE_API`. Importing the constant would make the test agree with any
  future value of it -- including a silent repoint back to v0, which is the one failure
  the control exists to catch.
* Control #3 also asserts the caller's `section_body` dict is not mutated on the way
  through.

---

## 5. Suite numbers

| run | result |
|---|---|
| BASELINE, before any edit | **2979 passed, 8 deselected** in 283.81s -- matches the brief exactly |
| AFTER all edits | **3058 passed, 8 deselected** in 326.16s |

Zero failures, zero errors, zero regressions.

The +79 delta reconciles exactly, and only 11 of it is mine:

| source | count |
|---|---|
| `tests/test_profile_write_transport.py` (this slice) | +10 |
| `tests/test_no_decoys.py::TestEveryConfigConstantHasAReader::test_constant_is_read_somewhere_in_the_package[FULLPROFILES_WRITE_API]` | +1 |
| `tests/test_profile_snapshot.py` (**a sibling agent's file, not this slice**) | +68 |
| **total** | **+79** |

The +1 is auto-generated, not written: `test_no_decoys.py:67` parametrizes over every
constant defined in `config.py`, so adding `FULLPROFILES_WRITE_API` creates a case
demanding that something in the package reads it. It PASSES because
`profile_write.py:26` imports it. Verified individually: `1 passed in 3.40s`.

Counts confirmed by collection, not inferred: `pytest --collect-only -q` reports 3058
for the tree; ignoring both new test files reports 2980 (= baseline 2979 + the
auto-generated case); the two new files collect 10 and 68 respectively.

---

## 6. Two observations for the lead

1. **Another implementer is writing in this same tree, concurrently.** During this
   slice, `naukri_server/tools/profile_snapshot.py` (27672 bytes),
   `tests/test_profile_snapshot.py` (68 tests) and a `.gitignore` edge appeared and
   kept changing (`profile_snapshot.py` mtime moved 09:01 -> 09:09 while I worked).
   The `.gitignore` change adding `profile_snapshots/` is **not mine** -- I touched
   none of those three files. It is flagged only so the commit attribution is right;
   both new production modules coexist cleanly and the combined suite is green.
2. **`profile_update.py:608` and its 405 are untouched by this slice.** The comment
   there ("The fullprofiles endpoint rejects all external API calls (405 from CDN)")
   was written against the v0 path with a literal method. Whether POST + the override
   header against v1 clears that 405 is section 8 item 2 of the contract document --
   unresolvable without an actual write, and correctly out of scope here. The browser-UI
   path in `profile_update.py` is left exactly as it was; nothing routes through the new
   transport yet.
