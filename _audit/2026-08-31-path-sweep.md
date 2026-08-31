# 2026-08-31 - identity paths across three public repositories

Sweep, fixes and new controls for one leak class across `naukri-mcp`, `uplers-mcp` and
`instahyre-mcp`. All three are public.

**This document names no real value.** Where a real identifier was found, it is written
`<given>` for the operator's given name and `<account>` for his Windows account name. That is
not decoration: this file is tracked in a public repository and is itself scanned by
`tests/test_no_committed_identity.py`, so an audit of a leak that quoted the leak would be the
same self-refuting artefact the fixes below exist to remove.

`jobcore` and `linkedin` are excluded - a concurrent agent owned both during this pass.

---

## 1. The instrument

`identity_sweep.py` walks `git ls-files` per repo and reports **eleven classes**, not one count.
Every pattern builds its separator from `chr(92)`, so no quoting layer between the source and the
regex engine can alter what is searched for. That is the whole subject of this pass, so the
instrument could not be allowed to have the defect it was hunting.

The one design decision that matters: the separator run is `+`, one **or more**. Driven against
both spellings before the sweep was trusted:

```
subject bytes (single): 'D:\\<given>\\projects'         <- one separator
subject bytes (double): 'D:\\\\<given>\\\\projects'     <- two

single sep   narrow=True   wide=True
DOUBLE sep   narrow=False  wide=True      <- the blind spot
fwd slash    narrow=True   wide=True
dbl fwd      narrow=False  wide=True
```

Instruments, both uncommitted, at
`D:\workspace\projects\job-hunting\mcp-servers\_audit\tools\` - `identity_sweep.py` (the sweep)
and `redact_identity_paths.py` (the substitution, section 4). They sit above all three repos
because they are cross-repo; committing them was outside this pass's mandate. **Harvest or
delete deliberately** - they are not durable where they are.

---

## 2. What the sweep found

Counts are hits / files over tracked, non-binary files, before any edit.

| class | what it hunts | uplers | instahyre | naukri |
|---|---|---|---|---|
| **A1 drive root + given name** | `X:<sep><given>` | **27 / 9** | **9 / 3** | **4 / 2** |
| A2 drive root, any segment | `X:<sep>anything` | 38 / 12 | 19 / 4 | 34 / 11 |
| **B1 Windows user path** | `Users<sep><account>` | **2 / 1** | **6 / 3** | **4 / 3** |
| **B2 bare account name** | the account name alone | **4 / 1** | **6 / 3** | **4 / 3** |
| C1 POSIX home | `/home/<name>` | 3 / 2 | 2 / 1 | 18 / 7 |
| C2 macOS home | `/Users/<name>` | 1 / 1 | 0 | 4 / 2 |
| **D1 bare given name** | the given name, no path | **61 / 21** | **11 / 5** | **10 / 6** |
| D2 github handle | the repo owner's handle | 5 / 3 | 5 / 5 | 8 / 5 |
| E1 email local-part | the operator's address | 0 | 0 | 0 |
| F1 UNC path | `\\host\share` | 20 / 7 | 32 / 6 | 26 / 13 |
| G1 expanded USERPROFILE | `X:<sep>Users` | 4 / 2 | 10 / 4 | 17 / 6 |

A1 reproduces the brief's floor exactly - 27/9, 9/3, 4/2. **B1 and B2 do not appear in the brief
at all**: the real Windows account name was tracked in all three repos, and in two of them it
sits in **production source**, not test data -
`instahyre_server/paths.py` (x2), `instahyre_server/policy.py`, `naukri_server/policy.py`.

C1 and C2 are entirely `/home/runner`, `/home/user`, an anchored-prose form, and two invented
names under a home root. There is no real POSIX home on this machine; the class is clean,
which is a different statement from unexamined. F1 is vendor URLs inside captured fixtures plus
elided documentation. A2 and G1 are supersets kept for calibration.

---

## 3. The finding: three repos, three different reasons, one green suite each

**naukri had the rules and they were blind.** Measured directly rather than inferred - the guard
at HEAD, run against the HEAD tree in a detached worktree:

```
=== HEAD tree, HEAD guard: the shipped state ===
426 passed in 17.86s
```

Zero failures, over a tree carrying eight tracked lines that hold either the given name as a
drive root or the real account name. Its own `hits_in()` returns `[]` on every one:

```
tests/test_config_prose_leak.py:737          naukri guard hits_in() -> []
tests/test_resource_prompt_scrubbing.py:18   naukri guard hits_in() -> []
tests/test_resource_prompt_scrubbing.py:41   naukri guard hits_in() -> []
tests/test_resource_prompt_scrubbing.py:49   naukri guard hits_in() -> []
naukri_server/policy.py:331                  naukri guard hits_in() -> []
tests/test_apply_patch_path_leak.py:12       naukri guard hits_in() -> []
tests/test_apply_patch_path_leak.py:423      naukri guard hits_in() -> []
tests/test_config_tool.py:93                 naukri guard hits_in() -> []
                                    TOTAL:    0 hits over 8 real leaking lines
