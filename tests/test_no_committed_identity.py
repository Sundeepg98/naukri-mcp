"""No tracked file may carry a real identity, or the key to one.

WHAT A GREEN RUN MEANS, AND WHAT IT DOES NOT
--------------------------------------------
**NAMES HAVE NO SHAPE.** This guard hunts IDENTIFIERS. A green suite means "no
identifiers of the shapes hunted here". It does NOT mean "no PII" and it does
NOT mean "no third-party personal data".

Every real name removed from this repository was found by a human reading field
names and values, not by any guard -- and none of them could have been found by
a shape check, because ``Priya Raman`` and an invented ``Alex Rivera`` are the
same shape, the same length, the same character classes. **Do not let a green
run be read as clean.**

That is not an abstract caveat here. Measured on this repo the day this file
was written: ``naukri_server/tools/inbox.py`` carries a real third-party
recruiter name, and this guard fires on it ZERO times, across every shape
below. The file is silent in every list this module can produce. That single
measurement is the whole paragraph above, demonstrated.

THE DESIGN RULE, WHICH EVERY CHECK BELOW OBEYS
----------------------------------------------
**Hunt by SHAPE. Allowlist the SYNTHETIC. Never blocklist the real.**

A committed list of real strings IS a de-anonymisation key -- that is the
defect this whole effort exists to remove, so the guard may not contain one.
Every allowlist here holds only obviously-fake values, safe to commit because
they already ARE the literal content of the committed fixtures. When a check
fires on something already synthetic, WIDEN THE ALLOWLIST -- never narrow the
shape, never delete the check.

Corollary, and it is not decoration: **failure messages render redacted**. A CI
log is a publication channel, and a guard that prints the identifier it found
has republished it somewhere new.

THE FILE LIST COMES FROM ``git ls-files``, ALWAYS
-------------------------------------------------
Never a glob, never an enumerated set, never a chosen subdirectory. That is the
whole lesson from the sibling repos: a previous guard covered 4 fixtures out of
18 and the leak lived in the gap. A ``git ls-files`` list means file 406 is
covered the day someone adds it.

TWO REGISTERS, AND WHY THEY ARE DIFFERENT
------------------------------------------
* :data:`DECLARED_PLANTS` -- files that legitimately CONTAIN a shape-valid
  string as test scaffolding, pinned BY COUNT rather than skipped. A skip is a
  promise that nothing in the file needs checking; a count is not. A new real
  identifier in one of these files changes the count and goes red.
* :data:`UNREMEDIATED_LEAKS` -- files with a GENUINE find that has not been
  scrubbed yet. These are marked ``xfail(strict=True)``: they FAIL today and
  are reported as failures, and the day the data is removed the test XPASSes,
  strict turns that red, and the register entry must be deleted. A leak is
  never quieted by widening an allowlist -- doing that is the exact defect this
  module exists to prevent.

WHY THE TOKEN BOUNDARY, MEASURED ON THIS REPO
----------------------------------------------
``(?<![A-Za-z0-9_])...(?![A-Za-z0-9_])``, never a digit boundary. A digit
boundary does not stop a match INSIDE a longer alphanumeric token, and a
10-digit run inside a 40-hex git SHA is the commonest false positive here:
sweeping every tracked file, the digit-bounded phone pattern produced 8 hits
and the token-bounded one produced 3. All 5 of the difference were digit runs
inside 40-hex commit SHAs. The same boundary kills a prose ``anchored/home/tail``
reading as a home directory, and a Python local named ``_has_bearer`` reading
as a bearer-token assignment. See
:func:`test_a_digit_boundary_would_match_inside_a_git_sha_and_a_token_one_does_not`.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: This module by path. It necessarily contains regexes and planted controls
#: that look exactly like the shapes it hunts, so it cannot sweep itself.
#: An exclusion is a promise, so it is kept to precisely one path and
#: :func:`test_exactly_one_path_is_skipped` pins that nobody adds a second.
SELF = "tests/test_no_committed_identity.py"

# ===========================================================================
# WIRING -- the repo-specific part. Every value below is invented or reserved.
# ===========================================================================

#: Reserved and stub domains. Subdomains of a reserved name are reserved too,
#: which is why a suite may use ``attacker@evil.example.com``.
SYNTHETIC_EMAIL_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "localhost",
        # This repo's own synthetic fixture domains, DISCOVERED by running the
        # check with an empty allowlist and reading what fired -- not guessed.
        # Listing them is safe: they are already the literal committed content
        # of the tests below, and none of them resolves to anything.
        "hireco.com",  # tests/test_job_parsing_deep.py, invented recruiter co
        "betacorp.com",  # tests/test_tracking_helpers.py, invented employer
        "test.com",  # tests/test_health_deep.py, stub profile payload
        "email.com",  # probing/*.md, the literal placeholder "user@email.com"
    }
)
SYNTHETIC_EMAIL_SUFFIXES = (
    ".invalid",
    ".example.com",
    ".example.org",
    ".example.net",
)

#: Placeholder numbers. Anything all-zeroes is also allowed, which is what
#: covers Naukri's own masked virtual number ``+910000000000``.
SYNTHETIC_PHONES = frozenset({"9876543210", "1000000000", "0000000000"})

#: Account names in an absolute path that identify nobody. ``runner`` is the
#: GitHub Actions account and appears in this repo's CI-path reasoning; the
#: rest are ordinary placeholders. Matched case-insensitively, and also as a
#: substring token so ``testuser`` and ``some-user`` pass without listing.
PLACEHOLDER_ACCOUNTS = frozenset(
    {
        "you",
        "user",
        "username",
        "me",
        "someone",
        "somebody",
        "anonymous",
        "test",
        "runner",
        "windows",
        "public",
        "default",
        "home",
        "root",
    }
)
PLACEHOLDER_ACCOUNT_TOKENS = ("user", "test", "example", "placeholder", "runner")

#: A credential value that is self-evidently not one. A backtick means the
#: value sits in a markdown code span, i.e. documentation naming an env var
#: rather than carrying its contents -- which is what ``Bearer
#: (`MCP_SHARED_SECRET`)`` in README.md is. ``$`` and ``{`` cover shell and
#: template references for the same reason.
PLACEHOLDER_MARKERS = (
    "xxx",
    "dummy",
    "fake",
    "redacted",
    "placeholder",
    "<",
    "...",
    "`",
    "$",
    "{",
)

#: Words that mark a 64-hex string in an ``_audit/`` note as a FILE DIGEST --
#: build evidence, not an identifier. Deliberately a CONTEXT allowance and not
#: a list of values: a new slice note recording a new digest is covered the day
#: it is written, and a 64-hex string in an audit note with no digest language
#: near it still fires.
DIGEST_CONTEXT = re.compile(r"sha-?256|digest|checksum|byte-for-byte", re.I)
DIGEST_CONTEXT_WINDOW = 400
DIGEST_DIRS = ("_audit/",)

# ===========================================================================
# The shapes
# ===========================================================================

#: Suffixes whose bytes are not text and cannot be read for identifiers.
BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".db", ".ico",
        ".woff", ".woff2", ".ttf", ".pyc", ".so", ".dll", ".exe", ".whl",
        ".db-shm", ".db-wal",
    }
)

#: Files whose content is hashes, which manufacture false emails and false
#: ten-digit runs.
HASHY = re.compile(r"(requirements.*\.txt|\.lock)$", re.I)

#: A 40-hex commit SHA, token-bounded. A line carrying one is skipped by the
#: phone check: a SHA contains 10-digit runs that start 6-9, and this repo has
#: many. Measured, this subtracts 0 hits TODAY -- the token boundary already
#: catches every current case -- and it is kept because it is the one shape
#: that survives a SHA whose first ten characters happen to be digits.
GIT_SHA = re.compile(r"(?<![A-Za-z0-9_])[0-9a-f]{40}(?![A-Za-z0-9_])")

EMAIL_SHAPE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")

#: TOKEN boundaries, not digit boundaries -- see the module docstring.
PHONE_SHAPE = re.compile(
    r"(?<![\dA-Za-z_.])(?:\+?91[-\s]?)?[6-9]\d{9}(?![\dA-Za-z_.])"
)
PHONE_E164_SHAPE = re.compile(
    r"(?<![\dA-Za-z_.])\+\d{1,3}[-\s]?\d{6,12}(?![\dA-Za-z_.])"
)

#: BOTH separator forms, in ONE character class. This is written with the Write
#: tool and never through a shell heredoc: a heredoc collapses the doubled
#: backslash, the class silently degrades to forward-slash-only, and the
#: Windows half of the pattern goes dead without a single test changing.
#: :func:`test_the_windows_path_shape_matches_both_separator_forms` asserts
#: both halves so that degradation fails loudly instead of going quiet.
WINDOWS_USER_PATH = re.compile(r"[A-Za-z]:[\\/]Users[\\/]([A-Za-z0-9._-]{2,})")

#: A drive path whose FIRST segment is not a generic one -- e.g. ``D:\Given``.
#:
#: This is a separate check from :data:`WINDOWS_USER_PATH` because that one
#: requires a literal ``Users`` segment, and a drive rooted straight at a
#: person's name has none. That gap was not hypothetical: it published a
#: given name 14 times across 8 files, and it survived this guard's first
#: version -- the guard went green over a file that carried the leak, because
#: the leak was one path segment to the left of where the check was looking.
#:
#: Measured before writing it: the whole repository contains 10 distinct
#: absolute drive-path roots, of which 9 are already generic. So the allowlist
#: below is small, auditable, and holds ONLY generic tokens -- no real value is
#: named here, which is what keeps this an allowlist of the synthetic rather
#: than a blocklist of the real. Widen it when a genuinely generic root fires.
DRIVE_ROOT_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]([A-Za-z0-9_.-]{2,})")

GENERIC_DRIVE_ROOTS = frozenset(
    {
        "users",        # handed to WINDOWS_USER_PATH, which checks the NEXT segment
        "windows",
        "programdata",
        "program",      # "Program Files" truncates at the space
        "workspace",
        "dev-cache",
        "temp",
        "tmp",
        "repo",
        "out.csv",      # a test corpus literal
    }
)

#: The POSIX home form. The lookbehind excludes ``:`` so a drive-letter path is
#: counted once, by the shape above, and not twice; and it excludes word
#: characters so the prose ``anchored/home/tail`` stops reading as a home
#: directory.
POSIX_HOME_PATH = re.compile(
    r"(?<![A-Za-z0-9_:])(?:/home/|/Users/)([A-Za-z0-9._-]{2,})"
)

HEX32_SHAPE = re.compile(r"(?<![A-Za-z0-9_])[0-9a-fA-F]{32}(?![A-Za-z0-9_])")
HEX64_SHAPE = re.compile(r"(?<![A-Za-z0-9_])[0-9a-fA-F]{64}(?![A-Za-z0-9_])")

JWT_SHAPE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
)

#: A credential assignment. The keyword is TOKEN-BOUNDED on both sides, which
#: is what stops the Python local ``_has_bearer = bool(os.environ.get(`` in
#: naukri.py from reading as a bearer token; the separator accepts whitespace
#: as well as ``=``/``:`` so that a bare ``Bearer <token>`` still fires.
CREDENTIAL_ASSIGN = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:nauk_at|nauksessionid|NKWAP|jssid|MYNAUKRI|sessionid|csrftoken"
    r"|Authorization|Bearer)"
    r"(?![A-Za-z0-9_])"
    r"[\s=:]{1,4}\"?([^\s\"',]{20,})",
    re.I,
)

AWS_KEY_SHAPE = re.compile(r"(?<![A-Za-z0-9_])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9_])")
GITHUB_TOKEN_SHAPE = re.compile(
    r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9_])"
)
PRIVATE_KEY_SHAPE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def redact(value: str) -> str:
    """``<first2>..<last2>`` plus a length. Never the identifier itself."""
    if len(value) <= 6:
        return "<%d chars>" % len(value)
    return "%s..%s <%d chars>" % (value[:2], value[-2:], len(value))


# ---------------------------------------------------------------------------
# Allow predicates. Each answers "is this hit already synthetic?"
# ---------------------------------------------------------------------------


def _email_ok(match: re.Match, text: str, rel: str) -> bool:
    # An address the SITE itself masked -- "q*****4@gmail.com" -- reaches the
    # shape as "4@gmail.com" because "*" is not a local-part character. It is
    # masked at source and is not ours to fix, so it is allowed by CONTEXT
    # (a "*" immediately before the match) rather than by value.
    if match.start() > 0 and text[match.start() - 1] == "*":
        return True
    domain = match.group(0).rsplit("@", 1)[1].lower().rstrip(".")
    if domain in SYNTHETIC_EMAIL_DOMAINS:
        return True
    return any(domain.endswith(suffix) for suffix in SYNTHETIC_EMAIL_SUFFIXES)


def _phone_ok(match: re.Match, text: str, rel: str) -> bool:
    digits = re.sub(r"\D", "", match.group(0))
    for cut in (0, 1, 2, 3):
        candidate = digits[cut:]
        if candidate and (set(candidate) == {"0"} or candidate in SYNTHETIC_PHONES):
            return True
    # A line carrying a 40-hex commit SHA is not a line carrying a telephone
    # number. Kept even though it subtracts nothing today -- see GIT_SHA.
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    line = text[line_start : line_end if line_end != -1 else len(text)]
    return bool(GIT_SHA.search(line))


def _account_ok(match: re.Match, text: str, rel: str) -> bool:
    account = match.group(1).lower().strip("._-")
    if account in PLACEHOLDER_ACCOUNTS:
        return True
    return any(token in account for token in PLACEHOLDER_ACCOUNT_TOKENS)


#: Opaque ids that are INVENTED. Safe to commit precisely because none of them
#: is real -- this is an allowlist of the synthetic, never a blocklist of the
#: real, and a committed list of real values would itself be the de-anonymising
#: key this whole guard exists to prevent.
#:
#: These four replaced real inbox ids in `tests/test_inbox_field_paths.py` on
#: 2026-08-24. They keep the fixture's SHAPE (32 lowercase hex) because that
#: shape is what the fixture proves; the repeating four-character groups are
#: deliberate, so a human reading the file can see at a glance they are fake.
#: GROW this set when a check fires on something already synthetic. Never
#: narrow the shape, and never delete the check.
SYNTHETIC_IDS = frozenset(
    {
        "aaaa1111bbbb2222cccc3333dddd4444",
        "bbbb2222cccc3333dddd4444eeee5555",
        "cccc3333dddd4444eeee5555ffff6666",
        "dddd4444eeee5555ffff6666aaaa7777",
    }
)


def _digest_ok(match: re.Match, text: str, rel: str) -> bool:
    """An invented id, or a file digest recorded in an ``_audit/`` note.

    CONTEXT, not value, for the digest half: the path must be an audit note AND
    digest language must appear in the preceding window. A hex string in a
    fixture row has neither and still fires, which is the separation the spec
    asks for -- unless the value is one we invented ourselves.
    """
    if match.group(0).lower() in SYNTHETIC_IDS:
        return True
    if not rel.replace("\\", "/").startswith(DIGEST_DIRS):
        return False
    # Scope to the markdown BLOCK the hex sits in, plus a window before it --
    # not a flat character count. A digest table declares "SHA-256" once in its
    # header and then runs for as many rows as it has; a flat window allows the
    # first rows and fires on the last, which is an arbitrary line to draw.
    block_start = text.rfind("\n\n", 0, match.start()) + 2
    window = text[max(0, block_start - DIGEST_CONTEXT_WINDOW) : match.start()]
    return bool(DIGEST_CONTEXT.search(window))


def _drive_root_ok(match: re.Match, text: str, rel: str) -> bool:
    """A drive path is fine if its first segment names a place, not a person.

    ``D:\\workspace`` says nothing about who owns the machine. ``D:\\Given``
    says exactly who owns it, and that is the form that leaked. Ellipsis and
    angle-bracket placeholders are documentation, not layout.
    """
    seg = match.group(1)
    if seg.lower() in GENERIC_DRIVE_ROOTS:
        return True
    if any(tok in seg.lower() for tok in PLACEHOLDER_ACCOUNT_TOKENS):
        return True
    return "." * 3 in seg or "<" in seg


def _credential_ok(match: re.Match, text: str, rel: str) -> bool:
    value = match.group(1) if match.groups() else match.group(0)
    lowered = value.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    return len(set(value)) <= 2


def _never_ok(match: re.Match, text: str, rel: str) -> bool:
    """AWS keys, GitHub tokens and PEM blocks have no benign spelling.

    Measured on this repo: ZERO of all three today. The check exists to keep it
    that way, which is the only reason a check with no current hits is worth
    its line count.
    """
    return False


#: name -> (pattern, allow predicate). The name is what a failure reports and
#: what both registers are keyed on.
SHAPES = (
    ("email", EMAIL_SHAPE, _email_ok),
    ("phone", PHONE_SHAPE, _phone_ok),
    ("phone", PHONE_E164_SHAPE, _phone_ok),
    ("user path", WINDOWS_USER_PATH, _account_ok),
    ("user path", POSIX_HOME_PATH, _account_ok),
    ("drive root", DRIVE_ROOT_PATH, _drive_root_ok),
    ("hex32 id", HEX32_SHAPE, _digest_ok),
    ("hex64 id", HEX64_SHAPE, _digest_ok),
    ("credential", JWT_SHAPE, _credential_ok),
    ("credential", CREDENTIAL_ASSIGN, _credential_ok),
    ("credential", AWS_KEY_SHAPE, _never_ok),
    ("credential", GITHUB_TOKEN_SHAPE, _never_ok),
    ("credential", PRIVATE_KEY_SHAPE, _never_ok),
)

SHAPE_NAMES = tuple(dict.fromkeys(name for name, _, _ in SHAPES))


def hits_in(text: str, *, rel: str = "<string>", only: str | None = None):
    """``(shape name, redacted value)`` for everything not allowed."""
    found = []
    for name, pattern, allowed in SHAPES:
        if only is not None and name != only:
            continue
        if name in ("email", "phone") and HASHY.search(rel):
            continue
        for match in pattern.finditer(text):
            if not allowed(match, text, rel):
                found.append((name, redact(match.group(0))))
    return found


def counted(text: str, rel: str):
    """``{shape name: hit count}`` for one blob of text."""
    tally = {}
    for name, _ in hits_in(text, rel=rel):
        tally[name] = tally.get(name, 0) + 1
    return tally


def read_tracked(rel: str) -> str:
    try:
        return (REPO / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - unreadable blob
        return ""


@lru_cache(maxsize=1)
def tracked_files() -> tuple:
    """Every file git tracks. The list is never a glob and never enumerated."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return tuple(line.strip() for line in out.splitlines() if line.strip())


