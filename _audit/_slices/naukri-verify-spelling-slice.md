# The verify-side row-id spelling: seven controls, each shown RED first

**Slice**: pin the `_to_expected` fix in `naukri_server/tools/profile_sections.py`.
**File added**: `tests/test_profile_section_verify_spelling.py` (36 tests).
**Network**: ZERO calls and ZERO writes against naukri.com. Fixtures only; three
seams mocked (`api_client` in the module namespace, `fullprofiles_write`, the
event-bus emit). The snapshot layer runs for real against a `tmp_path` dir.
**Server processes**: untouched. No restart, no kill, no start.
**Git**: nothing committed, staged, or history-changed by this slice.

---

## 1. What was fixed, and why it was worse than a missing check

`write_section` derives TWO artefacts from one caller `fields` dict, and they do
not share a vocabulary:

| artefact | destination | spelling | authority |
|---|---|---|---|
| `section_body` | the wire | WRITE: `onlineProfileId` | `mnj_v320` @640662 `er=function(a,b){return a.forEach(function(a){a.onlineProfileId=a.id, delete a.profileId, delete a.id})...`; same shape @656875 / @668250 / @681299 / @696760 |
| `expected` | `verify_write`, compared against the AFTER READ | READ: `id` | `_audit/_slices/naukri-profile-read-shape.md` -- `onlineProfile` is the only section whose row id is named plainly `id` |

`_build_body` handed `dict(fields)` to both. For the five id-mapped whole-list
collections an UPDATE therefore asked the verifier to find `onlineProfileId` in
a payload that only ever carries `id`. Containment could not match, so
`verified` could never be True and a write that landed perfectly came back
`status: "error", error_code: "NOT_PERSISTED"`.

That is a CHECK THAT CANNOT PASS. It is the mirror of a check that cannot fail,
and it is worse for the reader: a check that cannot fail merely certifies
nothing, while this one actively teaches distrust of a write that worked -- and
the recommended response to `NOT_PERSISTED` is to restore from the snapshot,
i.e. to undo a correct write.

`_to_expected(fields, spec)` renames `spec.id_field` to `spec.read_id_field`
when they differ and is identity when they agree. `_build_body` calls it on
both the SINGLE_ROW and WHOLE_LIST paths. `rows_lost` already took the read
spelling; this was the mirror site.

## 2. SHA-256 of the file under test

| when | SHA-256 | bytes | CRLF |
|---|---|---|---|
| before the slice | `992c4762b9f4cbf80332e6341073b8653cc6186aa459b8e6d7acd5d5476285c7` | 20574 | 0 |
| after the slice | `992c4762b9f4cbf80332e6341073b8653cc6186aa459b8e6d7acd5d5476285c7` | 20574 | 0 |

Identical. Every mutation was applied from a pristine binary copy of the
original bytes and reverted from that same copy in binary mode, one at a time,
so line endings could not drift. `git diff --stat` on the file reads
`83 insertions(+), 20 deletions(-)` before and after, unchanged.

## 3. The five mutations

Each is a single edit inside the fixed code path. One was live at a time.

| id | edit | mutated SHA-256 |
|---|---|---|
| A | `_to_expected` becomes the identity function (`return dict(fields)`) -- the pre-fix behaviour | `e9f400c1e04222259e586391d1065145b1c66864c71581eb1a87d908e1d5ffba` |
| C | `_to_expected` renames UNCONDITIONALLY against a hardcoded read spelling: `expected["id"] = expected.pop(spec.id_field, None)` | `e9fd79f9f0ca901cd25cf276064a8723ec39ab9d122fa6eb416b38d5aea96963` |
| D | `_to_expected` returns ONLY the row id: `return {spec.read_id_field: fields[spec.id_field]}` -- verification that cannot fail | `d068da9f009c157dd5e27daf691c0546430a6a6f6c734f86e446348f05a3402c` |
| E | `rows_lost` handed the WRITE spelling (`spec.id_field` instead of `spec.read_id_field`) | `8c0a6ebca4651f8d1567229d8c3b11b7e5de49fadb5f38f8be002cde9aea3d80` |
| F | the READ spelling pushed into the wire body: `_merge_whole_list(current, _to_expected(dict(fields), spec), ...)` | `eaa24f807eb6683452e585933b7a3cd31f05ed5a4ba2663140639f6e141d57e3` |

## 4. The seven controls, with their verbatim RED

### Control 1 -- the headline

A clean `onlineProfiles` UPDATE whose after read genuinely contains the updated
row must report `status: "updated"`, `verified: True`.

`TestACleanUpdateIsReportedAsUpdated` -- RED under **A**:

