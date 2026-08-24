# Profile-write safety rails: snapshot, diff, verify

Slice deliverable. Built 2026-08-24 against HEAD `c3550d3`, branch master.

**ZERO network calls were made by this slice and ZERO writes were issued against
naukri.com.** The layer is transport-free by construction: it imports nothing
from `api.py`, `browser.py`, `interfaces.py` or `profile_write.py`, opens no
socket and no page. Every test runs on synthetic fixture dicts. No personal data
appears in the module, the tests, or this document.

---

## 1. Files

| File | State | What it is |
|---|---|---|
| `naukri_server/tools/profile_snapshot.py` | NEW, 686 lines | The rails. Snapshots, structural diff, verify-after-write, `rows_lost`. |
| `tests/test_profile_snapshot.py` | NEW, 560 lines | 68 tests. All 8 required controls plus their opposite-direction controls. |
| `.gitignore` | +6 lines at end | Adds `profile_snapshots/`. See section 2. |

Both new files are **strict ASCII** (verified by byte scan: zero bytes > 127).
Nothing else in the tree was modified. The four files reserved for the
concurrent slice -- `naukri_server/tools/profile_write.py`, `config.py`,
`interfaces.py`, `api.py` -- were READ but never written. No git history command
was run: no `add`, no `commit`, no branch operation. The live MCP server
(PID 43228) was not touched.

---

## 2. The snapshot directory, and the config constant that decided it

```
snapshot_dir()  ->  DATA_DIR / "profile_snapshots"
```

**The deciding constant is `naukri_server/config.py:54`:**

```python
DATA_DIR = Path(os.environ.get("NAUKRI_DATA_DIR", str(_PACKAGE_ROOT)))
```

`_PACKAGE_ROOT` is the repo root, so `DATA_DIR` IS the checkout unless
`NAUKRI_DATA_DIR` overrides it. That is the exact fact `tests/test_no_tree_writes.py`
was written about, and it asserts it directly (`assert DATA_DIR == REPO`).

**The precedent followed is `config.py:62`, `EXPORTS_DIR = DATA_DIR / "exports"`** --
the repo's one existing convention for a runtime-state DIRECTORY (as opposed to
the eleven `DATA_DIR / "*.json"` state FILES). `exports/` is gitignored;
`profile_snapshots/` now is too, for a stronger reason: an export is a report,
whereas a snapshot is the FULL live profile read.

Three consequences, each deliberate:

* **The constant lives in the module, not in config.py.** It is
  `_SNAPSHOT_DIR`, underscore-prefixed and module-level, mirroring
  `tools/export.py:24 _EXPORTS_DIR = EXPORTS_DIR` so a test can redirect it.
  Putting it in `config.py` was rejected for two reasons: config.py belongs to
  the concurrent slice, and `test_no_tree_writes.py:78` greps config.py for
  `DATA_DIR / "<name>.json"` and requires every hit to appear in its
  `RUNTIME_FILES` list -- a directory declared there would have been outside
  that regex anyway, so the constant would have bought nothing and cost a
  cross-slice edit.
* **`snapshot_dir()` is PURE.** It resolves the path and creates nothing.
  `mkdir` happens inside `save_snapshot` and nowhere else, so importing the
  module, listing snapshots, or pruning a non-existent directory cannot dirty
  the checkout. Control 8 pins this.
* **`profile_snapshots/` is gitignored**, verified with the tool rather than by
  reading the file: `git check-ignore -v profile_snapshots/probe.json` reported
  `.gitignore:100:profile_snapshots/`.

No conflict with `test_no_tree_writes.py` was found; that suite passes
unchanged (`243 passed, 2 skipped` across the five meta-guard modules).

---

## 3. Public surface