@lru_cache(maxsize=1)
def sweepable() -> tuple:
    return tuple(
        rel
        for rel in tracked_files()
        if Path(rel).suffix.lower() not in BINARY_SUFFIXES and rel != SELF
    )


# ===========================================================================
# THE TWO REGISTERS
# ===========================================================================

#: Files that legitimately contain a shape-valid string as test scaffolding,
#: pinned BY COUNT rather than skipped.
#:
#: EMPTY TODAY, and the mechanism is kept anyway. It was populated for one
#: measurement: ``tests/test_path_leaks.py`` fed the path scrubber three
#: absolute paths carrying a real Windows account name, which a scrubber test
#: legitimately must. A concurrent wave renamed that account to ``TestUser``
#: while this file was being written, the entry went from correct to
#: over-declared inside an hour, and it was removed rather than left standing.
#: That is why :func:`test_no_plant_is_over_declared` exists: an over-declared
#: plant is a silent allowance for a hit nobody has looked at.
DECLARED_PLANTS = {}

#: Files with a GENUINE find that has not been scrubbed yet. These are NOT
#: allowlisted -- no value of theirs is ever added to a set above. The sweep
#: case for a registered path is marked ``xfail``, so the run reports it as an
#: expected FAILURE and never as a pass, and
#: :func:`test_the_leak_register_is_current` holds the register honest in both
#: directions: a registered file that stops firing is a STALE entry and goes
#: red, a registered file that fires MORE has a new leak hiding behind the
#: xfail and goes red.
#:
#: Values are never recorded here. Only the path, the reason, and counts.
#: EMPTY, and that is the intended steady state -- not a sign the mechanism is
#: unused. Four entries stood here on 2026-08-24 and all four were remediated
#: the same day, which is why the register is empty rather than absent:
#:
#:   _sweep/verdicts-2026-08-22.json          untracked (raw live capture)
#:   probing/analytics-report.md              untracked (whole directory)
#:   probing/recruiter-activity-deep-report.md  untracked (whole directory)
#:   tests/test_inbox_field_paths.py          scrubbed to synthetic values
#:
#: Adding an entry here is how you declare a KNOWN find you cannot fix yet. It
#: marks that file xfail, so the suite stays honest instead of green. Removing
#: an entry is how you declare it fixed -- and `test_the_leak_register_is_current`
#: goes red in BOTH directions, so neither move can be made silently.
UNREMEDIATED_LEAKS = {}


