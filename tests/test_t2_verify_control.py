"""CONTROL for the t2_verify_pending SCHEDULED TASK.

WHY THIS FILE EXISTS
--------------------
`_task_t2_verify_pending` has run 1,830 times on the operator's live server
between 2026-08-20 and 2026-08-30 and returned the SAME bytes every single
time::

    {"status": "completed", "confirmed": 0, "reverted": 0, "revert_failed": 0}

The `auto_fix_pending` table it walks has never held a row, so the task has
never been OBSERVED returning anything else. A probe that has only ever been
seen returning zeros is indistinguishable, from the outside, from a probe that
is dead. This repo's standing rule is that an instrument enters the register
only once it has been SHOWN FAILING at the mutation it catches; the mirror of
that rule is that a probe must be SHOWN SUCCEEDING before its zeros mean
anything.

WHAT IS UNDER TEST
------------------
`naukri_server.scheduler_tasks._task_t2_verify_pending` ITSELF -- the exact
coroutine the scheduler invokes -- not a helper it happens to call.
`tests/test_healing_integration.py` already drives
`t2_autofix.verify_due_pending_rows` directly; that proves the LIBRARY layer.
This file proves the TASK layer: that the task passes the right repo root,
passes a revalidation callback that can actually return True, and hands the
counts back out to the scheduler instead of swallowing them.

THE THREE CASES
---------------
1. CONFIRM  -- due row + endpoint re-validates healthy  -> confirmed == 1
               (the case production has never once produced)
2. REVERT   -- due row + endpoint still drifts          -> reverted   == 1
               (the safety half; the whole reason the task exists)
3. NOT DUE  -- row whose verify_at is in the FUTURE     -> all zeros,
               and then the SAME row, once its verify_at is moved into the
               past, confirms. Without this pairing all three cases could
               pass on a task that is broken in one direction.

WHAT IS PATCHED, AND WHAT DELIBERATELY IS NOT
---------------------------------------------
Exactly three redirections, all of them boundaries, none of them logic:

  * `naukri_server.config._PACKAGE_ROOT` -> a throwaway `git init` repo, so
    the healer's commits and reverts never touch the real checkout.
  * `naukri_server.database.DB_PATH`     -> a tmp sqlite file (tighter than
    conftest's session-scoped isolation, so sibling tests cannot leak a
    pending row into these counts).
  * `naukri_server.interfaces.api_client.get` -> the HTTP boundary. This is
    the ONLY seam inside `_revalidate_endpoint`. Everything else in that
    function stays live: the real `naukri_server.config` constant lookup, the
    real module-singleton `DriftDetector`, a real snapshot recorded through
    the detector's real public API, and the real `check_drift` comparison. So
    every conservative branch (constant missing, no snapshot, fetch raised,
    any exception) is still armed and would still return False. CONFIRM and
    REVERT differ ONLY in the JSON bytes the network hands back: identical
    patching, opposite verdicts, produced by real logic.

The healer circuit breaker is enabled through `_set_state_path_for_tests`, so
this file never writes the operator's `healing_state.json`.

PURE: no network, no browser, no live DB. Real `git` subprocesses against a
tmp_path repo only.
"""

import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server import config as real_config
from naukri_server import scheduler_tasks
from naukri_server.healing import circuit, snapshot, t2_autofix
from naukri_server.probes.drift_detector import detector as live_detector


# The T2 constant these controls drive. It must satisfy three things at once:
#   - tier_registry maps it to T2 (else apply_t2_fix refuses it),
#   - it exists in the REAL naukri_server.config (else _revalidate_endpoint
#     takes its "config constant missing" branch and returns False),
#   - the fake repo carries the parser module its field fix targets.
T2_CONSTANT = "SEARCH_API"

# Production drift on the live server was drift_type='field' on all 324
# EndpointDriftDetected events, so these controls exercise the field path.
CANONICAL_KEY = "title"
NEW_ALIAS = "newJobTitle"