```
E       AssertionError: the after read genuinely contains the updated row, so this write DID land -- reporting it unverified is the check-that-cannot-pass this control exists for.
E         status='error' error_code='NOT_PERSISTED' verified=False rows_lost=[]
E         reason: checked 2 expected key(s) ['description', 'onlineProfileId'] against section 'onlineProfile' of the post-write read: NOT all present. Diff over that section: 0 added, 0 removed, 1 changed, 19 unchanged. 0 change(s) not accounted for by the expected write (collateral). NOTE: 'onlineProfiles' is sent to Naukri as a WHOLE LIST -- a payload carrying one row deletes every other row in it.
E       assert False is True
```

The reason line is the defect stated in its own words: the diff shows `1
changed` and `0 collateral` -- the module could SEE the change land and still
called it unpersisted. The unit-level half of the same control:

```
E       AssertionError: verify_write compares this against the AFTER READ, which spells the row id 'id'. Got {'onlineProfileId': 'ROW2', 'description': 'desc-two-updated'}
E       assert {'description...leId': 'ROW2'} == {'description... 'id': 'ROW2'}
E         Left contains 1 more item:
E         {'onlineProfileId': 'ROW2'}
E         Right contains 1 more item:
E         {'id': 'ROW2'}
```

### Control 2 -- table-driven across all five id-mapped collections

`TestEveryIdMappedCollectionVerifies` -- RED under **A**, `5 failed, 1 passed`
(the passing one is the table-coverage guard, which does not depend on the
mutation). All five parametrisations failed; `patents` verbatim:

```
E       AssertionError: 'patents' (bundle 696760 (Wr)) writes 'patentId' and reads 'id'. The after read contains the updated row, so this must verify.
E         status='error' error_code='NOT_PERSISTED' verified=False rows_lost=[]
E         reason: checked 2 expected key(s) ['description', 'patentId'] against section 'patent' of the post-write read: NOT all present. Diff over that section: 0 added, 0 removed, 1 changed, 19 unchanged. 0 change(s) not accounted for by the expected write (collateral). NOTE: 'patents' is sent to Naukri as a WHOLE LIST -- a payload carrying one row deletes every other row in it.
E       assert ('error' == 'updated'
E         - updated
E         + error)
```

Failing ids: `onlineProfiles`, `workSamples`, `presentations`, `publications`,
`patents`. The class also carries a guard that reads the id-mapped set OFF
`SECTION_SPECS` and compares it to this file's table, so a sixth collection
cannot be added to the spec without this file noticing.

### Control 3 -- the rename is CONFINED

`_to_expected` must be identity for `languages` (read and write both spell it
`languageId`) and for every SCALAR_BLOCK section (no row id at all).

`TestTheRenameIsConfinedToSectionsThatNeedIt` -- RED under **C**, `6 failed, 6
passed`. The six that stayed green are the five id-mapped rename assertions plus
the non-empty-table guard: mutation C still renames the five correctly, which is
precisely why confinement needs its own control.

`languages`, unit:

```
E       AssertionError: languages is a whole-list section whose read and write spellings AGREE. Renaming here would break a section that worked.
E       assert {'id': 'LANG2...e': 'Expert'}} == {'languageId'...e': 'Expert'}}
E         Left contains 1 more item:
E         {'id': 'LANG2'}
E         Right contains 1 more item:
E         {'languageId': 'LANG2'}
```

`languages`, end to end -- the repair eating its neighbour:

```
E       AssertionError: languages has no spelling gap. A rename applied to it would send the verifier looking for an 'id' key its rows do not have.
E         status='error' error_code='NOT_PERSISTED' verified=False rows_lost=[]
E         reason: checked 2 expected key(s) ['id', 'proficiency'] against section 'languages' of the post-write read: NOT all present. Diff over that section: 0 added, 0 removed, 2 changed, 25 unchanged. 0 change(s) not accounted for by the expected write (collateral). NOTE: 'languages' is sent to Naukri as a WHOLE LIST -- a payload carrying one row deletes every other row in it.
```

Every SCALAR_BLOCK section (the list is derived from `SECTION_SPECS`, so it is
whatever the table says: `careerPreferences`, `keySkills`, `resumeHeadline`,
`summary`) failed the same way:

```
E       AssertionError: 'summary' is a SCALAR_BLOCK: it has no row id at all (id_field=None, read_id_field=None), so `expected` is the caller's fields verbatim.
E       assert {'id': None, ...y': 'a value'} == {'summary': 'a value'}
E         Left contains 1 more item:
E         {'id': None}
```