def leak_ids():
    """The parametrisation, with registered leaks marked xfail."""
    params = []
    for rel in sweepable():
        entry = UNREMEDIATED_LEAKS.get(rel)
        if entry is None:
            params.append(pytest.param(rel, id=rel))
        else:
            params.append(
                pytest.param(
                    rel,
                    id=rel,
                    marks=pytest.mark.xfail(
                        reason="UNREMEDIATED LEAK: %s" % entry[0]
                    ),
                )
            )
    return params


# ---------------------------------------------------------------------------
# 1. Every shape, over every tracked file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", leak_ids())
def test_no_tracked_file_carries_a_real_identifier(rel):
    """Nothing is skipped but this module. Plants are pinned; leaks xfail."""
    tally = counted(read_tracked(rel), rel)
    # Collected, not asserted per shape. Asserting inside the loop reports the
    # first offending shape and hides every later one in the same file, which
    # is how a second leak stays invisible behind a first.
    violations = [
        "%d unallowed %s hit(s), %d declared"
        % (count, name, DECLARED_PLANTS.get((rel, name), 0))
        for name, count in sorted(tally.items())
        if count > DECLARED_PLANTS.get((rel, name), 0)
    ]
    assert not violations, "%s: %s" % (rel, "; ".join(violations))


def test_the_sweep_actually_looked():
    """A parametrised sweep passes vacuously on an empty file list."""
    assert len(sweepable()) >= 300, len(sweepable())