```

The mechanism is one character. All three rules were written `[\\/]` - exactly one separator -
so the captured segment had to start on the character immediately after it. On the doubled
spelling that character is another separator, and the rule does not match. **The doubled form is
the common one in committed code**: a JSON config, a Python string literal, a docstring quoting
either, and `repr()` of any Windows path all write it. Every leak found across all three repos
was doubled.

The existing control asserted both separator *characters*. It never asserted the separator
*count*. That is exactly the gap it fell through.

**uplers and instahyre had no rule at all.** This is the part the brief did not predict, and it
is worse than a blind rule. Both have a `tests/test_path_hygiene.py` that is genuinely thorough -
but its subject is a **tool result at runtime**. It walks payloads and has never read a tracked
file. Both also have a `tests/test_pii_hygiene.py` that *does* walk `git ls-files`, and it hunts:

```
uplers    (15 shapes): GIT_SHA40, EMAIL_SHAPE, PHONE_IN_SHAPE, PHONE_E164_SHAPE,
                       PHONE_ID_CONTEXT, LINKEDIN_SLUG, LINKEDIN_COMPANY_ID,
                       LINKEDIN_MEMBER_TOKEN, LINKEDIN_URN_ID, JWT_SHAPE,
                       COOKIE_ASSIGNMENT, BARE_INT_4PLUS, TITLECASE_WORDS,
                       LOWER_SLUG_2SEG, SUSPICIOUS_TABLE_NAME
          PATH-shaped: NONE

instahyre (20 shapes): the same 15, plus ACCOUNT_SCOPED_ID_IN_PATH,
                       SYNTHETIC_ID_PLACEHOLDER, ACCOUNT_KEY_NAME,
                       ENTROPIC_ATOM, PUBLIC_AVATAR_PATH
          PATH-shaped: ACCOUNT_SCOPED_ID_IN_PATH and PUBLIC_AVATAR_PATH -- both are
                       URL-route rules. Neither is a filesystem path.