| Function | Contract |
|---|---|
| `snapshot_dir() -> Path` | Where snapshots live. Pure, creates nothing. |
| `async save_snapshot(profile, label) -> dict` | Full profile + `{taken_at, label, sections, snapshot_version: 1}`, written through `utils.save_json_atomic`. Returns metadata only, never the body. |
| `list_snapshots() -> list` | Newest first. `{file, taken_at, label, bytes, section_count, readable}`. Never returns content. |
| `load_snapshot(name) -> dict` | Bare filename only. Traversal raises `ValueError`. |
| `prune_snapshots(keep) -> int` | Keeps newest N, returns deletions. `keep < 1` raises. |
| `diff_section(before, after, section) -> dict` | `{added, removed, changed, unchanged_count, id_field, section, present_before, present_after}`. |
| `verify_write(before, after, section, expected) -> dict` | `{verified, reason, diff, collateral, section, expected_keys}`. |
| `rows_lost(before, after, section, id_field) -> list` | Row ids present before, absent after. |
| `detect_id_field(before_rows, after_rows)` | Generic row-id detection, no field names. |
| `read_key` / `write_key` / `resolve_section` | Spelling resolution. |
| `READ_KEY_FOR_WRITE_KEY`, `WRITE_KEY_FOR_READ_KEY`, `WHOLE_LIST_SECTIONS`, `SNAPSHOT_VERSION` | Exported constants. |

### Filename format

```
profile-<YYYYMMDDThhmmssffffffZ>__<slug>__<8 hex>.json
```

Fixed-width UTC stamp first, so plain lexicographic order IS chronological
order -- `list_snapshots` and `prune_snapshots` both sort by NAME, not mtime,
so ordering does not depend on filesystem timestamp resolution or on a file
having been copied. The 8 random hex characters make two saves in the same
microsecond distinct, so a save can never silently overwrite an earlier one.
The slug is stripped of every character outside `[A-Za-z0-9_-]`, **dots
included**: `save_json_atomic` derives its `.backup` and `.tmp` sidecars with
`Path.with_suffix`, which replaces the LAST suffix, so a dot inside a label
would have sent the temp file somewhere unintended.

### The read/write key asymmetry

Five collections come back from the READ under a SINGULAR key and go out on the
WRITE under a PLURAL one. Evidence: `naukri-profile-read-shape.md` (21
top-level sections at `expand_level=4`) and `naukri-profile-write-contract.md`
(per-section body envelopes, rows 13-17).

| WRITE key | READ key |
|---|---|
| `onlineProfiles` | `onlineProfile` |
| `patents` | `patent` |
| `presentations` | `presentation` |
| `publications` | `publication` |
| `workSamples` | `workSample` |

Same-name both ways and therefore identity through the mapping: `employments`,
`educations`, `schools`, `itskills`, `projects`, `certifications`, `languages`,
`profile`.

`resolve_section` tries the given spelling, then the read spelling, then the
write spelling, so every rail accepts either. Control 7 pins that all three of
`rows_lost`, `diff_section` and `verify_write` actually consult it -- a mapping
only one function honours is worse than none.

### `WHOLE_LIST_SECTIONS` -- the six that can lose rows

`onlineProfiles`, `workSamples`, `presentations`, `publications`, `patents`,
`languages`. Write-contract section 6.1: these send **every row every time**, so
a caller that sends one row deletes the rest. `languages` is the trap in the
set -- it is spelled identically on both sides, so it does not show up in the
singular/plural table and is the easiest of the six to forget.
`verify_write`'s `reason` appends an explicit WHOLE LIST warning whenever the
section resolves into this set.

### Row matching is id-aware, and the detector is generic

`diff_section` matches array-of-object rows by a DETECTED row id rather than by
position, so deleting a row from the middle of a collection reports one removed
ROW instead of a cascade of shifted indices naming the wrong rows.

The detector takes no field names. Its rule is structural: a key present on
every row, id-ish by suffix, whose value is a non-empty scalar and whose values
are DISTINCT within each list. That last clause is load-bearing -- the read
payload stamps the same `profileId` on every row of every section, so a
detector that took the first `*Id` key it saw would match every row to every
other row and report a wiped collection as clean. A dedicated test plants
`profileId` on all fixture rows and asserts the detector returns `id`.

The detector returns `None` when neither list has at least two rows: with a
single row, uniqueness cannot discriminate a real row id from a profile-level
anchor, and positional matching is equivalent anyway, so "cannot tell" is the
honest answer rather than a coin flip. `rows_lost` never guesses -- the caller
passes `id_field` explicitly.

### Stated limitations