def test_the_binary_skip_is_a_filter_and_not_an_accident():
    """The binary skip must be provable with ZERO binaries in the repo.

    This assertion used to read ``len(tracked_files()) > len(sweepable())`` --
    i.e. it proved the skip worked by requiring that some binary file happened
    to be tracked. On 2026-08-24 the repo's only tracked binaries (a committed
    ``.mypy_cache`` sqlite trio) were correctly untracked, and the check went
    red for a good change. A guard whose premise is "the defect is still here"
    is a guard that punishes the fix.

    So exercise the FILTER over a synthetic list instead. It holds at zero
    binaries and it would still hold at fifty.
    """
    names = ["a/b.py", "a/c.md", "a/d.png", "a/e.db", "a/f.whl", "a/g.json"]
    kept = [n for n in names if Path(n).suffix.lower() not in BINARY_SUFFIXES]
    assert kept == ["a/b.py", "a/c.md", "a/g.json"], kept
    # and the real list never contains a suffix we claim to skip
    assert not [
        rel for rel in sweepable() if Path(rel).suffix.lower() in BINARY_SUFFIXES
    ]


def test_exactly_one_path_is_skipped():
    """An exclusion is a promise that nothing in the file needs checking.

    That promise is what hid a real member URN at HEAD in a sibling repo, so
    this module is allowed exactly one -- itself, which cannot sweep itself
    without matching its own regex literals and planted controls. A second
    exclusion has to change this test to exist.
    """
    skipped = [
        rel
        for rel in tracked_files()
        if Path(rel).suffix.lower() not in BINARY_SUFFIXES and rel not in sweepable()
    ]
    # Stated for BOTH states, because this file is untracked until it is
    # committed and a test that only holds once it is tracked is a test that
    # was never run in the state it was written in.
    expected = [SELF] if SELF in tracked_files() else []
    assert skipped == expected, skipped
    assert SELF not in sweepable(), "the guard must never sweep itself"
    assert (REPO / SELF).exists()


