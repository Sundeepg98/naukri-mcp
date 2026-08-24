# The READ/WRITE row-id mapping, pinned with controls shown failing first

Slice date 2026-08-24. Scope: ONE new file,
`tests/test_profile_section_id_mapping.py` (33 tests). Nothing else in the repo
was edited. `naukri_server/tools/profile_sections.py` was read, and was broken
and restored six times to produce the RED column below; it is byte-identical to
where it started.

**ZERO network calls and ZERO writes against naukri.com were made by this
slice.** Every test drives fixtures through three mocked seams -- the profile
READ (`api_client` in the module namespace), the transport
(`fullprofiles_write`), and the event-bus emit -- exactly as
`tests/test_profile_sections.py` does. The snapshot layer runs for real against
a `tmp_path` directory.

**No personal data.** Every id in the fixtures is synthetic: the profile anchor
is 64 literal zeros, and the online-profile rows are `OP1` .. `OP5`. The live
`profileId` (64 characters) and the live online-profile row id (48 characters)
appear nowhere in the test file or in this document. In the RED messages below,
the 64-zero placeholder is abbreviated `<zeros64>` for readability; the test
output printed the zeros literally.

---

## What is being pinned

The profile READ and the profile WRITE do not spell whole-list row ids the same
way.

* `_audit/_slices/naukri-profile-read-shape.md` -- the live read returns
  section `onlineProfile` (singular) whose row id field is plainly **`id`**, a
  48-character string. It is the only section whose row id is named `id`.
* `_audit/_slices/naukri-profile-write-contract.md` -- the editor's builder at
  bundle offset 640662 is
  `er=function(a,b){return a.forEach(function(a){a.onlineProfileId=a.id,`
  `delete a.profileId, delete a.id}), {onlineProfiles:a, profileId:b}}`.
  The same `row.id -> <section>Id` rename is documented for `workSamples`
  (656875), `presentations` (668250), `publications` (681299) and `patents`
  (696760).

Without the mapping, `_merge_whole_list` matched the caller's
`onlineProfileId` against live rows carrying only `id`, so no row ever matched
and updating any of those five collections was impossible -- every attempt
raised "No row in ... carries ...".

`SectionSpec.read_id_field` (defaulting to `id_field`) plus
`_to_write_row(read_row, spec)` close the gap, and are confined: when
`read_id_field == id_field` the row is returned unchanged, so `languages`
passes through untouched.

---

## The eight controls

All eight were observed FAILING before being accepted. One mutation at a time;
after each run the source was restored from the original bytes in a `finally`
block and re-hashed.

| # | Control (class in `tests/test_profile_section_id_mapping.py`) | Mutation that produced RED | Result |
|---|---|---|---|
| 1 | `TestTheSpecTableDeclaresBothSpellings` -- all five id-mapped collections declare `read_id_field == "id"`, `languages` declares `"languageId"`, table-driven | M1: drop `read_id_field="id"` from the `patents` spec | 1 failed, 7 passed |
| 2 | `TestToWriteRowIsIdentityForLanguages` -- `_to_write_row` is identity for `languages`, `profileId` included | M2: delete the `read_id_field == id_field` early return, making the strip unconditional | 2 failed, 1 passed |
| 3 | `TestToWriteRowMapsAnOnlineProfileRow` -- sets `onlineProfileId` from `id`, removes `id` and `profileId` | M3: `_to_write_row` returns the row unchanged, always | 4 failed |
| 4 | `TestAnUpdateMatchesARowThatCarriesOnlyId` -- an update by `onlineProfileId` matches a live row carrying only `id`; merged in place, row count unchanged | M3 | 3 failed |
| 5 | `TestTheOtherRowsSurviveAndAreMapped` -- 4 live rows in, 4 on the wire, the 3 untouched ones carry their mapped ids | M5: map only the MATCHED row, leave the others in the read spelling | 3 failed, 1 passed |
| 6 | `TestACreateAppendsAndLeavesTheRestMapped` -- a create appends and leaves the existing rows intact and mapped | M3 | 1 failed, 2 passed |
| 7 | `TestRowsLostUsesTheReadSpelling` -- **the most important control**; a row vanishing from `onlineProfile` between the two reads IS detected | M7: `rows_lost(before, after, spec.envelope, spec.id_field)` | 2 failed, 2 passed |
| 8 | `TestTheRefusalNamesTheCollectionAndTheReadSpelling` -- the refusal names the collection AND the read spelling | M8: drop the "(The read payload spells this id %r ...)" sentence | 1 failed, 3 passed |

### Verbatim RED messages

**Control 1** -- `test_the_declared_spellings_match_the_measured_payloads[patents-patentId-id]`

