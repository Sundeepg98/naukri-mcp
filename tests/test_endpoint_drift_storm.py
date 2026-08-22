"""EndpointDrift banked the same fact once per probe run.

Third instance of the same defect class, after `ReminderDue` (2026-08-21,
1,084 rows / 50 facts) and `ApplicationStale` (2026-08-21, 85 rows / 17 facts).
Measured on his DB read-only at 2026-08-22T17:00Z:

    notifications                    212 rows, 198 undelivered
    EndpointDrift                     81 rows, ALL 81 undelivered  (41%)
      DASHBOARD_API        added/field  x33   08-21T07:36 .. 08-21T15:38
      APPLIED_JOBS_API     added/field  x25   08-21T09:37 .. 08-21T15:38
      RECOMMENDED_JOBS_API removed/field x23  08-22T07:40 .. 08-22T16:41

THREE distinct facts, 81 rows. It was 58 rows / 2 facts when first flagged
yesterday; `RECOMMENDED_JOBS_API` began drifting at 07:40 today and was still
minting ~4 rows/hour when this was written.

ONLY HALF OF THE PRIOR FIX APPLIES. `ReminderDue` and `ApplicationStale` were
emitted from READ paths, so half one detached the emission from the read. This
event's producer is `probes/api_validator.py::api_validator_probe`, a SCHEDULED
probe - the emission is correct and is left exactly as it is. What is wrong is
only that the same fact is banked over and over, which is half two: dedupe at
the subscriber, keyed on `constant_name` rather than `job_id`.

WHY AN EXISTING-UNDELIVERED-ROW PREDICATE AND NOT `last_notified_at` + AN
INTERVAL: a drifted constant stays drifted until someone fixes the parser, so
an interval sets the storm's RATE, not its CEILING - the same reasoning that
settled the reminder case, and drift is an even purer example of a permanently
true condition. This predicate is bounded by construction (one undelivered
EndpointDrift per constant, ever) and re-arms the moment the brief delivers.

It FAILS OPEN. A broken dedupe degrades to the old noise, which is recoverable;
silently dropping a genuine NEW drift is not - and the third constant appearing
mid-audit is the proof that new drifts really do arrive.

THE PROPERTY MOST LIKELY TO BE BROKEN BY A CARELESS DEDUPE is that a drift on a
constant nobody has heard about yet must announce itself IMMEDIATELY. That is
the whole point of the signal. It is pinned end to end through the real
subscriber against real SQLite in TestAThirdConstantStillAnnounces below, not
just at the mock layer.

Every guard here ships with a CONTROL that can fail it. "Suppress everything"
passes a suppression test trivially, and this repo has produced fourteen
checks-that-cannot-fail in three days - two of them inside guards built the
same day.
"""

import ast
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from naukri_server.events import EndpointDriftDetected

PKG = Path(__file__).resolve().parent.parent / "naukri_server"


def _drift(constant_name, **kw):
    kw.setdefault("severity", "added")
    kw.setdefault("drift_type", "field")
    kw.setdefault("changed_fields", {"data.title": "appeared"})
    kw.setdefault("snapshot_age_days", 3.0)
    return EndpointDriftDetected(constant_name=constant_name, **kw)


async def _undelivered(constant_name):
    """Undelivered EndpointDrift rows for one constant, straight from SQLite."""
    from naukri_server.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT COUNT(*) FROM notifications "
            "WHERE event_type = 'EndpointDrift' AND delivered_via IS NULL "
            "AND json_extract(metadata, '$.constant_name') = ?",
            (constant_name,),
        )
        return (await cur.fetchone())[0]
    finally:
        await db.close()


async def _all_rows(constant_name):
    """Every EndpointDrift row for one constant, delivered or not."""
    from naukri_server.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id FROM notifications "
            "WHERE event_type = 'EndpointDrift' "
            "AND json_extract(metadata, '$.constant_name') = ?",
            (constant_name,),
        )
        return [r[0] for r in await cur.fetchall()]
    finally:
        await db.close()


