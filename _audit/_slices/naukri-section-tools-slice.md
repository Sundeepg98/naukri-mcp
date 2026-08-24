# Profile-section tools and the restore path

Slice: expose the profile-section WRITE layer as MCP tools, and build the
RESTORE path on top of it. Build and unit-test only -- ZERO network calls and
ZERO writes were made against naukri.com. Everything below was measured on
this box on 2026-08-24 with `venv\Scripts\python.exe`.

Live server PID 43228 was not restarted, killed or contacted. No git
history-changing command was run.

---

## 1. Files changed

| File | State | Lines | What |
|---|---|---|---|
| `naukri_server/tools/profile.py` | MODIFIED | +130 | registration only |
| `naukri_server/tools/profile_restore.py` | NEW | 502 | the restore layer |
| `tests/test_profile_restore.py` | NEW | 537 | 38 tests, 8 controls |
| `tests/conftest.py` | MODIFIED | +42 | snapshot-dir redirect |

### `naukri_server/tools/profile.py`

| Line | What |
|---|---|
| 15 | `from naukri_server.tools.profile_sections import write_section` |
| 16-19 | `from naukri_server.tools.profile_restore import (list_profile_snapshots, restore_section)` |
| 145-151 | section header comment |
| 153-206 | `@mcp.tool() naukri_update_profile_section(section, fields, confirm=False)` -> `handle_tool_action(..., "profile.write_section")` |
| 208-223 | `@mcp.tool() naukri_list_profile_snapshots(limit=20)` -> `handle_tool_action(..., "profile.list_snapshots")` |
| 226-272 | `@mcp.tool() naukri_restore_profile_section(snapshot, section, confirm=False)` -> `handle_tool_action(..., "profile.restore_section")` |

No logic was added to the tool layer. Each tool is a one-line delegate wrapped
in `handle_tool_action` with a `profile.<verb>` action name, matching the six
tools already in the file.

The `naukri_update_profile_section` docstring states, in order: nothing is
written unless `confirm=True`; the write is verified by RE-READING the profile
and a 200 that did not persist is reported as an error and never as success; a
pre-write snapshot is always taken and its name returned; the six whole-list
collections are read-merged so the other rows survive; pass the id field to
UPDATE a row and omit it to CREATE one. All 16 writable section names are
listed with their id fields, grouped by kind.

### `naukri_server/tools/profile_restore.py`

| Line | Symbol |
|---|---|
| 79 | `_error` |
| 85 | `_spec_for` -- unknown section name -> validation error, never a raise |
| 97 | `_load` -- inherits the `load_snapshot` traversal guard |
| 130 | `_scalar_block` -- the read returns `profile` as an ARRAY OF ONE |
| 150 | `_scalar_fields` -- filtered by `SCALAR_ALLOWED_FIELDS` |
| 179 | `_classify` -- which rows moved, and whether they are addressable |
| 213 | `_row_guidance` -- names the ids, one clause per way they moved |
| 235 | `_restorability` -- the single supportability test |
| 268 | `_section_diff` -- scalar sections diff their OWNED FIELDS only |
| 293 | `preview_restore(snapshot, section)` |
| 351 | `restore_section(snapshot, section, confirm=False)` |
| 445 | `_unsupported` |
| 452 | `_delegate` -- the ONLY path to a write, and it is `write_section` |
| 482 | `list_profile_snapshots(limit=20)` |

Nothing in this module calls `fullprofiles_write`. Every write is issued as an
ordinary `write_section` call, so the confirm gate, the pre-write snapshot,
the post-write re-read and the collateral detector are inherited rather than
re-implemented.

### `tests/conftest.py`

Lines 166-206: the `snapshot_home` autouse fixture, placed directly above the
DB-isolation block and beside the `agent.POLICY_STATE_PATH` redirect it
copies. It monkeypatches `profile_snapshot._SNAPSHOT_DIR` to
`tmp_path / "profile_snapshots"` and returns the path.

