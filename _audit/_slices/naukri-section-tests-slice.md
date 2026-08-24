# Section-write orchestrator test suite -- RED table and integrity record

Slice: test suite for `naukri_server/tools/profile_sections.py`.
File written: `tests/test_profile_sections.py` (new, 42.0 KB, 62 test cases).
Date: 2026-08-24. Branch: master. No commit made by this slice.

ZERO network calls and ZERO writes against naukri.com were issued at any point.
Three seams are mocked and nothing else: the profile READ (`api_client` in the
module namespace), the TRANSPORT (`fullprofiles_write`), and the event bus
emit. The snapshot layer runs for real against a `tmp_path` directory, because
"a snapshot was taken before the write" is part of the behaviour under test.
The live MCP server (PID 43228) was not touched, restarted or killed.

No personal data appears in the test file: synthetic section rows only, and the
profile id is the literal `"0" * 64` stand-in.

---

## 1. Integrity of the module under test

`profile_sections.py` was mutated 17 times, one at a time, and restored from
the original bytes after every run. Restoration is binary (`Path.write_bytes`),
never `write_text`, so LF endings survive on Windows. The hash was re-asserted
after each restore, not only at the end.

| | SHA-256 of `naukri_server/tools/profile_sections.py` |
|---|---|
| BEFORE (measured before any mutation) | `34bdc5799b789d65d7dfdd0d475b86a23b0a7c8072fdf391b3b79ecc24de5f9d` |
| AFTER (measured after the 17th restore) | `34bdc5799b789d65d7dfdd0d475b86a23b0a7c8072fdf391b3b79ecc24de5f9d` |
| Identical | YES |
| CR bytes in file | 0 (LF-only, unchanged) |

The module under test was READ, mutated transiently, and restored. It carries
no edit from this slice. The two defects found in it (section 3) were NOT
fixed here.

---

## 2. The RED table -- all 17 controls shown failing first

Each row: the deliberate break, the single test node run against it, and the
verbatim first `E`-line pytest printed. Every control went RED. A control that
had stayed green under its own mutation would certify nothing and is recorded
as such; there are none.

| # | Control | Mutation applied | Observed RED message |
|---|---|---|---|
| 1 | Preview writes nothing | preview fires the transport before returning | `AssertionError: confirm=False wrote to the transport 1 time(s). A preview that writes is worse than no preview. | assert 1 == 0` |
| 2 | Preview body == confirmed body | preview reports the unmerged row instead of the real body | `AssertionError: the previewed body and the sent body differ for 'employments'. Preview: {"employments":{"designation":"Staff Engineer","employmentId":"E2"}} Sent: {"employments":[{"designation":"Staff Engineer","employmentId":"E2"}]}` |
| 3 | WHOLE_LIST merge preserves every other row | `merged = [dict(fields)]` -- send only the caller's row | `AssertionError: a whole-list write put 1 row(s) on the wire; the live list has 5. Naukri replaces the whole collection with what it receives, so this drops 4 row(s). | assert 1 == 5` |
| 4 | WHOLE_LIST create appends | `return [dict(row)]` instead of `rows + [dict(row)]` | `AssertionError: creating a whole-list row sent 1 row(s); 5 live + 1 new = 6 | assert 1 == 6` |
| 5 | WHOLE_LIST unmatched id is refused | the `raise ValueError` replaced with a silent append | `AssertionError: an id matching no live row returned 'preview'; silently appending orphans the edit the caller meant to make | assert 'preview' == 'error'` |
| 6 | SINGLE_ROW sends a one-element array | single-row branch disabled, falls through to the read-merge | `AssertionError: a single-row section put 3 row(s) on the wire. The server matches on the id and merges; the other rows are neither sent nor affected. | assert 3 == 1` |
| 7 | Create vs update routing | `is_create = False` | `AssertionError: 'employments' with fields ['employmentType', 'organization', 'startDate'] routed as 'update', expected 'create'. | assert 'update' == 'create'` |
| 8 | Required-on-create enforced only on create | the required-on-create check disabled | `AssertionError: assert 'preview' == 'error'` |
| 9 | Verified write returns `updated` | success status renamed to `ok` | `AssertionError: a write confirmed in the re-read reported 'ok' (Wrote summary and confirmed it in a fresh read. No rows lost, no collateral changes.) | assert 'ok' == 'updated'` |
| 10 | Unverified write returns NOT_PERSISTED | `if not result["verified"]` turned into `if not True` -- the verify result ignored | `AssertionError: the re-read does not contain the change, yet the write reported 'updated'. A 200 is acceptance, not persistence. | assert 'updated' == 'error'` |
| 11 | Collateral damage surfaced | the `lost or collateral` branch disabled | `AssertionError: a row vanished and the write reported 'updated' | assert 'updated' == 'error'` |
| 12 | Controlled-vocabulary guard | the vocab type check neutered | `AssertionError: a bare string in a controlled-vocabulary field returned 'preview'. The API accepts it and silently ignores it. | assert 'preview' == 'error'` |
| 13 | Scalar scope guard | the stray-field check neutered | `AssertionError: write_section('summary', {'resumeHeadline': ...}) returned 'preview'. Every scalar section shares the 'profile' envelope, so nothing else stops this from rewriting the headline. | assert 'preview' == 'error'` |
| 14 | Unknown section | the `spec is None` guard removed | `AttributeError: 'NoneType' object has no attribute 'vocab_fields'` |
| 15 | Empty / non-dict fields refused | the non-empty-dict guard removed | `AssertionError: write_section read the profile more times than the test armed (1 read(s) already served) | TypeError: 'NoneType' object is not iterable | TypeError: 'int' object is not iterable` |
| 16 | Transport exception is an error | the API_ERROR payload relabelled `"status": "updated"` | `AssertionError: the transport raised and the call reported 'updated' | assert 'updated' == 'error'` |
| 17 | SECTION_SPECS consistency (table-driven) | `id_field` dropped from the `patents` spec | `AssertionError: section 'patents' is a whole_list collection with no id_field. Without one there is no way to target an update, and a whole-list merge cannot match the caller's row against the live list.` |