# =====================================================================
# 1. THE STORM, reproduced against real SQLite through the real router
#
# conftest._isolated_test_db points database.DB_PATH at a session tmp file
# carrying the real schema, so these exercise the actual json_extract query -
# the part most likely to be quietly wrong, since `metadata` is a JSON TEXT
# column and there is no constant_name column to match on.
# =====================================================================

class TestTheStorm:

    @pytest.mark.asyncio
    async def test_twelve_probe_runs_bank_one_row(self):
        """RED before the fix: 12 identical drift reports banked 12 rows.

        Twelve is his measured rate for a little over three hours. The real
        counts are 33, 25 and 23.
        """
        from naukri_server.healing.router import _store_drift_notification
        name = "STORM_TWELVE_API"

        for _ in range(12):
            await _store_drift_notification(name, "no parser module mapped", _drift(name))

        assert await _undelivered(name) == 1, (
            "one drifted constant is ONE fact; %d rows means the storm is live"
            % await _undelivered(name)
        )

    @pytest.mark.asyncio
    async def test_CONTROL_the_first_report_is_always_stored(self):
        """CONTROL. The cheapest way to pass the test above is to store
        nothing, which would mean he never hears about drift again."""
        from naukri_server.healing.router import _store_drift_notification
        name = "STORM_FIRST_API"

        assert await _undelivered(name) == 0
        await _store_drift_notification(name, "T3 endpoint - manual review required", _drift(name))
        assert await _undelivered(name) == 1

    @pytest.mark.asyncio
    async def test_delivery_re_arms_the_next_report(self):
        """The predicate is `delivered_via IS NULL`, so the brief delivering a
        drift notification lets the next probe run mint a fresh one. Without
        this the signal would be one-shot for the life of the database."""
        from naukri_server.database import mark_notifications_delivered
        from naukri_server.healing.router import _store_drift_notification
        name = "STORM_REARM_API"

        await _store_drift_notification(name, "healer disabled", _drift(name))
        await _store_drift_notification(name, "healer disabled", _drift(name))
        assert len(await _all_rows(name)) == 1

        await mark_notifications_delivered(await _all_rows(name))
        assert await _undelivered(name) == 0

        await _store_drift_notification(name, "healer disabled", _drift(name))
        assert await _undelivered(name) == 1, "delivery must re-arm the signal"
        assert len(await _all_rows(name)) == 2

    @pytest.mark.asyncio
    async def test_the_stored_row_carries_the_constant_name_in_metadata(self):
        """The dedupe key has to be IN the row it dedupes against. If the
        writer ever stops writing `metadata.constant_name`, the predicate
        silently matches nothing and every run banks again."""
        from naukri_server.database import get_db
        from naukri_server.healing.router import _store_drift_notification
        name = "STORM_META_API"

        await _store_drift_notification(name, "endpoint not in tier registry",
                                        _drift(name, severity="removed"))

        db = await get_db()
        try:
            cur = await db.execute(
                "SELECT json_extract(metadata, '$.constant_name'), priority "
                "FROM notifications WHERE event_type = 'EndpointDrift' "
                "AND json_extract(metadata, '$.constant_name') = ?",
                (name,),
            )
            row = await cur.fetchone()
        finally:
            await db.close()

        assert row is not None, "the notification was not stored at all"
        assert row[0] == name
        assert row[1] == "high", "severity=removed is a high-priority drift"


# =====================================================================
# 2. THE PROPERTY MOST LIKELY TO BE BROKEN
# =====================================================================