```

So between the two guards each repo left an exact hole: **payloads checked for paths, committed
files checked for everything but paths.** The class was UNGUARDED, not under-guarded.

The irony in instahyre is worth recording. The leaking docstrings in `paths.py` and `policy.py`
are the ones explaining how a path gets relativised, and how `repr()` doubles a separator so an
exact-substring scrubber walks straight past it. They were correct about the mechanism and
demonstrated it with the real value.

---

## 4. Fixes: the value, never the escaping

The stated trap - a redaction that reports success and changes nothing, because the replacement
was written with doubled backslashes against a file holding single ones - is not defended against
with care here. It is made **structurally impossible**:

1. **The separator run is captured and re-emitted, never written.** Whatever the file holds
   comes back byte for byte, because it is `group(1)` copied, not reconstructed. The needle does
   not depend on the spelling it hunts.
2. **The replacement is a lambda, not a template.** `re.sub` template syntax parses backslashes;
   a function does not. No layer is left that can mangle one.

Every substitution asserts its count against the pre-measured sweep, and every file is re-scanned
afterwards and must come back at zero. A redaction that changed nothing fails the tool.

One more defect was caught before it shipped: all three trees are CRLF on disk except two files,
and reading with default translation then writing would have silently rewritten every line of all
18 files. Both sides now use `newline=""`. The diff stats confirm it - **31/31, 15/15 and 8/8
line changes**, each equal to its substitution count, with no line-ending churn.

Replacements: the given name in a drive root becomes `workspace` (already in naukri's
`GENERIC_DRIVE_ROOTS` - it names a place); the account becomes `user` (already in
`PLACEHOLDER_ACCOUNTS`). Two occurrences in uplers had the account assembled from parts
(`B.join(("C:", "Users", <account>, ...))`) or printed with separators stripped, so no path-shape
regex can reach them; both were named as literal pairs, neither side containing a backslash.

| file | given | account | assembled |
|---|---|---|---|
| `uplers/README.md` | 2 | - | - |
| `uplers/_audit/_slices/_slice-assessment-flags.md` | 2 | - | - |
| `uplers/_audit/_slices/_slice-browser-parity-census.md` | 10 | - | - |
| `uplers/_audit/_slices/_slice-outreach-readthrough.md` | 2 | - | - |
| `uplers/_audit/_slices/_slice-resume-write-build.md` | 2 | - | - |
| `uplers/_audit/_slices/_slice-saved-and-preference.md` | 4 | - | - |
| `uplers/_audit/_slices/_slice-server-info-honesty.md` | 1 | - | - |
| `uplers/tests/test_path_hygiene.py` | 3 | 2 | 2 |
| `uplers/uplers_server/policy.py` | 1 | - | - |
| `instahyre/instahyre_server/paths.py` | 3 | 2 | - |
| `instahyre/instahyre_server/policy.py` | - | 1 | - |
| `instahyre/tests/test_path_hygiene.py` | 5 | 3 | - |
| `instahyre/tests/test_scoring_policy.py` | 1 | - | - |
| `naukri/naukri_server/policy.py` | - | 1 | - |
| `naukri/tests/test_apply_patch_path_leak.py` | - | 2 | - |
| `naukri/tests/test_config_prose_leak.py` | 1 | - | - |
| `naukri/tests/test_config_tool.py` | - | 1 | - |
| `naukri/tests/test_resource_prompt_scrubbing.py` | 3 | - | - |
| **TOTAL** | **40** | **12** | **2** |

Every file reported `left=0/0` after substitution. The escaping survived at all three depths -
r-string, doubled and quadrupled:

```
-        OSError(13, "Permission denied", r"C:\Users\<account>\config\jobhunt.json")
+        OSError(13, "Permission denied", r"C:\Users\user\config\jobhunt.json")
-        str(...) -> [Errno 13] Permission denied: 'C:\\Users\\<account>\\config\\...'
+        str(...) -> [Errno 13] Permission denied: 'C:\\Users\\user\\config\\...'
-    client actually receives, and JSON escaping (`D:\\\\<given>`) still contains
+    client actually receives, and JSON escaping (`D:\\\\workspace`) still contains
```

Where the real path was test DATA proving a detector works, the synthetic segment keeps every
property the test asserted - each of those tests asserts a *shape* is caught, never that the
segment is any particular value.

---

## 5. Rules

### naukri - widened

`tests/test_no_committed_identity.py`. Each rule's separator run becomes `+`.
`POSIX_HOME_PATH` additionally gains `/` in its lookbehind: widening to `/+` would otherwise let
it start on the second slash of `https://home/x`, past the colon it was written to block.
Measured both ways - the tracked-file hit set is unchanged and the URL stays silent.

`_account_ok` gains the ellipsis/angle-bracket clause its sibling `_drive_root_ok` has had since
the day it was written. **The widening is what made this necessary, and the sequence is the
point**: while the run was one character, this predicate never saw the elided form at all, so the
two predicates disagreed about what a documentation placeholder is and the disagreement was
invisible. The first thing the widening did was report an ellipsis as a leaking account name.
Fixed in the predicate. **`DECLARED_PLANTS` remains `{}`.**

Three doubled-spelling entries join `PLANTED` and four elided/URL entries join `BENIGN`. Those
registers were as blind as the rules: **every path entry in both was written with a single
separator**, so the register could not have caught the defect it existed to prove.

### uplers and instahyre - ported

325 lines appended to each `tests/test_pii_hygiene.py` - the tracked-file walker, which is where
a committed-file rule belongs. Three rules (`WINDOWS_USER_PATH`, `DRIVE_ROOT_PATH`,
`POSIX_HOME_PATH`), two allowlists, five tests. Every separator is built from `chr(92)` at import
time, so **the source contains no literal doubled backslash inside a pattern at all** - a
collapse has nothing to collapse.

The allowlists are measured, not guessed. After the fixes the entire tracked tree of both repos
contains these drive roots and accounts, and every one is generic:

```
uplers      roots: claude-workspace, opt, users, workspace   accounts: ..., user   homes: runner
instahyre   roots: leak, users, workspace                    accounts: ..., user   homes: runner
```

Both suites' new checks are driven over a `PATH_SHAPES` registry, so a rule added later is
covered the moment it is registered rather than the day someone remembers to extend a test.

---

## 6. Every new check, shown failing

**Mutation 1 - undo the `+` on every rule** (i.e. restore the pre-2026-08-31 rules), then run the
shipped control body. naukri and both ported copies:

```
AssertionError: WINDOWS_USER_PATH IS BLIND TO THE DOUBLED-SEPARATOR SPELLING --
the exact defect that let 52 identity paths ship green on 2026-08-31
```

**Mutation 2 - make the mutation a no-op** (rewrite the run so `]+`->`]` changes nothing), then
run the control-for-the-control:

```
AssertionError: WINDOWS_USER_PATH: the mutation changed nothing, so this control is
inert -- the separator run is no longer spelled the way it was fixed
```

**Mutation 3 - a predicate that allows everything**, then run the planted-layout control:

```
AssertionError: a planted absolute layout was NOT caught, so the corresponding absence
assertion certifies nothing: 'traceback from C:\\Users\\<invented>\\AppData'
```

The account in that last line is elided, and the reason is worth a sentence. The plant is an
INVENTED name, but a plant must be shape-valid or it proves nothing - so quoting the failure
verbatim in this file trips the very guard this document reports on. It did: the first draft
fired four times here. **A name has no shape**, and the guard cannot tell an invented one from a
real one, which is the same limit section 8 names for the class it cannot cover.

**Mutation 4 - a predicate that allows nothing**, then run the benign control:

```
AssertionError: an already-synthetic path was reported as a leak, which is how a guard
gets narrowed into uselessness: 'scrubbed to C:\\Users\\runner\\work'
```

**Mutation 5 - the real historical content.** Not a synthetic fixture: the actual lines these
repos shipped, pulled from `git show HEAD:`, driven through the pre-fix rule and the shipped one.
This is the strongest of the five, because the subject is a genuine historical defect.

```
naukri  tests/test_resource_prompt_scrubbing.py
          PRE-FIX rule over the SHIPPED file  -> 0 hits []
          WIDENED rule over the SHIPPED file  -> 3 hits [drive root x3]
        naukri_server/policy.py
          PRE-FIX rule over the SHIPPED file  -> 0 hits []
          WIDENED rule over the SHIPPED file  -> 1 hits [user path]

uplers    the NEW check over uplers' own HEAD content    -> 29 findings
instahyre the NEW check over instahyre's own HEAD content -> 15 findings
```

The instahyre run in full, and note that the assertion message redacts every value - a guard that
prints what it found has merely moved the leak:

```
AssertionError -- 15 findings:
  instahyre_server/paths.py:8    DRIVE_ROOT_PATH    D:...ep  (segment Su...ep is not generic)
  instahyre_server/paths.py:9    DRIVE_ROOT_PATH    D:...ep  (segment Su...ep is not generic)
  instahyre_server/paths.py:10   DRIVE_ROOT_PATH    D:...ep  (segment Su...ep is not generic)
  instahyre_server/paths.py:98   WINDOWS_USER_PATH  C:...ll  (segment <4 chars, letters>)
  instahyre_server/paths.py:99   WINDOWS_USER_PATH  C:...ll  (segment <4 chars, letters>)
  instahyre_server/policy.py:164 WINDOWS_USER_PATH  C:...ll  (segment <4 chars, letters>)
  tests/test_path_hygiene.py:8   DRIVE_ROOT_PATH    D:...ep  (segment Su...ep is not generic)
  tests/test_path_hygiene.py:9   DRIVE_ROOT_PATH    D:...ep  (segment Su...ep is not generic)
  tests/test_path_hygiene.py:10  DRIVE_ROOT_PATH    D:...ep  (segment Su...ep is not generic)
  tests/test_path_hygiene.py:417 DRIVE_ROOT_PATH    D:...ep  (segment Su...ep is not generic)
  tests/test_path_hygiene.py:418 WINDOWS_USER_PATH  C:...ll  (segment <4 chars, letters>)
  tests/test_path_hygiene.py:462 DRIVE_ROOT_PATH    D:...ep  (segment Su...ep is not generic)
  tests/test_path_hygiene.py:746 WINDOWS_USER_PATH  C:...ll  (segment <4 chars, letters>)
  tests/test_path_hygiene.py:857 WINDOWS_USER_PATH  C:...ll  (segment <4 chars, letters>)
  tests/test_scoring_policy.py:339 DRIVE_ROOT_PATH  D:...ep  (segment Su...ep is not generic)
```

No check survived its mutations, so none was deleted.

---

## 7. Suites and commits

| repo | suite before | suite after | delta | commit |
|---|---|---|---|---|
| uplers | 1706 passed | **1711 passed** | +5 | `5e1b5f4` |
| instahyre | 1541 passed | **1546 passed** | +5 | `4e944ab` |
| naukri | 4038 passed, 8 deselected | **4046 passed, 8 deselected** | +8 | `b4c25840` |