* `verify_write` accounts a change to `expected` by leaf-path suffix plus
  value, so if the SAME field changed to the SAME value on two different rows,
  both count as accounted-for. Documented in `_is_accounted`. **Row deletion is
  unaffected**: a removed path is NEVER accounted for, by construction, which
  is why the deletion detector cannot be defeated this way. Write-contract 5.4
  is the justification -- deletion of a scalar is expressed as an explicit
  empty string, never as omission, so a path that simply vanished is always a
  loss the caller did not ask for.
* An EMPTY `expected` returns `verified: False`, not vacuously True. A gate
  that passes when asked to check nothing is not a gate.
* `rows_lost` RAISES on a section that is present but is not a list, rather
  than returning a reassuring empty list.

---

## 4. The 8 controls, every one shown RED first

Method: for each control the source was broken deliberately, the test run, the
observed message recorded verbatim, and the file restored. Driver:
`scratchpad/red_driver.py` (disposable; it patches one anchor string, runs one
selector, restores, and verifies restoration by SHA-256). **Ten mutations, ten
reds, zero survivors.**

| # | Control | Deliberate break | Result | Observed RED (verbatim) |
|---|---|---|---|---|
| 1 | A snapshot round-trips | `save_snapshot` persists `"profile": {}` instead of the profile | RED `1 failed` | `E AssertionError: assert {} == {'employments... services.'}]}` / `E Right contains 4 more items:` |
| 2 | `load_snapshot` refuses traversal | early `return Path("%s/%s" % (snapshot_dir(), name))` ahead of every guard | RED `8 failed, 2 passed` | `E FileNotFoundError: no such snapshot: ../secrets.json` / `E FileNotFoundError: no such snapshot: ..\secrets.json` / `E PermissionError: [Errno 13] Permission denied: '...\profile_snapshots\..'` |
| 3a | `prune_snapshots` raises on `keep < 1` | `if keep < 1:` -> `if keep < -999999:` | RED `3 failed` | `E Failed: DID NOT RAISE <class 'ValueError'>` (once per `keep` in 0, -1, -99) |
| 3b | `prune_snapshots` keeps the NEWEST N | prune's sort `reverse=True` -> `reverse=False` | RED `1 failed` | `E AssertionError: assert ['profile-202...eadbeef.json'] == ['profile-202...eadbeef.json']` / `E At index 0 diff: 'profile-20260101T000000000000Z__s1__deadbeef.json' != 'profile-20260104T000000000000Z__s4__deadbeef.json'` |
| 4 | `verify_write` is False when the expected change is absent | `verified = _section_contains(after_value, expected)` -> `verified = True` | RED `1 failed` | `E assert True is False` |
| 5 | **`collateral` catches a silent deletion** | `if not _is_accounted(...)` -> `if False:` (collateral always empty) | RED `1 failed` | `E AssertionError: and the vanished row must still be caught` / `E assert []` |
| 6 | `rows_lost` returns the dropped ids | `if key in after_ids or key in seen:` -> `if True:` (always returns `[]`) | RED `1 failed` | `E AssertionError: assert [] == ['L1', 'L2', 'L4', 'L5']` / `E Right contains 4 more items, first extra item: 'L1'` |
| 7 | Singular/plural mapping, both directions | `READ_KEY_FOR_WRITE_KEY = {}` | RED `12 failed, 10 passed` | `E KeyError: 'onlineProfiles'` / `E KeyError: 'patents'` / `E KeyError: 'presentations'` |
| 8a | The snapshot dir is gitignored | remove `profile_snapshots/` from `.gitignore` | RED `1 failed` | `E AssertionError: profile_snapshots/ holds the FULL live profile read and is written into DATA_DIR, which is the repo root. Un-ignored, the next 'git add -A' commits his employment history.` / `E assert 'profile_snapshots/' in {'*.backup', '*.pyc', ...}` |
| 8b | The suite never writes into the real snapshot dir | `snapshot_dir()` returns `DATA_DIR / "profile_snapshots"` directly, ignoring the redirectable module global | RED `1 failed` | `E AssertionError: the autouse redirect is not in effect` / `E assert WindowsPath('.../naukri/profile_snapshots') != WindowsPath('.../naukri/profile_snapshots')` |