class TestAThirdConstantStillAnnounces:
    """His third constant, RECOMMENDED_JOBS_API, started drifting mid-audit
    while 58 rows for two OTHER constants sat undelivered. A dedupe that keyed
    on anything coarser than the constant - a global "one drift pending" flag,
    a per-severity key, a time window - would have swallowed it."""

    @pytest.mark.asyncio
    async def test_a_new_constant_announces_immediately_despite_a_backlog(self):
        from naukri_server.healing.router import _store_drift_notification
        first, second, third = "TRIO_DASHBOARD", "TRIO_APPLIED", "TRIO_RECOMMENDED"

        for _ in range(8):
            await _store_drift_notification(first, "no parser module mapped", _drift(first))
        for _ in range(6):
            await _store_drift_notification(second, "no parser module mapped", _drift(second))

        assert await _undelivered(first) == 1
        assert await _undelivered(second) == 1
        assert await _undelivered(third) == 0

        await _store_drift_notification(
            third, "no verified alias mapping to apply (would be a guess)",
            _drift(third, severity="removed"),
        )

        assert await _undelivered(third) == 1, (
            "a drift on a constant with nothing pending MUST announce itself - "
            "this is the signal the whole subsystem exists to deliver"
        )
        assert await _undelivered(first) == 1
        assert await _undelivered(second) == 1

    @pytest.mark.asyncio
    async def test_end_to_end_through_the_real_subscriber(self):
        """Not the choke point in isolation - the actual `@subscriber` the event
        bus calls, on an unmapped constant (tier_for returns None), which is the
        shortest real path to a notification."""
        from naukri_server.healing import tier_registry
        from naukri_server.healing.router import on_endpoint_drift
        name = "E2E_UNMAPPED_API_ZZZ"
        assert tier_registry.tier_for(name) is None, "pick a genuinely unmapped constant"

        for _ in range(5):
            await on_endpoint_drift(_drift(name))
        assert await _undelivered(name) == 1

        other = "E2E_UNMAPPED_OTHER_ZZZ"
        assert tier_registry.tier_for(other) is None
        await on_endpoint_drift(_drift(other))
        assert await _undelivered(other) == 1, (
            "the backlog for one constant must not silence another"
        )


# =====================================================================
# 3. The dedupe decision itself, at the mock layer
# =====================================================================

class TestDedupeDecision:

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_suppressed_when_one_is_already_pending(self, mock_has, mock_store):
        """RED before the fix: the writer stored unconditionally."""
        from naukri_server.healing.router import _store_drift_notification
        mock_has.return_value = True

        await _store_drift_notification("DASHBOARD_API", "healer disabled",
                                        _drift("DASHBOARD_API"))

        mock_store.assert_not_called()
        mock_has.assert_awaited_once_with(
            "EndpointDrift", "DASHBOARD_API", metadata_key="constant_name")

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_CONTROL_stores_when_nothing_is_pending(self, mock_has, mock_store):
        from naukri_server.healing.router import _store_drift_notification
        mock_has.return_value = False

        await _store_drift_notification("DASHBOARD_API", "healer disabled",
                                        _drift("DASHBOARD_API"))

        mock_store.assert_called_once()
        stored = mock_store.call_args[0][0]
        assert stored["event_type"] == "EndpointDrift"
        assert stored["metadata"]["constant_name"] == "DASHBOARD_API"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_dedupe_is_scoped_to_the_constant(self, mock_has, mock_store):
        from naukri_server.healing.router import _store_drift_notification
        pending = {"DASHBOARD_API"}
        mock_has.side_effect = lambda event_type, key, metadata_key=None: key in pending

        await _store_drift_notification("DASHBOARD_API", "r", _drift("DASHBOARD_API"))
        await _store_drift_notification("APPLIED_JOBS_API", "r", _drift("APPLIED_JOBS_API"))

        assert mock_store.call_count == 1
        assert mock_store.call_args[0][0]["metadata"]["constant_name"] == "APPLIED_JOBS_API"

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_dedupe_failure_fails_open(self, mock_has, mock_store):
        """A broken predicate degrades to the old noise, which is recoverable.
        Dropping a genuine new drift is not."""
        from naukri_server.healing.router import _store_drift_notification
        mock_has.side_effect = RuntimeError("no such column: constant_name")

        await _store_drift_notification("DASHBOARD_API", "healer disabled",
                                        _drift("DASHBOARD_API"))

        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_an_empty_constant_name_is_not_deduped_away(self, mock_has, mock_store):
        """An empty key cannot be matched against anything, so a predicate that
        returns True for '' must not silence the report."""
        from naukri_server.healing.router import _store_drift_notification
        mock_has.return_value = True

        await _store_drift_notification("", "unknown drift_type", _drift(""))

        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_a_store_failure_never_escapes(self, mock_has, mock_store):
        """The writer is called from an event-bus handler. A notification
        failure must not crash dispatch."""
        from naukri_server.healing.router import _store_drift_notification
        mock_has.return_value = False
        mock_store.side_effect = RuntimeError("disk full")

        await _store_drift_notification("DASHBOARD_API", "healer disabled",
                                        _drift("DASHBOARD_API"))  # must not raise