```
AssertionError: section 'patents' is READ with its row id spelled 'id'; the
spec says 'patentId'. Get this wrong and no live row matches a caller's id, so
the collection cannot be updated at all.
assert 'patentId' == 'id'
```

**Control 2** -- `test_a_language_row_passes_through_byte_identical`, then
`test_a_language_row_keeps_its_profile_id`

```
AssertionError: a languages row was transformed on its way to the wire. It
reads and writes `languageId` alike and the bundle documents no strip for it,
so it must pass through untouched.
  Read: {"ability":["Read","Write","Speak"],"entityLanguageId":"1","lang":"Lang-One","languageId":"LANG1","proficiency":{"id":1,"value":"Expert"},"profileId":"<zeros64>"}
  Sent: {"ability":["Read","Write","Speak"],"entityLanguageId":"1","lang":"Lang-One","languageId":"LANG1","proficiency":{"id":1,"value":"Expert"}}
```

```
AssertionError: `profileId` was stripped off a languages row. Only the five
collections whose read and write spellings differ are stripped (bundle offsets
640662, 656875, 668250, 681299, 696760); generalising it invents a contract the
evidence does not carry.
```

**Control 3** -- `test_the_write_id_is_taken_from_the_read_id` (first of four)

```
AssertionError: the row went to the wire without `onlineProfileId` set from the
read row's `id`: {"description":"desc-OP1","id":"OP1","profile":"Portfolio","profileId":"<zeros64>","url":"url-one"}
assert None == 'OP1'
```

**Control 4** -- `test_the_update_reaches_the_transport_at_all`

```
AssertionError: the update never reached the transport. The caller's
`onlineProfileId` matched no live row, because the live rows carry `id` --
which is the defect the read/write id mapping exists to remove.
assert 0 == 1
```

This is the pre-fix behaviour reproduced exactly: the merge refused, and nothing
was sent.

**Control 5** -- `test_the_untouched_rows_are_exactly_the_bundles_transform`,
then `test_no_untouched_row_still_carries_the_read_only_keys`

```
AssertionError: untouched row OP1 is not on the wire under a WRITE id at all.
Ids sent: ['None', 'OP2']. A row that kept its READ spelling is a row the server
cannot match.
```

```
AssertionError: a row went to the wire still carrying keys the editor deletes
off every row: {"description":"desc-OP1","id":"OP1","profile":"Portfolio","profileId":"<zeros64>","url":"url-one"}
```

Worth noting: under M5 the row-COUNT test still passed (4 rows in, 4 rows on the
wire). The partial mapping preserves the count and breaks only the spelling,
which is what makes the "untouched rows carry their mapped ids" half of this
control load-bearing on its own rather than a restatement of the count.

**Control 6** -- `test_the_four_existing_rows_are_still_mapped`

```
AssertionError: existing row OP1 is not on the wire under a WRITE id at all.
Ids sent: ['None']. Appending must not skip the rename over the rows it keeps.
```

**Control 7** -- `test_a_row_that_vanished_between_the_two_reads_is_named`, then
`test_the_spelling_handed_to_rows_lost_is_the_read_one`

```
AssertionError: a row present in the before-read and gone from the after-read
was reported as []. Handed the write spelling, rows_lost matches no row at all,
and an empty list is indistinguishable from 'nothing was lost' -- the silent
wipe this layer exists to catch.
assert [] == ['OP4']
```

```
AssertionError: rows_lost was called as [('onlineProfiles', 'onlineProfileId')].
It compares two READ payloads, so it needs the READ spelling of the row id; the
write spelling matches nothing and silently reports no losses.
```

Measured directly against the same fixtures:
`rows_lost(before, after, "onlineProfiles", "id")` returns `['OP4']`;
`rows_lost(before, after, "onlineProfiles", "onlineProfileId")` returns `[]`.

**Control 8** -- `test_the_message_names_the_read_spelling_of_the_id`

```
AssertionError: the refusal names the WRITE spelling only. The read payload
spells this row id `id`, and a caller comparing the message against a profile
read has no way to connect the two: "No row in 'onlineProfiles' carries
onlineProfileId == 'OP9'. Pass the id of a row that exists, or omit it to create
a new one."
assert "'id'" in "No row in 'onlineProfiles' carries onlineProfileId == 'OP9'. Pass the id of a row that exists, or omit it to create a new one."
```

---

## SHA-256 of `naukri_server/tools/profile_sections.py`

| when | sha256 |
|---|---|
| before the slice | `fce4c7447106fd894d04f4cb53994f240c5c1a44461f03545c7183ffbb1b0ea0` |
| after the slice | `fce4c7447106fd894d04f4cb53994f240c5c1a44461f03545c7183ffbb1b0ea0` |

