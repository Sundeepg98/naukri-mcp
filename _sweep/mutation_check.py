"""Show each new guard FAILING at the mutation it claims to catch.

A check that cannot fail certifies nothing. This applies a one-line mutation to
a source or test file, runs the single guard that should notice, restores the
file, and reports RED (good) or SURVIVED (the guard is decorative and must be
deleted).

Run:  venv\\Scripts\\python.exe _sweep\\mutation_check.py
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "venv" / "Scripts" / "python.exe"

SERVICE = REPO / "naukri_server" / "services" / "profile_service.py"
SHAPE_TEST = REPO / "tests" / "test_dashboard_request_shape.py"
REG_TEST = REPO / "tests" / "test_tier_registry.py"

# (label, file, find, replace, test node id)
MUTATIONS = [
    (
        "rename the source key the four fields read",
        SERVICE,
        'safe_get(db, "unreadPowerNvite", field_name="unread_invites"',
        'safe_get(db, "unreadPowerNviteXX", field_name="unread_invites"',
        "tests/test_dashboard_request_shape.py::TestTheRequestIsBare"
        "::test_the_four_repaired_fields_resolve_from_a_real_payload",
    ),
    # A plain rename of the payload local -- an ordinary refactor -- silently
    # zeroes the AST extractor, which would make the unreachable-read census
    # pass over nothing at all. This mutation is why the vacuity control exists.
    (
        "rename the payload local, zeroing the AST extractor",
        SERVICE,
        'safe_get(db, ',
        'safe_get(dashx, ',
        "tests/test_dashboard_request_shape.py::TestEveryDashboardReadIsReachable"
        "::test_the_census_is_not_vacuous",
    ),
    (
        "add a dashboard read for a key that does not exist",
        SERVICE,
        '        result["profile_quality"] = safe_get(db, "profileQuality")',
        '        result["profile_quality"] = safe_get(db, "profileQuality")\n'
        '        result["zz"] = safe_get(db, "zzzInventedDashboardKey")',
        "tests/test_dashboard_request_shape.py::TestEveryDashboardReadIsReachable"
        "::test_no_new_unreachable_dashboard_read",
    ),
    (
        "ledger a repaired field instead of fixing it",
        SHAPE_TEST,
        '        "unreadMailCount", "userId", "viewsTrend",',
        '        "unreadMailCount", "userId", "viewsTrend", "mrt",',
        "tests/test_dashboard_request_shape.py::TestEveryDashboardReadIsReachable"
        "::test_the_repaired_four_are_no_longer_unreachable",
    ),
    # Reintroducing the original defect must redden BOTH request-shape guards,
    # which sit on two different import paths (services.profile_service and
    # tools.profile).
    (
        "reintroduce the narrowing parameter (services path)",
        SERVICE,
        "        data = await api_client.get(DASHBOARD_API)",
        '        data = await api_client.get(DASHBOARD_API, params={"properties": "userDetails,lookupData"})',
        "tests/test_dashboard_request_shape.py::TestTheRequestIsBare"
        "::test_no_properties_parameter_is_sent",
    ),
    (
        "reintroduce the narrowing parameter (tools path)",
        SERVICE,
        "        data = await api_client.get(DASHBOARD_API)",
        '        data = await api_client.get(DASHBOARD_API, params={"properties": "userDetails,lookupData"})',
        "tests/test_profile_deep.py::TestDashboardSelectiveProperties"
        "::test_dashboard_sends_no_properties_param",
    ),
    (
        "blind the registry reference scan to every file",
        REG_TEST,
        "    return [\n        p for p in sorted(_PKG_ROOT.rglob(\"*.py\"))",
        "    return [\n        p for p in []",
        "tests/test_tier_registry.py::test_the_reference_scan_works",
    ),
]


def run(node):
    r = subprocess.run(
        [str(PY), "-m", "pytest", node, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
    )
    return r.returncode, r.stdout


def main():
    failures = []
    for label, path, find, repl, node in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if find not in original:
            print(f"[SETUP FAIL] {label}: anchor not found in {path.name}")
            failures.append(label)
            continue
        try:
            path.write_text(original.replace(find, repl), encoding="utf-8")
            code, out = run(node)
        finally:
            path.write_text(original, encoding="utf-8")

        verdict = "RED (good)" if code != 0 else "SURVIVED -- DELETE THIS GUARD"
        print(f"\n=== {label}")
        print(f"    node   : {node.split('::')[-1]}")
        print(f"    verdict: {verdict}")
        for line in out.splitlines():
            if line.startswith("E ") or "assert" in line[:12] or "passed" in line or "failed" in line:
                print(f"      {line.strip()[:180]}")
        if code == 0:
            failures.append(label)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} guard(s) survived their mutation: {failures}")
        sys.exit(1)
    print(f"all {len(MUTATIONS)} guards went RED at the mutation they claim to catch")


if __name__ == "__main__":
    main()