Two notes on what the reds actually proved.

* **Control 2's `PermissionError` is the interesting line.** With the guard
  neutralised, `load_snapshot("..")` did not merely fail to raise -- it reached
  the parent directory and tried to read it. That is direct evidence the guard
  is preventing a real escape, not decorating an impossible one.
* **Control 8b never wrote anything into the checkout.** The isolation
  assertion fires BEFORE the save, so the mutation reds on the assert rather
  than by producing the file it warns about. The checkout was confirmed clean
  afterwards: no `profile_snapshots/` directory exists in the repo.

### Opposite-direction controls (also in the suite)

A guard that rejects everything is not a guard, so each rail is pinned in both
directions: a legitimate bare filename loads; a confined-but-missing name
raises `FileNotFoundError` rather than the traversal `ValueError`; a clean write
returns an EMPTY `collateral`; `prune_snapshots` deletes nothing when `keep`
exceeds the count; `rows_lost` returns `[]` when the list survived; and the
row-id detector is pinned both on ignoring `profileId` and on declining when a
single row cannot discriminate.

---

## 5. Suite

| Run | Result |
|---|---|
| Baseline, before this slice | **2979 passed, 8 deselected** in 308.17s |
| After this slice | **3058 passed, 8 deselected** in 303.44s |

**Zero failures. Zero regressions. No test was modified, skipped or deselected.**

Delta accounting, measured rather than assumed:

| Source | Tests | How measured |
|---|---|---|
| `tests/test_profile_snapshot.py` (this slice) | **+68** | `pytest tests/test_profile_snapshot.py --collect-only` -> 68 |
| `naukri_server/tools/profile_snapshot.py` (this slice) | **+0** | Moved the module out of the tree, re-collected the rest of the suite: 2990 both with and without it. It adds no parametrized cases anywhere else. |
| Concurrent slice | +11 | Whole suite minus my test file collects 2990 vs the 2979 baseline. One is attributable to `config.py` gaining `FULLPROFILES_WRITE_API`, which `test_no_decoys.py` parametrizes over; the other 10 are inside that slice's `api.py` / `interfaces.py` changes. `tests/test_interfaces.py` still collects 8, and `profile_write.py` still declares 0 `@mcp.tool()`, so none of the 11 is a tool-count effect. |

2979 + 68 + 11 = 3058. All green.

The five meta-guard modules that police new source were also run on their own
and pass: `test_no_tree_writes`, `test_no_decoys`, `test_read_path_purity`,
`test_destructive_reachability`, `test_suite_integrity` -- `243 passed,
2 skipped`.

---

## 6. One incident, disclosed

The RED driver restored each patched file with `Path.write_text`, which applies
Windows newline translation. Both files it touched -- `profile_snapshot.py` and
`.gitignore` -- came back as CRLF where the tree uses LF. The driver's own
SHA-256 restoration check caught it and printed `*** MISMATCH ***`; both files
were converted back to LF by byte replacement and re-verified.

Confirmed afterwards: `git diff -- .gitignore` shows exactly the 6 lines this
slice added and nothing else; the only non-ASCII byte in `.gitignore` is an
em-dash on a line that predates this slice (checked against `git show
HEAD:.gitignore`); `profile_snapshot.py` is byte-clean ASCII, compiles, and its
68 tests pass post-restore. No other file was ever opened for writing by the
driver.

---

## 7. One thing for the wave lead to decide

The snapshot directory is redirected off the repo by an **autouse fixture local
to `tests/test_profile_snapshot.py`**, not by `tests/conftest.py`. That is
sufficient today because this slice owns the only callers. Once
`profile_write.py` starts calling `save_snapshot`, any test exercising a write
path will write into `DATA_DIR/profile_snapshots` -- inside the checkout --
unless the redirect moves to `conftest.py`, beside the existing
`agent.POLICY_STATE_PATH` redirect that exists for exactly this reason.

It was NOT moved there in this slice: `conftest.py` is shared, the concurrent
slice is live in the same tree, and the gitignore entry already makes the
failure non-committable. Flagging rather than improvising.