# =====================================================================
# 4. The predicate against real SQLite, on the constant_name key
# =====================================================================

class TestHasPendingNotificationByConstant:

    @pytest.mark.asyncio
    async def test_true_for_an_undelivered_row(self):
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "EndpointDrift", "title": "t", "body": "b",
            "priority": "medium", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"constant_name": "HPC-A_API"},
        })
        assert await has_pending_notification(
            "EndpointDrift", "HPC-A_API", metadata_key="constant_name") is True

    @pytest.mark.asyncio
    async def test_false_once_delivered(self):
        from naukri_server.database import (
            store_notification, has_pending_notification, mark_notifications_delivered,
        )
        row_id = await store_notification({
            "event_type": "EndpointDrift", "title": "t", "body": "b",
            "priority": "medium", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"constant_name": "HPC-B_API"},
        })
        assert await has_pending_notification(
            "EndpointDrift", "HPC-B_API", metadata_key="constant_name") is True
        await mark_notifications_delivered([row_id])
        assert await has_pending_notification(
            "EndpointDrift", "HPC-B_API", metadata_key="constant_name") is False

    @pytest.mark.asyncio
    async def test_constant_name_is_matched_exactly_not_by_substring(self):
        """The obvious cheap implementation is `metadata LIKE '%<name>%'`.
        Config constant names nest by construction - DASHBOARD_API is a strict
        prefix of a plausible DASHBOARD_API_V2 - so a LIKE would silence drift
        on the new endpoint because the old one is already pending."""
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "EndpointDrift", "title": "t", "body": "b",
            "priority": "medium", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"constant_name": "HPC_DASHBOARD_API_V2"},
        })
        assert await has_pending_notification(
            "EndpointDrift", "HPC_DASHBOARD_API_V2", metadata_key="constant_name") is True
        assert await has_pending_notification(
            "EndpointDrift", "HPC_DASHBOARD_API", metadata_key="constant_name") is False

    @pytest.mark.asyncio
    async def test_CONTROL_a_LIKE_would_have_matched_the_prefix(self):
        """The test above only means something if the trap it describes is
        real. Run the rejected implementation against the same row and show it
        returning the wrong answer."""
        from naukri_server.database import get_db
        db = await get_db()
        try:
            cur = await db.execute(
                "SELECT COUNT(*) FROM notifications WHERE event_type = 'EndpointDrift' "
                "AND delivered_via IS NULL AND metadata LIKE ?",
                ("%HPC_DASHBOARD_API%",),
            )
            like_hits = (await cur.fetchone())[0]
        finally:
            await db.close()
        assert like_hits >= 1, (
            "the LIKE trap did not reproduce, so the exact-match test above "
            "proves nothing - check the fixture row still exists"
        )

    @pytest.mark.asyncio
    async def test_false_across_event_types(self):
        """A pending HealingProposal for a constant must not silence its
        EndpointDrift, and vice versa."""
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "HealingProposal", "title": "t", "body": "b",
            "priority": "medium", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"constant_name": "HPC-D_API"},
        })
        assert await has_pending_notification(
            "EndpointDrift", "HPC-D_API", metadata_key="constant_name") is False
        assert await has_pending_notification(
            "HealingProposal", "HPC-D_API", metadata_key="constant_name") is True

    @pytest.mark.asyncio
    async def test_false_when_metadata_is_null(self):
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "EndpointDrift", "title": "t", "body": "b",
            "priority": "medium", "created_at": datetime.now(timezone.utc).isoformat(),
        })
        assert await has_pending_notification(
            "EndpointDrift", "HPC-E_API", metadata_key="constant_name") is False

    @pytest.mark.asyncio
    async def test_the_two_keys_do_not_see_each_other(self):
        """job_id and constant_name are different JSON paths on the same column.
        A row keyed by one must be invisible to a lookup on the other, or the
        generalisation has broken the reminder and stale dedupes."""
        from naukri_server.database import store_notification, has_pending_notification
        await store_notification({
            "event_type": "ReminderDue", "title": "t", "body": "b",
            "priority": "high", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": "HPC-CROSS"},
        })
        assert await has_pending_notification("ReminderDue", "HPC-CROSS") is True
        assert await has_pending_notification(
            "ReminderDue", "HPC-CROSS", metadata_key="constant_name") is False

    @pytest.mark.asyncio
    async def test_the_default_key_is_still_job_id(self):
        """Backward compatibility, stated as a test rather than assumed: the
        two shipped callers pass no metadata_key at all."""
        import inspect
        from naukri_server.database import has_pending_notification
        sig = inspect.signature(has_pending_notification)
        assert sig.parameters["metadata_key"].default == "job_id"

    @pytest.mark.asyncio
    async def test_a_bogus_metadata_key_is_refused_loudly(self):
        """The JSON path is built from this argument. Anything that is not a
        plain identifier is a programming error and must raise where a caller's
        fail-open can log it, not silently match nothing forever."""
        from naukri_server.database import has_pending_notification
        with pytest.raises(ValueError):
            await has_pending_notification("EndpointDrift", "X", metadata_key="a.b")
        with pytest.raises(ValueError):
            await has_pending_notification("EndpointDrift", "X", metadata_key="$.job_id")