def test_the_leak_register_is_current():
    """The hole a file-level xfail would otherwise open, closed from both ends.

    A registered file is EXPECTED to fail, so on its own an xfail would hide
    two opposite mistakes: a new identifier added to an already-dirty file, and
    an entry left standing after the data was scrubbed. This is the single
    place both go red, and its message says which one happened.

    Measured live while this guard was being written: a concurrent wave
    scrubbed two files between one run and the next. A register in a moving
    tree is only honest if it invalidates itself.
    """
    stale, grew = [], {}
    for rel, (_reason, expected) in UNREMEDIATED_LEAKS.items():
        assert rel in sweepable(), "%s is registered but not swept" % rel
        tally = counted(read_tracked(rel), rel)
        if not tally:
            stale.append(rel)
        for name, count in tally.items():
            if count > expected.get(name, 0):
                grew[(rel, name)] = (count, expected.get(name, 0))
    assert not stale, (
        "SCRUBBED, so the entry is stale -- delete these from "
        "UNREMEDIATED_LEAKS: %s" % stale
    )
    assert not grew, (
        "a NEW identifier was added to a file that is already xfailed, so it "
        "would otherwise be invisible -- {(path, shape): (found, declared)}: %s"
        % grew
    )


def test_no_plant_is_over_declared():
    """A plant declared higher than it fires is a silent allowance.

    This is not hypothetical: the one entry this register ever held went
    over-declared within the hour, when another wave renamed the account name
    its three planted paths carried. An over-declaration is indistinguishable
    from an allowlisted leak, so it goes red.
    """
    over = {
        (rel, name): (declared, counted(read_tracked(rel), rel).get(name, 0))
        for (rel, name), declared in DECLARED_PLANTS.items()
        if counted(read_tracked(rel), rel).get(name, 0) < declared
    }
    assert not over, "{(path, shape): (declared, actual)}: %s" % over