### Control 4 -- a CREATE is unaffected

No id field in `fields`, so `_to_expected` must not invent one.

`TestACreateIsUnaffected` -- RED under **C**, `6 failed` (all five
parametrisations plus the end-to-end create):

```
E       AssertionError: a create has no 'onlineProfileId' to rename; `expected` must be the caller's fields verbatim. Got {'profile': 'Portfolio', 'description': 'desc-new', 'url': 'url-new', 'id': None}
E       assert {'description...l': 'url-new'} == {'description...l': 'url-new'}
E         Left contains 1 more item:
E         {'id': None}
```

End to end, which is what an invented key costs a caller:

```
E       AssertionError: the after read carries the new row; the create landed.
E         status='error' error_code='NOT_PERSISTED' verified=False rows_lost=[]
E         reason: checked 4 expected key(s) ['description', 'id', 'profile', 'url'] against section 'onlineProfile' of the post-write read: NOT all present. Diff over that section: 1 added, 0 removed, 0 changed, 20 unchanged. 1 change(s) not accounted for by the expected write (collateral). NOTE: 'onlineProfiles' is sent to Naukri as a WHOLE LIST -- a payload carrying one row deletes every other row in it.
```

`1 added` and the module still says NOT_PERSISTED: `{"id": None}` matches no row
any read will ever return.

### Control 5 -- the fix must not blind the collateral detector

The control that keeps this repair honest. The intended change DOES land AND an
unrelated row disappears: the call must still refuse, and must NAME the lost
row.

`TestCollateralIsStillDetectedWhenTheWriteVerifies` -- RED under **E**:

```
E       AssertionError: the lost row must be NAMED, not merely counted -- an empty list is indistinguishable from 'nothing was lost'. rows_lost compares two READ payloads, so it takes the read spelling 'id'.
E         status='error' error_code='COLLATERAL_CHANGE' verified=True rows_lost=[]
E         reason: checked 2 expected key(s) ['description', 'id'] against section 'onlineProfile' of the post-write read: all present. Diff over that section: 0 added, 1 removed, 1 changed, 14 unchanged. 1 change(s) not accounted for by the expected write (collateral). NOTE: 'onlineProfiles' is sent to Naukri as a WHOLE LIST -- a payload carrying one row deletes every other row in it.
E       assert [] == ['ROW4']
E         Right contains one more item: 'ROW4'
```

Read the RED carefully -- it is the sharpest line in this slice.
`verified=True` and `error_code='COLLATERAL_CHANGE'` both SURVIVED the mutation.
The call still refused, and a test that only asserted the error code would have
passed. What died was the naming: `rows_lost=[]`, and an empty list is
indistinguishable from "nothing was lost". This is why the control asserts
`verified is True` FIRST: the control is only meaningful while verification
passes, because its whole claim is that a passing verification does not silence
collateral. A second test in the class pins that all four live rows still went
out on the wire, so the loss is attributed to the server rather than to the
payload this module built.

### Control 6 -- a genuine non-persistence still reports NOT_PERSISTED

`TestANonPersistedWriteStillFails` -- RED under **D**, `2 failed`:

```
E       AssertionError: the after read still shows the OLD description, so nothing landed. Reporting verified here would make the check unfailable.
E         status='updated' error_code=None verified=True rows_lost=[]
E         reason: checked 1 expected key(s) ['id'] against section 'onlineProfile' of the post-write read: all present. Diff over that section: 0 added, 0 removed, 0 changed, 20 unchanged. 0 change(s) not accounted for by the expected write (collateral). NOTE: 'onlineProfiles' is sent to Naukri as a WHOLE LIST -- a payload carrying one row deletes every other row in it.
E       assert True is False
```

`status='updated'` on a diff of `0 added, 0 removed, 0 changed`. Nothing moved
and the module reported success -- the exact failure mode this slice exists to
keep out, arrived at by an over-eager version of the same fix. The second test
in the class pins that containment is per-ROW: a change that landed on the wrong
row is not a landed write.

```
E       AssertionError: the new description landed on ROW3, not the ROW2 the caller named. `expected` pins BOTH the id and the value, so this must not verify.
E         status='error' error_code='COLLATERAL_CHANGE' verified=True rows_lost=[]
```

### Control 7 -- the wire keeps the WRITE vocabulary

`expected` moved to the read spelling; `section_body` must not have followed it.

`TestTheWireBodyStaysInTheWriteSpelling` -- RED under **F**, `6 failed`:

```
E       AssertionError: a row went out without onlineProfileId: {'description': 'desc-two-updated', 'id': 'ROW2'}. The editor renames id -> onlineProfileId before sending (bundle 640662); a row without it is a row the server cannot match.
E       assert 'onlineProfileId' in {'description': 'desc-two-updated', 'id': 'ROW2'}
```

and, table-driven, the shape of the damage:

```
E       AssertionError: 'patents' must go out keyed 'patentId', in order (bundle 696760 (Wr)). Got [{'profile': 'Portfolio', 'description': 'desc-one', 'url': 'url-row1', 'patentId': 'ROW1'}, {'profile': 'Portfolio', 'description': 'desc-two', 'url': 'url-row2', 'patentId': 'ROW2'}, {'profile': 'Portfolio', 'description': 'desc-three', 'url': 'url-row3', 'patentId': 'ROW3'}, {'profile': 'Portfolio', 'description': 'desc-four', 'url': 'url-row4', 'patentId': 'ROW4'}, {'description': 'desc-two-updated', 'id': 'ROW2'}]
E       assert ['ROW1', 'ROW... 'ROW4', None] == ['ROW1', 'ROW...ROW3', 'ROW4']
E         Left contains one more item: None
```

Worth naming, because it is not the failure one would predict: mixing the
vocabularies does not produce a rejected write, it produces a PHANTOM ROW.
`_merge_whole_list` looks for the caller's id under the WRITE spelling, finds
none (the row now says `id`), concludes "this is a create", and APPENDS. A live
row would have been duplicated on the profile rather than updated.

## 5. Control summary

| # | claim | class | mutation | verdict |
|---|---|---|---|---|
| 1 | a clean `onlineProfiles` update reports `updated` / `verified: True` | 2 tests | A | RED shown, GREEN after restore |
| 2 | the same across all five id-mapped collections | 5 params + 1 table guard | A | RED shown (5/5), GREEN after restore |
| 3 | `_to_expected` renames for the five, identity for `languages` and every SCALAR_BLOCK | 5 + 1 + 4 params + 2 | C | RED shown (6), GREEN after restore |
| 4 | a CREATE invents no id key | 5 params + 1 | C | RED shown (6), GREEN after restore |
| 5 | a landed update that lost a row is still `COLLATERAL_CHANGE` and NAMES the row | 2 tests | E | RED shown, GREEN after restore |
| 6 | a genuine non-persistence still reports `NOT_PERSISTED` | 2 tests | D | RED shown (2), GREEN after restore |
| 7 | `section_body` stays in the WRITE spelling | 1 + 5 params | F | RED shown (6), GREEN after restore |

## 6. Suite

| run | result |
|---|---|
| the new file alone | `36 passed in 2.80s` |
| full suite, `venv\Scripts\python.exe -m pytest tests/ -q` | `3283 passed, 8 deselected in 189.97s` |

Reconciliation, measured rather than assumed: `pytest --collect-only` reports
`3283/3291 tests collected (8 deselected)` with this file and
`3247/3255 tests collected (8 deselected)` with it ignored. The delta is exactly
the 36 tests added here, and 3247 is the briefed baseline unchanged -- nothing
else landed in `tests/` during this slice.

## 7. Fixture honesty and data hygiene

* No personal data anywhere. `profileId` is the stand-in `"0" * 64`; row ids are
  the tags `ROW1`..`ROW5` and `LANG1`..`LANG3`. No real 48-character
  online-profile id and no real `profileId` appears in the test file or here.
* Only `onlineProfile` was captured non-empty in the live read; `patent`,
  `presentation`, `publication` and `workSample` were empty arrays, so their row
  SHAPE is unmeasured. The five fixtures are therefore ONE synthetic shape
  reused, stated in the test module's docstring. What the table-driven controls
  exercise is the spec table plus `_to_expected` -- the id rename, which the
  bundle documents for all five -- not four guessed field lists.
* Strict ASCII, LF line endings, 27720 bytes.

## 8. Defects found

None. `_to_expected` behaves as the fix describes on every path exercised here,
and no surprise contradicted the brief. Two observations for the record, neither
a defect:

1. `_to_expected` is not reached on the SCALAR_BLOCK path -- `_build_body`
   returns `dict(fields)` directly for scalars. Control 3 therefore asserts the
   scalar identity property against the FUNCTION rather than through
   `write_section`, which is the stronger place for it: it holds even if a
   future edit routes the scalar path through `_to_expected`.
2. The failure mode of mixing the two vocabularies in the wire body is a phantom
   appended row, not a rejected write (control 7). That is a property of
   `_merge_whole_list` treating a missing write-spelling id as "create", and it
   is already the documented contract; it is recorded here because the RED is
   the only place the consequence is visible.