---

## 2. The 8 controls, each SHOWN FAILING first

Protocol: for each control the SOURCE was broken deliberately, one mutation at
a time, the control was run, the RED message recorded, and
`profile_restore.py` restored from a pristine byte copy with the sha256
re-checked after every single mutation. Harness: `redrun.py` + `redcases.json`
in the session scratchpad -- disposable, it is a `subprocess` loop plus a hash
check, not an instrument worth registering.

Final hash after all 8 restorations, verified equal to the pre-mutation hash:
`fc189546660255fe93ab52d3478caa57cfd8b2cb4fe8bcb520c7b1dd14296738`

| # | Control | Mutation | Observed RED |
|---|---|---|---|
| 1 | preview writes nothing and returns a diff | `_section_diff(...)` -> a hardcoded empty diff | `assert {} == {'profile.sum..., 'ORIGINAL')}` -- `AssertionError` printing the whole preview payload |
| 2 | a SCALAR_BLOCK restore routes through `write_section` with `confirm=True` | `_delegate` passes `confirm=False` | `E assert False is True` at test line 201 |
| 3 | a SINGLE_ROW restore is `unsupported` and NAMES the differing ids | the `if spec.kind == SINGLE_ROW:` branch disabled | `E assert 'E1' in "'employments' cannot be restored in a single write: ..."` -- it still refused, but stopped naming the row |
| 4 | an unsupported restore never reaches the transport | the `if not restorable:` guard disabled | `E AssertionError: assert 'updated' == 'unsupported'` -- it wrote ONE of the two differing rows, the exact half-restore this control exists for |
| 5 | `confirm=False` writes nothing | `_delegate` passes `confirm=True` | `E AssertionError: assert 'updated' == 'preview'` |
| 6 | a path-traversing snapshot name is refused | `_load` reads the path directly instead of via `load_snapshot` | `E AssertionError: assert 'NOT_FOUND' == 'VALIDATION_ERROR'` on `[../secrets.json]` |
| 7 | a section absent from the snapshot is a clear error, not a crash | the `if not snap_found:` guard disabled | `E AssertionError: assert 'unsupported' == 'error'` |
| 8 | the snapshot listing carries metadata only | the listing also returns the loaded snapshot bodies | `E AssertionError: 'CANARY-SUMMARY-DO-NOT-LEAK' reached a snapshot LISTING ...` |

Control 4 is the load-bearing one and is asserted against a TRANSPORT CALL
COUNT (`fake.writes == []`), never against a status string -- a broken refusal
would still return the string.

Two CONTROL-on-the-control tests keep the refusals honest:
`test_the_one_row_whole_list_case_does_write` (a path that refused everything
would satisfy every other assertion in that class) and
`test_CONTROL_a_legitimate_bare_name_is_accepted`.

---

## 3. The scope limit, stated rather than papered over

`write_section` sends ONE ROW PER CALL for a row collection. That decides what
this path can honestly do.

* **SCALAR_BLOCK** (summary, resumeHeadline, keySkills, careerPreferences) --
  restored fully in one call. Implemented.
* **WHOLE_LIST** (languages, onlineProfiles, workSamples, presentations,
  publications, patents) -- restored fully in one call WHEN the live list and
  the snapshot differ in exactly one row that exists on both sides and carries
  the write spec id field. That is the shape of undoing the write the snapshot
  was taken for. Implemented.
* **SINGLE_ROW** (employments, educations, schools, itskills, projects,
  certifications) -- `status: "unsupported"`, always. The reply names exactly
  which row ids changed, which are only in the snapshot, and which are only in
  the live profile, and tells the caller to restore them one at a time via
  `naukri_update_profile_section` with the section id field. Nothing is
  written, and that is asserted by transport-call count.
* A row DELETED on naukri.com CANNOT be recreated with its original id.
  Re-sending it creates a NEW row with a NEW id. Stated in the module
  docstring, in the tool docstring, and in the `unsupported` message itself.