# =====================================================================
# 5. SOURCE GUARD - exactly one function may bank an EndpointDrift row
#
# A behavioural test cannot see a SECOND writer added next month next to the
# deduped one. The router already has eight call sites funnelling into
# _store_drift_notification; a ninth that calls store_notification directly
# would restart the storm with every behavioural test above still green.
# =====================================================================

def drift_writers(root: Path) -> set:
    """Every (file, function) that calls store_notification with a literal
    event_type of "EndpointDrift"."""
    found = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relpath = path.relative_to(root).as_posix()
        stack = []

        class _V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", None))
                if name == "store_notification" and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Dict):
                        for k, v in zip(arg.keys, arg.values):
                            if (isinstance(k, ast.Constant) and k.value == "event_type"
                                    and isinstance(v, ast.Constant)
                                    and v.value == "EndpointDrift"):
                                found.add((relpath, stack[-1] if stack else "<module>"))
                self.generic_visit(node)

        _V().visit(tree)
    return found


_TWO_WRITERS = '''
async def _store_drift_notification(name):
    await store_notification({"event_type": "EndpointDrift", "title": name})

async def _some_new_path(name):
    """The regression this guard exists to catch."""
    await store_notification({"event_type": "EndpointDrift", "title": name})
'''

_OTHER_EVENT = '''
async def _store_proposal(name):
    await store_notification({"event_type": "HealingProposal", "title": name})
'''


class TestTheGuardItself:

    def test_CONTROL_detects_a_second_writer(self, tmp_path):
        (tmp_path / "two.py").write_text(_TWO_WRITERS, encoding="utf-8")
        assert drift_writers(tmp_path) == {
            ("two.py", "_store_drift_notification"),
            ("two.py", "_some_new_path"),
        }

    def test_CONTROL_is_silent_on_another_event_type(self, tmp_path):
        """No false positives, or the guard is noise."""
        (tmp_path / "other.py").write_text(_OTHER_EVENT, encoding="utf-8")
        assert drift_writers(tmp_path) == set()

    def test_exactly_one_deduped_writer_in_the_package(self):
        assert drift_writers(PKG) == {("healing/router.py", "_store_drift_notification")}, (
            "EndpointDrift rows must be banked from exactly one function, "
            "because exactly one function holds the dedupe. Found: %s"
            % sorted(drift_writers(PKG))
        )

    def test_that_one_writer_consults_the_dedupe(self):
        """Pairs with the test above: one writer is only worth pinning if that
        writer is the deduped one."""
        src = (PKG / "healing" / "router.py").read_text(encoding="utf-8")
        body = src.split("async def _store_drift_notification")[1].split("\nasync def ")[0]
        assert "has_pending_notification" in body
        assert 'metadata_key="constant_name"' in body