def test_the_leak_register_carries_a_reason_and_no_values():
    """A register entry must say WHY, and must never itself become the key."""
    for rel, entry in UNREMEDIATED_LEAKS.items():
        reason, expected = entry
        assert isinstance(reason, str) and len(reason) >= 20, rel
        assert all(name in SHAPE_NAMES for name in expected), (rel, expected)
        assert all(isinstance(n, int) and n > 0 for n in expected.values()), rel
        # The register is paths and counts. If a value ever gets pasted in
        # here, the register has become the pre-image it exists to remove.
        assert hits_in(reason, rel=SELF) == [], rel


# ---------------------------------------------------------------------------
# 2. Every check, SHOWN FAILING. A check never seen to fail certifies nothing.
# ---------------------------------------------------------------------------

#: Shape-valid violations, every one INVENTED. A control that needs a real
#: identifier has the same defect as the fixture it is guarding.
PLANTED = (
    ("email", "write to hiring@a-real-company.co.uk today"),
    ("phone", "he can be reached on 9123456789 any evening"),
    ("phone", "call +14155550142 for the desk"),
    ("user path", r"traceback from C:\Users\Jmorrissey\AppData\x.json"),
    ("user path", "traceback from C:/Users/Jmorrissey/AppData/x.json"),
    ("user path", "wrote /home/jmorrissey/.config/naukri/state.json"),
    ("hex32 id", 'row = {"mailId": "3f9c1d77b0e24a5581c6ff2049ab7e13"}'),
    ("hex64 id", "opaque = " + "9f" * 32),
    (
        "credential",
        "token = eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.QWxsSW52ZW50ZWQ",
    ),
    ("credential", "sessionid=7hQ2mNbVc9XpLw3ZrYt6JkSd8FgHjKl0Zx"),
    ("credential", "Authorization: 9zQmT4vXbNc2LpRw8YsK3JdF6GhAe1Uo5Ir"),
    ("credential", "Bearer 9zQmT4vXbNc2LpRw8YsK3JdF6GhAe1Uo5Ir7Ty"),
    ("credential", "aws_key = AKIA2J4KQ7ZR9WXMTB6L"),
    ("credential", "gh token ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"),
    ("credential", "-----BEGIN RSA PRIVATE KEY-----"),
)