`+5` in each ported repo is exactly the five new tests. naukri's `+8` is two new controls plus
six register entries. naukri's guard file alone: **426 -> 434**.

**Correction to `b4c25840`'s own message, which says `4038 -> 4045`.** That was the figure in
front of me when the commit was written, and it was a run with one failure -
`test_server_info.py::TestJobcoreIsStampedSeparately::test_the_jobcore_commit_is_jobcores_own_head`.
That failure is not this change: the test passes in isolation, and jobcore's reflog shows its
HEAD moved to `b2f5d16` at 18:03:49 - a concurrent agent committing to jobcore while the suite
was mid-run, so the stamp was read stale. The post-commit gate, run after jobcore settled, is
**4046 passed, 8 deselected, 0 failed**. Trust this table over the commit message.

Live-server constraints held. Port 8321 still listening throughout; `naukri.db`, `sync_state.json`
and `healing_state.json` are gitignored, untracked and were never written by this pass; nothing
was staged outside the explicit path lists.

After the fixes, across all three repos: **A1 = 0, B2 = 0.** Residual B1 is `users/self` (a Naukri
REST route, not a filesystem path), `Users/user`, `Users/runner`, `Users/TestUser` and one
invented surname. Residual C1/C2 is `/home/runner`, `/home/user`, an anchored-prose form and
invented names. Every residual segment is allowlisted-generic or invented; none names anybody.

---

## 8. NOT COVERED

Three separate guards were green over live leaks this morning. A fourth false all-clear is the
one outcome worse than finding nothing, so this section is the load-bearing one.

**D1 - the bare given name is NOT fixed, and in uplers it is the operator's real full personal
name.** This is the largest unfixed class and the one I would escalate first. `uplers` fell from
61 to 34 hits as a side effect of the path fixes; instahyre 11 -> 2; naukri 10 -> 6. What remains
in uplers is not a path at all:

- `tests/fixtures/talent_profile.json` is a **captured live profile** holding his real full name
  in `name` and in a `name_extration` first/last split, alongside headline, city, total
  experience and a written objective paragraph.
- ~28 assertions across `test_talent_shape.py`, `test_talent_tools.py`, `test_session.py`,
  `test_session_lifecycle.py`, `test_auth.py`, `test_policy_wiring.py`,
  `test_profile_direction.py` and `scripts/presence_is_auth_control.py` echo that name, including
  a surname and a full-name/first-name/last-name triple.
- `LICENSE:1` names him as copyright holder. That one is deliberate and should stay.

I did not touch this because the brief scoped me to path classes and because it is a different
piece of work with real regression risk across ~28 assertions - not because it is safe. **A
personal name has no shape**, so neither the new rules nor any rule in these repos can find the
next one; only a human reading values can. Naukri's own guard says this in its docstring and it
is correct.

**Also not covered:**

- **D2, the github handle** (5/5/8 hits) - public by construction, since all three repos are owned
  by that account. Counted, not fixed.
- **Published history.** Cleaning a tree does not remove a value from pushed history, and only
  delete-and-recreate was measured to remove retained objects. Every value redacted above is
  still resolvable by SHA in the public remotes. That is the operator's call and he has not made
  it. **Nothing here should be read as "the value is gone from GitHub."**
- **`jobcore` and `linkedin`** - owned by a concurrent agent, not swept by me. jobcore's
  `b2f5d16` says `fix(identity): the vendored file leaked a name` and looks like the same class,
  but I did not verify it.
- **The other repos under `mcp-servers/`** - `ats-jobs`, `jobspy`, `unipile` - not swept at all.
- **Untracked and gitignored files.** The sweep reads `git ls-files` only, which is the right
  denominator for "what is published" and the wrong one for "what is on this disk."
- **Binary and generated files** - suffixes skipped by both the sweep and the guards.
- **F1, UNC paths** (20/32/26) - reviewed by eye and every one is a vendor API host inside a
  captured fixture or an elided placeholder. No rule was written, so a real UNC path added
  tomorrow is caught by nothing.
- **A2 and G1 supersets** - drive roots and `X:<sep>Users` prefixes that are generic today. The
  new rules cover the person-named cases; a *machine*-identifying but not person-identifying
  layout string is still committed in several places by design.
- **The instruments themselves are uncommitted** (section 1). If they are not harvested they are
  gone.

Finally, and in the same spirit as the finding above: the new rules hunt three path SHAPES. A
green run means "no paths of these three shapes naming a non-generic segment." It does not mean
the repository is clean.