### DEVIATION FROM THE BRIEF -- please read

The brief says "WHOLE_LIST sections restore fully in one call, because the
whole list is the payload -- implement that", unconditionally. Measured, that
is not achievable through `write_section` as it stands:

* `write_section(section, fields, ...)` requires `fields` to be a non-empty
  DICT -- one row. A list is refused with `VALIDATION_ERROR`
  (`profile_sections.py:284`).
* `_build_body` for a whole-list section calls
  `_merge_whole_list(current, fields, id_field)` where `current` is the LIVE
  list (`profile_sections.py:255-262`). The payload is therefore "the live
  list with ONE row replaced", never "the snapshot list".
* So a single call can neither drop a row that appeared since the snapshot nor
  re-add one that was deleted, and two changed rows would need two calls.

The single-differing-row case IS provably a full restore of the list (the
payload is the whole list), so it is implemented; everything wider is refused
with the ids named. If the intent was for `write_section` to grow a whole-list
form that accepts the entire list, that is a change to `profile_sections.py`,
which this slice does not own.

### Second measured limit: onlineProfiles is not addressable

`SECTION_SPECS["onlineProfiles"].id_field` is `onlineProfileId`, but the
profile READ keys those rows by `id` (`naukri-profile-read-shape.md`, row-id
summary table). `_merge_whole_list` matches on the spec id field against LIVE
rows, so it would raise "No row ... carries onlineProfileId". This path
detects that and reports `unsupported` naming both spellings, rather than
inventing a mapping between them -- a wrong guess would append a duplicate
row. `languages` is unaffected: `languageId` is the same on both sides.

---

## 4. DEFECT FOUND IN A FILE THIS SLICE DOES NOT OWN

`naukri_server/tools/profile_sections.py`, lines **347, 360, 370, 381**, reads
the snapshot name as `snapshot.get("name")`. `save_snapshot` returns the
filename under **`file`** (`profile_snapshot.py:208`); there is no `name` key.
Consequences, all measured:

* a successful `write_section` reports `"snapshot": None`, so the caller
  cannot find the snapshot it just took;
* the `NOT_PERSISTED` message reads "Restore from snapshot None." -- at
  exactly the moment a restore is needed;
* the `COLLATERAL_CHANGE` message says the same.

One-word fix (`"name"` -> `"file"`), but it belongs to the section-write
owner. `tests/test_profile_restore.py:207-231` asserts the pre-write snapshot
exists ON DISK instead of trusting that field, and says why in its docstring.

---

## 5. The conftest change

`tests/test_profile_snapshot.py` redirected the snapshot directory with an
autouse fixture LOCAL to that module. Now that the write paths call
`save_snapshot`, any test touching a write would write the FULL live profile
read into `DATA_DIR/profile_snapshots` -- and `DATA_DIR` is the repo root. The
redirect now lives in `tests/conftest.py:166-206`, autouse for every test in
the session, beside the `agent.POLICY_STATE_PATH` redirect that exists for the
same reason.

**Scope note.** The fixture is FUNCTION-scoped autouse, not
`scope="session"`. Two measured reasons: `monkeypatch` and `tmp_path` are
function-scoped, and `test_profile_snapshot.py` COUNTS files in that directory
(`test_listing_orders_newest_first` asserts exact list equality,
`test_prune_keeps_exactly_the_n_newest` counts) -- one directory shared across
the session would make those order-dependent. "Autouse for the session" is
implemented as "autouse for every test in the session", which is also exactly
how the neighbouring `POLICY_STATE_PATH` redirect works.

`tests/test_profile_snapshot.py` was NOT edited. Its local fixture has the
same name and therefore SHADOWS the conftest one for that module, making it
redundant rather than conflicting.

### Evidence