#: The allowlisted equivalent of each plant above -- already synthetic, and it
#: must stay quiet. THE CONTROL FOR THE CONTROLS: without it every check above
#: would also pass on a guard that simply refuses everything, which would make
#: the allowlists meaningless and the suite unmaintainable.
BENIGN = (
    ("email", "write to nobody@example.com or team@evil.example.org"),
    ("email", "the fixture uses jane@hireco.com and hr@betacorp.com"),
    ("email", "profile shows q*****4@gmail.com, masked by the site"),
    ("phone", "the placeholder 9876543210 is not a person"),
    ("phone", 'the masked virtual number is "+910000000000"'),
    ("phone", "sha abcdefab9123456789cdefabcdefabcdefabcdef on master"),
    ("user path", r"scrubbed to C:\Users\runner\work\naukri"),
    ("user path", "mock returns /home/user/some/file"),
    ("user path", "the anchored/home/tail form keeps its separator"),
    ("hex64 id", "sha256 = " + "9f" * 32),
    ("credential", "Bearer (`MCP_SHARED_SECRET`) enables remote auth"),
    ("credential", 'sessionid="xxxxxxxxxxxxxxxxxxxxxxxxxx"'),
    ("credential", "Authorization: <redacted-by-the-scrubber-token>"),
)


@pytest.mark.parametrize(
    "shape, planted", PLANTED, ids=[f"{s}-{i}" for i, (s, _) in enumerate(PLANTED)]
)
def test_every_shape_can_actually_fail(shape, planted):
    """Each check, shown failing on an INVENTED violation of its own shape."""
    names = {name for name, _ in hits_in(planted, rel="scratch/planted.txt")}
    assert shape in names, (shape, names)


@pytest.mark.parametrize(
    "shape, benign", BENIGN, ids=[f"{s}-{i}" for i, (s, _) in enumerate(BENIGN)]
)
def test_the_synthetic_forms_are_allowed(shape, benign):
    """The allowlist has to actually allow, or the shape is unusable."""
    rel = "_audit/_slices/example-slice.md" if shape == "hex64 id" else "notes.md"
    names = {name for name, _ in hits_in(benign, rel=rel)}
    assert shape not in names, (shape, names, hits_in(benign, rel=rel))


def test_a_planted_violation_is_found_through_the_FILE_walker_too(tmp_path):
    """Not just the regex -- the read-and-count path a sweep failure travels.

    Planted in a TEMP file, never in a repo file: a guard that has to dirty the
    tree to prove itself cannot be trusted to have cleaned up after itself.
    """
    planted = tmp_path / "fixture_row.json"
    planted.write_text(
        '{"contact": "hiring@a-real-company.co.uk", "tel": "9123456789",\n'
        ' "mailId": "3f9c1d77b0e24a5581c6ff2049ab7e13"}\n',
        encoding="ascii",
    )
    tally = counted(planted.read_text(encoding="utf-8"), "fixtures/fixture_row.json")
    assert tally.get("email") == 1, tally
    assert tally.get("phone") == 1, tally
    assert tally.get("hex32 id") == 1, tally

    scrubbed = tmp_path / "scrubbed_row.json"
    scrubbed.write_text(
        '{"contact": "jane@hireco.com", "tel": "9876543210"}\n', encoding="ascii"
    )
    assert counted(scrubbed.read_text(encoding="utf-8"), "fixtures/scrubbed_row.json") == {}