RED: 17 of 17. GREEN-under-mutation: 0. Anchor misses: 0.

Harness (disposable, not harvested): a byte-level mutate / run-one-node /
restore / re-hash driver. Its per-control JSON record was kept only for this
document; the reproducible artifact is this table plus the mutation column,
which names the exact edit for each control.

---

## 3. Defects found in the module under test -- NOT fixed here

### DEFECT 1 (functional, blast radius 4 sites): the snapshot is never named

`write_section` reads `snapshot.get("name")` at four sites (lines 348, 360,
370 and the COLLATERAL_CHANGE message). `save_snapshot` in
`profile_snapshot.py` returns the filename under `"file"`; its return dict has
no `"name"` key at all:

```
status, file, path, taken_at, label, sections, section_count, bytes,
snapshot_version
```

Measured, not inferred: `has 'name' key: False`, and
`write_section snapshot.get("name") occurrences: 4`.

Consequence: every error payload reports `"snapshot": None`, and the
NOT_PERSISTED message renders literally as

> Restore from snapshot None if the section looks wrong.

The snapshot IS written to disk correctly -- only its name is lost on the way
into the payload. This defeats the recovery instruction on exactly the three
paths that exist to give the caller a way back: NOT_PERSISTED,
COLLATERAL_CHANGE and API_ERROR.

Suggested fix (one token, four sites): `snapshot.get("file")`.

Recorded in the test file as two `@pytest.mark.xfail(strict=True)` cases under
`TestDefectSnapshotIsNeverNamed`. `strict=True` is deliberate: the moment the
module is repaired these turn into hard FAILURES, which forces the markers to
be removed rather than left behind as a stale excuse. They are the only two
xfails in the file.

### DEFECT 2 (message, low severity): the merge refusal names the id field twice

`_merge_whole_list` raises

```python
"No row in %r carries %s == %r. ..." % (id_field, id_field, target)
```

Three placeholders, three arguments, but the first is `id_field` where the
collection name belongs. Reproduced verbatim:

> No row in 'languageId' carries languageId == 'L99'. Pass the id of a row
> that exists, or omit it to create a new one.

The message never says which collection was searched, which is the one thing a
caller needs when several sections are in flight. The function has no section
name in scope, so the fix is a signature change (pass `section`) rather than a
format-argument swap -- flagged for the lead rather than guessed at.

### OBSERVATION (design, not a defect): unknown-section uses a different error channel

`_spec()` RAISES `ValueError` for an unknown section, and that call sits
outside every `try` in `write_section`. Every other guard in the module RETURNS
`{"status": "error", "error_code": "VALIDATION_ERROR"}`. The docstring's
`Returns:` block documents only the dict form and no raise. An MCP tool calling
`write_section("typo", ...)` therefore gets an unhandled exception where every
other bad input gets a structured error. Control 14 pins the CURRENT behaviour
(`pytest.raises(ValueError)` plus the "lists the valid sections" assertion), so
the suite is honest about what the module does today; whether to unify the
channel is the lead's call.

---

## 4. Pre-existing suite failure -- caused by the module under test, not by this slice

The brief stated a baseline of 3058 passed, 8 deselected. The measured baseline
BEFORE this slice wrote a single line was:

```
1 failed, 3057 passed, 8 deselected in 223.32s
FAILED tests/test_read_path_purity.py::TestNoReadPathEmits::test_emit_census_matches_the_frozen_allowlist
```

Cause, reproduced in isolation:

```
AssertionError: the set of event emitters changed.
  NEW (is the enclosing function a READ? if so, gate it behind emit_events
  before adding it here):
    tools/profile_sections.py::write_section -> ProfileUpdated
  GONE (intentional? update EMIT_CENSUS): none
```

`profile_sections.py` is a new untracked module that emits `ProfileUpdated`,
and the frozen `EMIT_CENSUS` in `tests/test_read_path_purity.py` was never
updated to admit it. The emit is legitimate -- `write_section` is a WRITE, not
a read -- so the remedy is one entry added to `EMIT_CENSUS`, not a gate.

NOT APPLIED. `tests/test_read_path_purity.py` is outside this slice's single
owned file. Flagged for the wave lead.

---

## 5. Final suite numbers

| Run | Result |
|---|---|
| Brief's stated baseline | 3058 passed, 8 deselected |
| Measured baseline (before this slice) | 1 failed, 3057 passed, 8 deselected |
| New file alone (`tests/test_profile_sections.py`) | 60 passed, 2 xfailed |
| Full suite after this slice | 1 failed, 3117 passed, 8 deselected, 2 xfailed |

### 5.1 Full suite, after

```
1 failed, 3117 passed, 8 deselected, 2 xfailed in 203.09s (0:03:23)
FAILED tests/test_read_path_purity.py::TestNoReadPathEmits::test_emit_census_matches_the_frozen_allowlist
```

The delta against the measured baseline is exactly +60 passed and +2 xfailed
(3057 -> 3117), all from `tests/test_profile_sections.py`, which contributes
62 of the 62 tests it collects. Deselected is unchanged at 8.

The single failure is the pre-existing `EMIT_CENSUS` gap documented in section
4. It is byte-identical to the failure measured BEFORE this slice wrote
anything -- same node, same assertion, same named emitter
(`tools/profile_sections.py::write_section -> ProfileUpdated`) -- so it fails
with this slice's file present or absent. No other test changed state, and
this slice introduces no regression.

The two xfails are the DEFECT 1 ledger entries described in section 3, not
skipped work. They are `strict=True`, so they fail loudly the moment the
module is repaired.

---

## 6. What the suite covers

62 test cases across 17 controls, class-per-control:

- 1 `TestPreviewWritesNothing` (3) -- inert preview, nothing on disk, expand_level=4
- 2 `TestPreviewMatchesTheConfirmedBody` (3, parametrized over all three kinds)
- 3 `TestWholeListMergePreservesTheOtherRows` (5) -- the file's centre of gravity
- 4 `TestWholeListCreateAppends` (2)
- 5 `TestWholeListUnknownIdIsRefused` (2)
- 6 `TestSingleRowSendsOneRow` (2)
- 7 `TestCreateVersusUpdateRouting` (5, parametrized)
- 8 `TestRequiredOnCreate` (4)
- 9 `TestVerifiedWriteReportsUpdated` (4)
- 10 `TestUnverifiedWriteIsNotPersisted` (3)
- 11 `TestCollateralDamageIsSurfaced` (2)
- 12 `TestControlledVocabularyGuard` (3)
- 13 `TestScalarScopeGuard` (3)
- 14 `TestUnknownSection` (2)
- 15 `TestFieldsMustBeANonEmptyDict` (7, parametrized)
- 16 `TestTransportFailure` (4)
- 17 `TestSpecTableConsistency` (6)
- DEFECT LEDGER `TestDefectSnapshotIsNeverNamed` (2 strict xfails)

Three assertions beyond the brief's list, added because they are cheap and
each pins a real hazard:

- the read carries `expand_level=4` (the profile endpoint is a hard HTTP 400
  without it, so losing it turns every write into a read failure);
- `onlineProfiles` is written plural and READ singular -- if the merge cannot
  find the live list under the read spelling it starts from empty and deletes
  every other row, which is control 3's failure mode arriving by a different
  door;
- the WHOLE_LIST set in `SECTION_SPECS` is asserted equal to BOTH
  `profile_write.WHOLE_LIST_COLLECTIONS` and
  `profile_snapshot.WHOLE_LIST_SECTIONS`. Three modules name these six
  collections; a section the transport calls whole-list while the spec table
  calls it single-row is silent data loss, and nothing else in the repo
  compares the three lists.

The rig refuses to serve a profile read the test did not arm. That is why
controls 8, 14 and 15 can assert `rig.reads.calls == []` -- "refused before it
even read the profile" is a stronger claim than "did not write".