| Check | Result |
|---|---|
| `tests/test_no_tree_writes.py` | **15 passed** (83 passed when run with `test_profile_snapshot.py`) |
| `tests/test_profile_snapshot.py` unchanged | 68 passed |
| `tests/test_profile_snapshot.py` with its local fixture DELETED (transient probe; file restored byte-for-byte, sha256 re-verified) | **68 passed** -- the conftest fixture covers it alone |
| `profile_snapshots/` present in the checkout after the full suite | ABSENT |
| `git status --short` | only the 4 files of this slice, plus files owned by other agents |

Line endings: `profile.py`, `profile_restore.py` and `test_profile_restore.py`
are LF (0x0D count = 0). `tests/conftest.py` was found CRLF (280 CRLF pairs --
the only such file among those inspected) and was written back as LF.
`core.autocrlf=true` is set repo-locally and the blobs are LF, so
`git diff --stat tests/conftest.py` reports exactly
`1 file changed, 42 insertions(+)`: the normalisation adds nothing to the diff.

---

## 6. Measured numbers

### Tool count: 125 -> **128**, exactly +3

Measured by a fresh import of the registry, before and after the change:

```
python -c "from naukri_server import mcp; print(len(mcp._tool_manager._tools))"
before: 125     after: 128
```

All three names present in `mcp._tool_manager._tools`:
`naukri_update_profile_section`, `naukri_list_profile_snapshots`,
`naukri_restore_profile_section`.

### Suite: `venv\Scripts\python.exe -m pytest -q`

```
1 failed, 3155 passed, 8 deselected, 2 xfailed in 165.17s
```

Reconciliation against the stated baseline of 3058 passed / 8 deselected:

| Delta | Source |
|---|---|
| +38 passed | `tests/test_profile_restore.py` (this slice) |
| +60 passed, +2 xfailed | `tests/test_profile_sections.py` (sibling agent, 62 collected) |
| -1 passed, +1 failed | `test_read_path_purity.py::TestNoReadPathEmits::test_emit_census_matches_the_frozen_allowlist` |

3058 - 1 + 38 + 60 = **3155 passed**, plus 1 failed and 2 xfailed. Exact.

### The one failure is not from this slice -- proven, not asserted

```
E   AssertionError: the set of event emitters changed.
E       NEW: tools/profile_sections.py::write_section -> ProfileUpdated
```

`emit_sites()` is a STATIC scan of the package directory. Probe: the package
was copied to a temp tree, this slice was removed from the copy entirely
(`profile_restore.py` deleted, the `profile.py` tool block and its two imports
reverted), and the census re-run:

```
WITHOUT this slice, added vs census: [('tools/profile_sections.py', 'write_section', 'ProfileUpdated')]
ALSO without profile_sections.py:   []
```

The failure is caused solely by `profile_sections.py` existing on disk, which
predates this slice. The fix is one entry in `EMIT_CENSUS` in
`tests/test_read_path_purity.py`; `write_section` is not read-shaped, so
`test_no_read_shaped_function_emits_unguarded` is unaffected. Both files
belong to other agents.

---

## 7. What was NOT done

* No network call and no write of any kind against naukri.com. The transport
  is replaced wholesale in every test
  (`monkeypatch.setattr(psec, "fullprofiles_write", ...)`), and the profile
  READ with it.
* `profile_sections.py`, `profile_write.py`, `profile_snapshot.py`,
  `config.py`, `api.py`, `interfaces.py` and `tests/test_profile_sections.py`
  were read but never modified.
* `tests/test_profile_snapshot.py` and `tests/test_read_path_purity.py` were
  not modified. The two transient probes above restored their files
  byte-for-byte with the hash re-verified.
* No `git add`, `git commit`, `git stash` or any history-changing command.
* `preview_restore` has no production caller: the brief specifies exactly
  three tools, and `naukri_restore_profile_section(confirm=False)` is the
  preview surface a tool caller uses. `preview_restore` is a module-level API
  and is exercised by four tests.