# ---------------------------------------------------------------------------
# 3. The properties that let a check go quiet without a test changing
# ---------------------------------------------------------------------------


def test_the_windows_path_shape_matches_both_separator_forms():
    """THE DEGRADATION THIS FILE WAS WRITTEN WITH THE Write TOOL TO AVOID.

    A bash heredoc collapses a doubled backslash, so a character class written
    as backslash-backslash-slash arrives on disk as forward-slash-only. Every
    test still passes -- the forward-slash half works -- and the Windows half
    of the pattern is dead. Measured on this box, which is why this assertion
    exists rather than a comment.
    """
    backslash = r"C:\Users\Jmorrissey\AppData\Local\x.json"
    forwardslash = "C:/Users/Jmorrissey/AppData/Local/x.json"

    assert WINDOWS_USER_PATH.search(backslash), "the BACKSLASH half is dead"
    assert WINDOWS_USER_PATH.search(forwardslash), "the FORWARD-SLASH half is dead"
    assert WINDOWS_USER_PATH.search(backslash).group(1) == "Jmorrissey"
    assert WINDOWS_USER_PATH.search(forwardslash).group(1) == "Jmorrissey"

    # And the class really holds two distinct characters, so a collapse to
    # either one alone is caught rather than being a silent no-op.
    assert "\\" in WINDOWS_USER_PATH.pattern
    assert "/" in WINDOWS_USER_PATH.pattern


def test_a_digit_boundary_would_match_inside_a_git_sha_and_a_token_one_does_not():
    """THE MEASURED REASON FOR THE TOKEN BOUNDARY.

    Sweeping every tracked file, the digit-bounded phone pattern found 8 hits
    and the token-bounded one found 3; all 5 of the difference were 10-digit
    runs inside 40-hex commit SHAs. Driven here on an INVENTED 40-hex string so
    the control names no real commit.
    """
    sha = "abcdefab" + "9123456789" + "cdefabcdefabcdefabcdef"
    assert len(sha) == 40 and GIT_SHA.search(sha)

    digit_bounded = re.compile(r"(?<![\d.])(?:\+?91[-\s]?)?[6-9]\d{9}(?![\d.])")
    assert digit_bounded.search(sha), "the premise: a digit boundary DOES match"
    assert not PHONE_SHAPE.search(sha), "the fix: a token boundary does NOT"

    # The bare number, with nothing around it, must still fire under both --
    # otherwise the token boundary bought its precision by going blind.
    assert PHONE_SHAPE.search("call 9123456789 now")


def test_a_failure_never_prints_the_identifier():
    """A CI log is a publication channel."""
    value = "3f9c1d77b0e24a5581c6ff2049ab7e13"
    rendered = redact(value)
    assert value not in rendered
    assert rendered.startswith("3f") and rendered.endswith("chars>")
    assert len(rendered) < len(value)
    # And nothing the sweep emits carries a raw value either.
    for _name, shown in hits_in("mail 3f9c1d77b0e24a5581c6ff2049ab7e13 here"):
        assert value not in shown


def test_the_name_gap_is_stated_where_a_reader_will_see_it():
    """The most important sentence in this module, asserted so that an edit
    deleting it fails rather than quietly widening what green means."""
    doc = __doc__ or ""
    assert "NAMES HAVE NO SHAPE" in doc
    assert "does NOT mean" in doc and "no PII" in doc
    assert "third-party personal data" in doc


def test_this_guard_cannot_see_a_personal_name_and_says_so():
    """The docstring's claim, executed rather than asserted about itself.

    An invented recruiter name in an honest field is INVISIBLE to every shape
    in this module -- which is the entire reason the name gap has to be stated
    in prose for a human to read.
    """
    row = '{"recruiterName": "Meredith Vanstone", "designation": "HR Manager"}'
    assert hits_in(row, rel="naukri_server/tools/inbox.py") == []


def test_the_module_is_pure_ascii():
    """House rule, and a practical one: a smart quote in a regex is a bug that
    reads as a typo."""
    raw = Path(__file__).read_bytes()
    assert raw.decode("ascii", errors="strict")
