"""Census of naukri_server/healing/tier_registry.py.

Read-only. For every TierEntry in the registry, answer two independent
questions:

  1. Is the constant referenced by ANY production .py file under
     naukri_server/ (excluding healing/tier_registry.py itself and
     config.py, the definition site)?
  2. Is it referenced by the specific module named in its TierEntry?

Classification: OK / MISATTRIBUTED / ORPHAN / MODULE_MISSING.

Matching is a word-boundary regex (\\bNAME\\b) over raw file text, so
FOO_API does not match FOO_API_V2.

Writes _sweep/tier_registry_census.md. Modifies nothing else.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "naukri_server"
REGISTRY_FILE = PKG / "healing" / "tier_registry.py"
CONFIG_FILE = PKG / "config.py"
TESTS_DIR = REPO / "tests"
OUT_MD = REPO / "_sweep" / "tier_registry_census.md"

TIER_NAMES = {"TIER_T1": "T1", "TIER_T2": "T2", "TIER_T3": "T3"}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 1. Parse the registry with ast (handles single-line AND multi-line calls)
# ---------------------------------------------------------------------------


def parse_registry(path: Path):
    """Return (list of (const_name, tier, module_or_None, notes), dict names)."""
    tree = ast.parse(read_text(path), filename=str(path))
    entries = []
    seen_dicts = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AnnAssign, ast.Assign)):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not any(n.startswith("_T") and n.endswith("_ENTRIES") for n in names):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            raise SystemExit("SURPRISE: %s is not a dict literal" % names)
        seen_dicts.append(names[0])
        for key, val in zip(value.keys, value.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise SystemExit("SURPRISE: non-string key in %s" % names[0])
            if not (isinstance(val, ast.Call) and isinstance(val.func, ast.Name)
                    and val.func.id == "TierEntry"):
                raise SystemExit(
                    "SURPRISE: value for %s is not a TierEntry(...) call" % key.value
                )
            args = val.args
            if len(args) < 1:
                raise SystemExit("SURPRISE: TierEntry for %s has no args" % key.value)
            tier_node = args[0]
            if isinstance(tier_node, ast.Name) and tier_node.id in TIER_NAMES:
                tier = TIER_NAMES[tier_node.id]
            elif isinstance(tier_node, ast.Constant) and isinstance(tier_node.value, str):
                tier = tier_node.value
            else:
                raise SystemExit("SURPRISE: cannot resolve tier for %s" % key.value)
            module = None
            if len(args) >= 2:
                m = args[1]
                if isinstance(m, ast.Constant) and (
                    isinstance(m.value, str) or m.value is None
                ):
                    module = m.value
                else:
                    raise SystemExit(
                        "SURPRISE: cannot resolve module for %s" % key.value
                    )
            notes = ""
            if len(args) >= 3:
                n = args[2]
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    notes = n.value
            for kw in val.keywords:
                if kw.arg == "parser_module" and isinstance(kw.value, ast.Constant):
                    module = kw.value.value
                if kw.arg == "notes" and isinstance(kw.value, ast.Constant):
                    notes = kw.value.value
                if kw.arg == "tier" and isinstance(kw.value, ast.Name):
                    tier = TIER_NAMES.get(kw.value.id, kw.value.id)
            entries.append((key.value, tier, module, notes))
    return entries, seen_dicts


# ---------------------------------------------------------------------------
# 2. File corpora
# ---------------------------------------------------------------------------

EXCLUDED_PRODUCTION = {REGISTRY_FILE.resolve(), CONFIG_FILE.resolve()}


def production_files():
    out = []
    for p in sorted(PKG.rglob("*.py")):
        rp = p.resolve()
        if "__pycache__" in rp.parts:
            continue
        if rp in EXCLUDED_PRODUCTION:
            continue
        out.append(rp)
    return out


def test_files():
    if not TESTS_DIR.exists():
        return []
    return [
        p.resolve()
        for p in sorted(TESTS_DIR.rglob("*.py"))
        if "__pycache__" not in p.resolve().parts
    ]


PROD = production_files()
TESTS = test_files()
PROD_TEXT = {p: read_text(p) for p in PROD}
TEST_TEXT = {p: read_text(p) for p in TESTS}


def rel(p: Path) -> str:
    return str(p.relative_to(REPO)).replace("\\", "/")


# ---------------------------------------------------------------------------
# 3. The reference-search instrument (word-boundary)
# ---------------------------------------------------------------------------


def refs_in(name: str, corpus: dict) -> list:
    """Files in corpus whose raw text contains \\bNAME\\b."""
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    return [p for p, text in corpus.items() if pat.search(text)]


def module_to_path(dotted: str):
    """naukri_server.tools.foo -> absolute Path of .py (or package __init__)."""
    parts = dotted.split(".")
    base = REPO.joinpath(*parts)
    cand_mod = base.with_suffix(".py")
    if cand_mod.exists():
        return cand_mod.resolve()
    cand_pkg = base / "__init__.py"
    if cand_pkg.exists():
        return cand_pkg.resolve()
    return None


# ---------------------------------------------------------------------------
# 4. Controls
# ---------------------------------------------------------------------------

POS_CONTROL = "DASHBOARD_API"
NEG_CONTROL = "ZZZ_NOT_A_REAL_API"
BOUNDARY_PROBE = "DASHBOARD_AP"

pos_hits = refs_in(POS_CONTROL, PROD_TEXT)
neg_hits = refs_in(NEG_CONTROL, PROD_TEXT)
boundary_hits = refs_in(BOUNDARY_PROBE, PROD_TEXT)

pos_ok = any("services/profile_service.py" in rel(p) for p in pos_hits)

control_lines = []
control_lines.append(
    "POSITIVE CONTROL: search('%s') over %d production files" % (POS_CONTROL, len(PROD))
)
control_lines.append("  hits = %d" % len(pos_hits))
for p in pos_hits:
    control_lines.append("    %s" % rel(p))
control_lines.append(
    "  expected: >=1, including naukri_server/services/profile_service.py"
)
control_lines.append("  RESULT: %s" % ("PASS" if pos_ok else "FAIL"))
control_lines.append("")
control_lines.append(
    "NEGATIVE CONTROL: search('%s') over %d production files" % (NEG_CONTROL, len(PROD))
)
control_lines.append("  hits = %d" % len(neg_hits))
control_lines.append("  expected: 0")
control_lines.append("  RESULT: %s" % ("PASS" if not neg_hits else "FAIL"))
control_lines.append("")
control_lines.append(
    "WORD-BOUNDARY PROBE: search('%s') (a strict prefix of '%s')"
    % (BOUNDARY_PROBE, POS_CONTROL)
)
control_lines.append("  hits = %d" % len(boundary_hits))
control_lines.append(
    "  expected: 0 -- proves \\bNAME\\b does not match inside a longer identifier"
)
control_lines.append("  RESULT: %s" % ("PASS" if not boundary_hits else "FAIL"))

controls_ok = pos_ok and not neg_hits and not boundary_hits

# ---------------------------------------------------------------------------
# 5. Reconciliation
# ---------------------------------------------------------------------------

entries, seen_dicts = parse_registry(REGISTRY_FILE)
raw = read_text(REGISTRY_FILE)
grep_count = len(re.findall(r"TierEntry\(", raw))
parsed_count = len(entries)

names = [e[0] for e in entries]
dupes = sorted({n for n in names if names.count(n) > 1})

# ---------------------------------------------------------------------------
# 6. Classify
# ---------------------------------------------------------------------------

rows = []
for const, tier, module, notes in entries:
    prod_hits = refs_in(const, PROD_TEXT)
    tst_hits = refs_in(const, TEST_TEXT)
    mod_path = module_to_path(module) if module else None
    if module and mod_path is None:
        klass = "MODULE_MISSING"
    elif not prod_hits:
        klass = "ORPHAN"
    elif mod_path is not None and mod_path in prod_hits:
        klass = "OK"
    else:
        klass = "MISATTRIBUTED"
    rows.append(
        {
            "const": const,
            "tier": tier,
            "module": module,
            "mod_path": rel(mod_path) if mod_path else None,
            "class": klass,
            "prod": [rel(p) for p in prod_hits],
            "tests": [rel(p) for p in tst_hits],
        }
    )

counts = {"OK": 0, "MISATTRIBUTED": 0, "ORPHAN": 0, "MODULE_MISSING": 0}
for r in rows:
    counts[r["class"]] += 1

# ---------------------------------------------------------------------------
# 7. Emit markdown (ASCII only)
# ---------------------------------------------------------------------------

L = []
L.append("# tier_registry census")
L.append("")
L.append(
    "Generated by `_sweep/tier_registry_census.py` (read-only; no tracked file modified)."
)
L.append("")
L.append("Registry under census: `naukri_server/healing/tier_registry.py`")
L.append("")
L.append("Production corpus: every `.py` under `naukri_server/`, EXCLUDING")
L.append("`naukri_server/healing/tier_registry.py` (the registry itself) and")
L.append("`naukri_server/config.py` (the definition site), and excluding `__pycache__`.")
L.append(
    "Count: **%d files**. Test corpus: every `.py` under `tests/` -- **%d files**."
    % (len(PROD), len(TESTS))
)
L.append("")
L.append("Matching is a **word-boundary regex** `\\bCONST_NAME\\b` over raw file text,")
L.append(
    "so `FOO_API` cannot match `FOO_API_V2`. Raw text means a mention in a comment or"
)
L.append(
    "string also counts as a reference -- that is the conservative direction: it makes"
)
L.append("an ORPHAN verdict harder to reach, never easier.")
L.append("")
L.append("## 1. Control results (verbatim)")
L.append("")
L.append("```")
L.extend(control_lines)
L.append("```")
L.append("")
L.append(
    "Instrument status: **%s**"
    % ("ALL CONTROLS PASS" if controls_ok else "CONTROL FAILURE")
)
L.append("")
L.append("## 2. Entry-count reconciliation")
L.append("")
L.append("| source | count |")
L.append("|---|---|")
L.append(
    "| ast parse of the `_T*_ENTRIES` dicts (%s) | **%d** |"
    % (", ".join("`%s`" % d for d in seen_dicts), parsed_count)
)
L.append("| `grep -c 'TierEntry('` over the same file | **%d** |" % grep_count)
L.append("")
L.append(
    "Reconciliation: **%s**"
    % ("MATCH" if parsed_count == grep_count else "MISMATCH -- PARSER IS WRONG")
)
if dupes:
    L.append("")
    L.append("Duplicate constant names across dicts: %s" % ", ".join(dupes))
L.append("")
L.append("## 3. Summary counts")
L.append("")
L.append("| classification | count |")
L.append("|---|---|")
for k in ("OK", "MISATTRIBUTED", "ORPHAN", "MODULE_MISSING"):
    L.append("| %s | %d |" % (k, counts[k]))
L.append("| **TOTAL** | **%d** |" % len(rows))
L.append("")
by_tier = {}
for r in rows:
    by_tier.setdefault(
        r["tier"],
        {"OK": 0, "MISATTRIBUTED": 0, "ORPHAN": 0, "MODULE_MISSING": 0, "n": 0},
    )
    by_tier[r["tier"]][r["class"]] += 1
    by_tier[r["tier"]]["n"] += 1
L.append("Broken out by tier:")
L.append("")
L.append("| tier | total | OK | MISATTRIBUTED | ORPHAN | MODULE_MISSING |")
L.append("|---|---|---|---|---|---|")
for t in sorted(by_tier):
    b = by_tier[t]
    L.append(
        "| %s | %d | %d | %d | %d | %d |"
        % (t, b["n"], b["OK"], b["MISATTRIBUTED"], b["ORPHAN"], b["MODULE_MISSING"])
    )
L.append("")
L.append("## 4. Every non-OK entry")
L.append("")
non_ok = [r for r in rows if r["class"] != "OK"]
if not non_ok:
    L.append("_None._")
else:
    L.append(
        "| constant | tier | claimed module | classification | actual production "
        "referencing file(s) | referenced by tests? |"
    )
    L.append("|---|---|---|---|---|---|")
    order = {"MODULE_MISSING": 0, "ORPHAN": 1, "MISATTRIBUTED": 2}
    for r in sorted(non_ok, key=lambda r: (order[r["class"]], r["tier"], r["const"])):
        actual = "<br>".join("`%s`" % f for f in r["prod"]) if r["prod"] else "_(none)_"
        if r["tests"]:
            tref = "yes (%d): %s" % (
                len(r["tests"]),
                "<br>".join("`%s`" % f for f in r["tests"]),
            )
        else:
            tref = "no"
        L.append(
            "| `%s` | %s | `%s` | **%s** | %s | %s |"
            % (r["const"], r["tier"], r["module"], r["class"], actual, tref)
        )
L.append("")
L.append("Note on the ORPHAN verdict and the repo's own decoy check.")
L.append("`tests/test_no_decoys.py::TestEveryConfigConstantHasAReader` asserts every")
L.append("`config.py` constant is read somewhere in the package, using the same")
L.append("word-boundary regex this census uses -- but its `_reader_count(name,")
L.append("exclude=CONFIG_PY)` excludes ONLY `config.py`, and its `_production_files()`")
L.append("includes `naukri_server/healing/tier_registry.py`. So the registry entry")
L.append("itself counts as a reader there. Any constant listed in the tier registry")
L.append("satisfies that test even when nothing else in the package touches it, which")
L.append("is why `AB_REVIEW_FILTERS_API` is green under the standing check and ORPHAN")
L.append("here. This census excludes the registry, so it is the stricter instrument;")
L.append("the two do not disagree about the facts, only about what counts as a reader.")
L.append("")
L.append("## 5. OK entries (complete list)")
L.append("")
ok_names = [r["const"] for r in rows if r["class"] == "OK"]
L.append("%d entries:" % len(ok_names))
L.append("")
L.append(", ".join(ok_names) if ok_names else "_(none)_")
L.append("")

text = "\n".join(L) + "\n"
non_ascii = [(i, ch) for i, ch in enumerate(text) if ord(ch) > 127]
if non_ascii:
    raise SystemExit("SURPRISE: non-ASCII in output: %r" % non_ascii[:5])
OUT_MD.write_text(text, encoding="ascii", newline="\n")

# ---------------------------------------------------------------------------
# 8. Console summary
# ---------------------------------------------------------------------------

print("\n".join(control_lines))
print("")
print("parsed entries    : %d" % parsed_count)
print("grep TierEntry(   : %d" % grep_count)
print("reconciliation    : %s" % ("MATCH" if parsed_count == grep_count else "MISMATCH"))
print("production files  : %d" % len(PROD))
print("test files        : %d" % len(TESTS))
print("")
for k in ("OK", "MISATTRIBUTED", "ORPHAN", "MODULE_MISSING"):
    print("%-15s: %d" % (k, counts[k]))
print("%-15s: %d" % ("TOTAL", len(rows)))
print("")
print("ORPHAN         : %s" % ", ".join(r["const"] for r in rows if r["class"] == "ORPHAN"))
print(
    "MISATTRIBUTED  : %s"
    % ", ".join(r["const"] for r in rows if r["class"] == "MISATTRIBUTED")
)
print(
    "MODULE_MISSING : %s"
    % ", ".join(r["const"] for r in rows if r["class"] == "MODULE_MISSING")
)
print("")
print("wrote %s" % OUT_MD)
if not controls_ok or parsed_count != grep_count:
    sys.exit(2)
