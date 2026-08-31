"""Which dashboard keys does the reader NAME that the endpoint never SENDS?

The four fields this wave was pointed at (`unreadPowerNvite`, `totalPowerNvite`,
`unreadMostRelevantMail`, `mrt`) were unreachable because the request was
narrowed. Once the request is bare, they arrive. The obvious next question --
the one nobody had asked -- is how many OTHER keys `_get_dashboard` names that
the live endpoint does not send under ANY request shape.

Method: AST-walk `_get_dashboard` for every `safe_get(db, "k1", "k2", ...)`
positional key literal, then intersect with the key set of a REAL captured bare
payload (`_sweep/dashboard-payloads.json`, captured 2026-08-31).

An alias chain is UNREACHABLE only when NONE of its alternatives is a real key:
`safe_get(db, "name", "fullName")` resolves, because `name` is real.

Read-only. No network. Run:
    venv\\Scripts\\python.exe _sweep\\dashboard_reader_census.py
"""

import ast
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "naukri_server" / "services" / "profile_service.py"
PAYLOADS = Path(__file__).resolve().parent / "dashboard-payloads.json"
FUNC = "_get_dashboard"


def read_keys(func_node):
    """Every safe_get(db, ...) alias chain in *func_node*, in source order."""
    chains = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "safe_get"):
            continue
        if not node.args:
            continue
        target = node.args[0]
        # only reads straight off `db`; nested reads (safe_get(safe_get(db,...)))
        # are a different question and are reported separately
        if not (isinstance(target, ast.Name) and target.id == "db"):
            continue
        keys = [a.value for a in node.args[1:]
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if keys:
            chains.append((node.lineno, tuple(keys)))
    return chains


def main():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == FUNC),
        None,
    )
    if func is None:
        raise SystemExit(f"ABORT: {FUNC} not found in {SRC}")

    chains = read_keys(func)
    real = set(json.loads(PAYLOADS.read_text(encoding="utf-8"))["bare"]["keys"])

    # ---- CONTROLS. An absence-based census with no working instrument is noise.
    print("=== CONTROLS ===")
    ctl_pos = [c for c in chains if c[1][0] == "profileViewCount"]
    print(f"positive: a chain known to resolve ('profileViewCount') was extracted: "
          f"{'PASS' if ctl_pos else 'FAIL'} ({len(ctl_pos)} found)")
    print(f"positive: 'profileViewCount' in captured payload: "
          f"{'PASS' if 'profileViewCount' in real else 'FAIL'}")
    print(f"negative: a name that cannot exist is absent from the payload: "
          f"{'PASS' if 'zzzNotARealKey' not in real else 'FAIL'}")
    print(f"scan is non-vacuous: {len(chains)} chains extracted, "
          f"{len(real)} real keys -- {'PASS' if len(chains) > 20 and len(real) > 20 else 'FAIL'}")

    resolvable = [(ln, ks) for ln, ks in chains if any(k in real for k in ks)]
    unreachable = [(ln, ks) for ln, ks in chains if not any(k in real for k in ks)]

    print(f"\n=== CENSUS: safe_get(db, ...) chains in {FUNC} ===")
    print(f"total chains      : {len(chains)}")
    print(f"resolvable        : {len(resolvable)}")
    print(f"UNREACHABLE (always None, silently): {len(unreachable)}")

    print("\n--- unreachable chains (line: names tried) ---")
    for ln, ks in unreachable:
        print(f"  {ln:>4}: {', '.join(ks)}")

    print("\n--- every unreachable NAME, deduped ---")
    names = sorted({k for _, ks in unreachable for k in ks})
    print(f"  {len(names)} names: {names}")

    print("\n--- real keys NOTHING in this function reads ---")
    named = {k for _, ks in chains for k in ks}
    print(f"  {sorted(real - named)}")


if __name__ == "__main__":
    main()