# The response shape the detector snapshots as the baseline.
HEALTHY_PAYLOAD = {
    "noOfJobs": 12,
    "jobDetails": [
        {"jobId": "010101", "title": "Backend Engineer", "companyName": "Acme"},
    ],
}

# Same endpoint, one field GONE. Fed through the real check_drift against the
# baseline above this produces a real DriftReport (severity "removed").
DRIFTED_PAYLOAD = {
    "noOfJobs": 12,
    "jobDetails": [
        {"jobId": "010101", "companyName": "Acme"},
    ],
}

# The exact bytes the live server has returned 1,830 times running.
PRODUCTION_ZEROS = {
    "status": "completed",
    "confirmed": 0,
    "reverted": 0,
    "revert_failed": 0,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path):
    """A real git repo with a seeded fake naukri_server tree.

    Same shape as the fixture in tests/test_healing_t2_autofix.py -- the healer
    runs real `git add` / `git commit` / `git revert`, so a real repo is the
    only honest substrate.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@local"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test-user"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"], check=True)

    pkg = repo / "naukri_server"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "config.py").write_text(
        '"""Fake config."""\n'
        'SEARCH_API = "/jobapi/v3/search"\n'
        'PROFILE_API = "/profile/v2"\n',
        encoding="utf-8",
    )

    services = pkg / "services"
    services.mkdir()
    (services / "__init__.py").write_text("", encoding="utf-8")
    (services / "search_service.py").write_text(
        '"""Fake search service."""\n'
        'FIELD_ALIASES = {\n'
        '    "title": ["title", "jobTitle"],\n'
        '    "company": ["company", "companyName"],\n'
        '}\n',
        encoding="utf-8",
    )

    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial", "--no-gpg-sign"],
        check=True,
    )
    yield repo


@pytest.fixture(autouse=True)
def healer_enabled_off_disk(tmp_path):
    """Enable the healer WITHOUT writing the operator's healing_state.json.

    `circuit.enable()` persists to DATA_DIR/healing_state.json, which is the
    live checkout. `_set_state_path_for_tests` is the module's own supported
    escape hatch; the original path is restored on teardown so no other test
    inherits the redirect.
    """
    original_path = circuit._STATE_PATH
    circuit._set_state_path_for_tests(tmp_path / "healing_state.json")
    circuit._reset_for_tests()
    circuit.enable()
    try:
        yield
    finally:
        circuit._reset_for_tests()
        circuit._set_state_path_for_tests(original_path)


@pytest.fixture
def snapshotted_endpoint():
    """Record a REAL baseline schema in the REAL module-singleton detector.

    This is what makes the CONFIRM case honest. `_revalidate_endpoint` bails
    out with False when `has_snapshot(path)` is false, so a control that
    skipped this step could never confirm anything -- and a control that faked
    `check_drift` would not be testing the detector at all. We record a genuine
    snapshot through the detector's own public API and let the real comparison
    decide.

    Yields the endpoint path (read off the real config, exactly as
    `_revalidate_endpoint` reads it) and clears the snapshot afterwards so the
    process-wide singleton does not bleed into other tests.
    """
    path = getattr(real_config, T2_CONSTANT)
    live_detector.snapshot_endpoint(path, HEALTHY_PAYLOAD)
    try:
        yield path
    finally:
        live_detector.clear(path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_scheduled_task(fake_repo, payload):
    """Invoke the REAL scheduled coroutine and return (result, network_mock).

    Only two things are redirected here: the repo root the task resolves from
    config, and the HTTP boundary inside `_revalidate_endpoint`. The task, the
    callback it passes, `verify_due_pending_rows`, `verify_pending_row`, the
    drift detector and every git call are the production objects.
    """
    fake_get = AsyncMock(return_value=payload)
    with patch("naukri_server.config._PACKAGE_ROOT", fake_repo), \
         patch("naukri_server.interfaces.api_client.get", new=fake_get):
        result = await scheduler_tasks._task_t2_verify_pending()
    return result, fake_get


async def _apply_one_pending_fix(fake_repo, *, verify_delay_seconds):
    """Create ONE real pending T2 row via the real apply path.

    `verify_delay_seconds` is the module's own documented test override. A
    NEGATIVE value is how these controls simulate "the ten minutes have already
    elapsed" without patching the clock: the row lands with a verify_at that is
    genuinely in the past, and the task's real `datetime.now()` cutoff and the
    real `verify_at <= ?` SQL filter still decide dueness on their own.
    """
    outcome = await t2_autofix.apply_t2_fix(
        fake_repo,
        T2_CONSTANT,
        drift_type="field",
        canonical_key=CANONICAL_KEY,
        new_alias=NEW_ALIAS,
        verify_delay_seconds=verify_delay_seconds,
        skip_emit=True,
    )
    assert outcome.applied is True, f"setup failed, cannot test the task: {outcome}"
    return outcome


def _parser_source(fake_repo):
    return (fake_repo / "naukri_server" / "services" / "search_service.py").read_text(
        encoding="utf-8"
    )


async def _row(row_id):
    from naukri_server.database import get_auto_fix_pending
    return await get_auto_fix_pending(row_id)


async def _force_row_due(row_id):
    """Move one row's verify_at into the past, in the TEST database only.

    Used by the NOT-YET-DUE control's second phase so the two phases differ in
    exactly one variable: the timestamp. Everything else -- the row, the repo,
    the commit, the payload, the task -- is identical between them.
    """
    from naukri_server.database import get_db
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    db = await get_db()
    try:
        await db.execute(
            "UPDATE auto_fix_pending SET verify_at = ? WHERE id = ?", (past, row_id),
        )
        await db.commit()
    finally:
        await db.close()


# ===========================================================================
# CASE 1 -- CONFIRM. The outcome production has never once produced.
# ===========================================================================


@pytest.mark.asyncio
async def test_control_task_returns_confirmed_1_when_endpoint_revalidates_healthy(
    fake_repo, tmp_path, snapshotted_endpoint,
):
    """A due pending row whose endpoint is healthy must come back confirmed==1.

    In 1,830 production runs this number has never been anything but 0. This is
    the control that says a 1 is reachable at all, through the real task.
    """
    db_path = tmp_path / "control.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()

        applied = await _apply_one_pending_fix(fake_repo, verify_delay_seconds=-60)
        head_after_fix = snapshot.head_sha(fake_repo)

        result, fake_get = await _run_scheduled_task(fake_repo, HEALTHY_PAYLOAD)

        row = await _row(applied.pending_row_id)

    # The task's own return value -- this is what the scheduler stores.
    assert result["status"] == "completed"
    assert result["confirmed"] == 1, f"expected confirmed==1, task returned {result}"
    assert result["reverted"] == 0
    assert result["revert_failed"] == 0

    # It actually revalidated: the real callback reached the network boundary
    # with the real config path, and the real detector said "no drift".
    fake_get.assert_awaited_once_with(snapshotted_endpoint)

    # And it persisted the verdict rather than only reporting it.
    assert row["status"] == "confirmed"

    # A confirm must NOT revert: HEAD is untouched and the fix is still there.
    assert snapshot.head_sha(fake_repo) == head_after_fix
    assert NEW_ALIAS in _parser_source(fake_repo)


# ===========================================================================
# CASE 2 -- REVERT. The safety half; the reason the task exists at all.
# ===========================================================================


@pytest.mark.asyncio
async def test_control_task_returns_reverted_1_when_endpoint_still_drifts(
    fake_repo, tmp_path, snapshotted_endpoint,
):
    """A due pending row whose endpoint STILL drifts must come back reverted==1,
    with the healer's commit actually undone in the repo.

    Note the setup is identical to the CONFIRM control except for the JSON the
    network returns. The verdict is produced by the real drift detector over
    real bytes, not by the patch.
    """
    db_path = tmp_path / "control.db"
    original_source = _parser_source(fake_repo)

    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()

        applied = await _apply_one_pending_fix(fake_repo, verify_delay_seconds=-60)
        assert NEW_ALIAS in _parser_source(fake_repo)  # the fix did land first

        result, fake_get = await _run_scheduled_task(fake_repo, DRIFTED_PAYLOAD)

        row = await _row(applied.pending_row_id)

    assert result["status"] == "completed"
    assert result["reverted"] == 1, f"expected reverted==1, task returned {result}"
    assert result["confirmed"] == 0
    assert result["revert_failed"] == 0

    fake_get.assert_awaited_once_with(snapshotted_endpoint)
    assert row["status"] == "reverted"

    # The safety contract in the only form that counts: the edit is GONE and a
    # revert commit sits on top of the fix commit.
    assert NEW_ALIAS not in _parser_source(fake_repo)
    assert _parser_source(fake_repo) == original_source
    assert snapshot.head_sha(fake_repo) != applied.commit_sha


# ===========================================================================
# CASE 3 -- NOT YET DUE. Proves the zeros are a verdict, not a no-op.
# ===========================================================================


@pytest.mark.asyncio
async def test_control_task_returns_zeros_while_not_due_then_confirms_once_due(
    fake_repo, tmp_path, snapshotted_endpoint,
):
    """Two phases, one variable.

    PHASE 1: a pending row whose verify_at is the production VERIFY_DELAY_SECONDS
    (600s) in the future. The task must return the exact production zeros AND
    must not have revalidated anything -- the dueness filter, not a swallowed
    count, is what produced them.

    PHASE 2: the SAME row, SAME repo, SAME commit, SAME healthy payload, SAME
    task -- only verify_at moved into the past. Now it confirms.

    Phase 2 is what makes phase 1 mean something. Zeros from a task that always
    returns zeros and zeros from a task correctly reporting "nothing due" look
    identical from the outside; only the pair tells them apart.
    """
    db_path = tmp_path / "control.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()

        # Production delay, not a test override: the row is due in 10 minutes.
        applied = await _apply_one_pending_fix(
            fake_repo, verify_delay_seconds=t2_autofix.VERIFY_DELAY_SECONDS,
        )
        head_after_fix = snapshot.head_sha(fake_repo)

        # -- PHASE 1 -------------------------------------------------------
        result_early, fake_get_early = await _run_scheduled_task(
            fake_repo, HEALTHY_PAYLOAD,
        )
        row_early = await _row(applied.pending_row_id)

        assert result_early == PRODUCTION_ZEROS, (
            "a not-yet-due row must produce exactly the bytes production has "
            f"returned 1,830 times, got {result_early}"
        )
        # Never revalidated -- so the zeros came from the dueness filter.
        fake_get_early.assert_not_awaited()
        # And the row is untouched, still waiting its turn.
        assert row_early["status"] == "pending"
        assert snapshot.head_sha(fake_repo) == head_after_fix
        assert NEW_ALIAS in _parser_source(fake_repo)

        # -- PHASE 2 -------------------------------------------------------
        # Change exactly one thing: the clock the row is measured against.
        await _force_row_due(applied.pending_row_id)

        result_due, fake_get_due = await _run_scheduled_task(
            fake_repo, HEALTHY_PAYLOAD,
        )
        row_due = await _row(applied.pending_row_id)

    assert result_due["confirmed"] == 1, (
        "the same row, once due, must be processed -- otherwise phase 1's "
        f"zeros proved nothing, got {result_due}"
    )
    assert result_due["reverted"] == 0
    assert result_due["revert_failed"] == 0
    fake_get_due.assert_awaited_once_with(snapshotted_endpoint)
    assert row_due["status"] == "confirmed"