Identical. The hash was also re-checked after each of the six mutation runs and
matched every time; the mutation harness read and wrote the file in BINARY mode
throughout, so line endings could not drift. The new test file is 0 non-ASCII
bytes, 0 CR bytes.

## Suite

`venv\Scripts\python.exe -m pytest tests/ -q`

| run | result |
|---|---|
| baseline, before this slice | 3214 passed, 8 deselected, 0 failed |
| after this slice | **3247 passed, 8 deselected, 0 failed** |

33 added, which is the whole delta: no pre-existing test changed state.

---

## Defect found, NOT fixed

**An UPDATE to any of the five id-mapped whole-list collections returns
`status: "error", error_code: "NOT_PERSISTED"` even when it lands perfectly.**

VERIFIED BY INSTRUMENT, against fixtures, before any test was written.
`_build_body` returns `expected = dict(fields)` -- the caller's fields in the
WRITE spelling, including `onlineProfileId`. `verify_write` then checks
containment of `expected` against the AFTER READ, which spells that row id
`id`. No read row can ever contain `onlineProfileId`, so `verified` is always
False for these five collections.

Reproduction (4 live rows, update `OP2`'s url, after-read reflects the change
and loses nothing):

```
SENT: {"onlineProfiles":[{...,"onlineProfileId":"OP1",...},
                         {...,"onlineProfileId":"OP2","url":"u-new"},
                         {...,"onlineProfileId":"OP3",...},
                         {...,"onlineProfileId":"OP4",...}]}
STATUS: error | error_code: NOT_PERSISTED
VERIFIED: False
REASON: checked 2 expected key(s) ['onlineProfileId', 'url'] against section
        'onlineProfile' of the post-write read: NOT all present. Diff over that
        section: 0 added, 0 removed, 1 changed, 19 unchanged. 0 change(s) not
        accounted for by the expected write (collateral).
ROWS_LOST: []
```

The body on the wire is exactly right and the diff is clean (0 collateral, 0
rows lost); only the containment check fails, and only on the id key. The caller
is told "The write was accepted but the re-read does not show it. Nothing here
confirms the change landed" after a write that did land.

Scope of the damage: `onlineProfiles`, `workSamples`, `presentations`,
`publications`, `patents` -- updates only. Creates are unaffected (no id in
`fields`). `languages` and every SINGLE_ROW / SCALAR_BLOCK section are
unaffected. Non-id fields are unaffected; it is the id key alone that cannot be
found.

Not fixed here, per the slice brief. The obvious shape of a fix is to translate
`expected`'s id key to `spec.read_id_field` before handing it to `verify_write`
-- the mirror of what `rows_lost` already gets right -- but that is a change to
`profile_sections.py` and belongs to whoever owns that file.

**None of the eight controls above depends on this defect either way.** Controls
4, 5 and 6 assert on the SENT BODY, which is what the mapping fixes. Control 7
asserts on `result["rows_lost"]` (populated regardless of `verified`) and on
`result["status"] == "error"`, which stays true whether the error is the current
`NOT_PERSISTED` or the `COLLATERAL_CHANGE` a fix would produce -- a lost row is
an error under either. So a fix for this defect will not break this file.

---

## Observation: a tension inside the write-contract slice

`_audit/_slices/naukri-profile-write-contract.md` says two things about
`languages` that do not sit comfortably together:

* Section 5, the create/update table, distinguishes it explicitly: the five
  mapped collections read "`<x>Id` set from `row.id`", while `languages` reads
  "`languageId`, `entityLanguageId` -- both carried per row". No rename, no
  strip.
* Section 6.1, after quoting `er` verbatim (rename plus
  `delete a.profileId, delete a.id`), says: "Same pattern at 656875, 668250,
  681299, 696760, and inside `at` at 790786 for `languages`."

Read strictly, 6.1 would put `languages` under the same strip. Section 6.1 is
titled and framed around the FULL-LIST hazard, so "same pattern" most likely
refers to sending every row every time, which is the section's subject. The read
capture settles the rename half either way: `languages` rows carry six keys and
`id` is not among them, so there is nothing to rename from. What remains open is
narrower -- whether the editor also deletes `profileId` off each language row.

The shipped behaviour is the confined one (languages passes through with
`profileId` intact) and control 2 pins it. If a future reader re-reads offset
790786 and finds a `delete a.profileId` there, control 2 is exactly the test
that will have to change, and it names the evidence it rests on. Flagging rather
than resolving: settling it needs a bundle re-read, which is outside this slice.
