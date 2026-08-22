"""HealingProposal banks the same fact once per drift event, with no dedupe.

Fourth instance of the same defect class, after `ReminderDue` (1,084 rows / 50
facts), `ApplicationStale` (85 rows / 17 facts) and `EndpointDrift` (81 rows /
3 facts, all three 2026-08-21/22).

THIS ONE IS LATENT, NOT ABSENT. His DB holds 0 HealingProposal rows today, for
two reasons that are both temporary: the healer circuit is off, and the
synthesis path needs a candidate that survives the dry-run. The producer is the
same scheduled api_validator probe that minted the 84 EndpointDrift rows, and
the two writers here sit on that same per-probe-run cadence:

    healing/t3_notify.py::notify_t3_proposal     - every T3 drift, every run
    healing/router.py::_store_synthesis_proposal - every shadow-mode candidate

A T3 endpoint stays drifted until someone edits the parser, exactly like the
EndpointDrift case, so the storm is a matter of flipping one flag. Both writers
already carry `metadata.constant_name`, so the fix is the same predicate the
EndpointDrift writer took: one undelivered row per constant, re-armed by
delivery, keyed on the CONSTANT and nothing coarser.

IT FAILS OPEN. A broken predicate degrades to the old noise, which is
recoverable; swallowing a genuine new proposal is not.

THE TRAP THIS FILE EXISTS TO PIN. router.py::_notify_t3 treats a
not-delivered T3 outcome as "fall back and bank a plain EndpointDrift row":

    outcome = await t3_notify.notify_t3_proposal(...)
    if not outcome.delivered:
        await _store_drift_notification(...)

A dedupe that reports a duplicate as delivered=False therefore does not stop
the storm - it MOVES it, converting a HealingProposal storm into an
EndpointDrift one. A suppression must be a NO-OP, not a fallback, so the
outcome carries a separate `suppressed` flag and the router's fallback is
conditioned on `not delivered and not suppressed`. A naive test passes without
that distinction, which is why TestTheT3FallbackTrap asserts the EndpointDrift
count directly and keeps a CONTROL showing the fallback still fires for a
genuinely undelivered outcome.

Every guard here ships with a CONTROL that can fail it. "Suppress everything"
passes a suppression test trivially, and this repo has produced fourteen
checks-that-cannot-fail in three days.

ISOLATION NOTE. conftest._isolated_test_db points database.DB_PATH at a
SESSION-scoped tmp DB, so rows written here outlive the test that wrote them
and are visible to every later test file. Two consequences, both deliberate:
every test below uses its own constant name, and those names are synthetic
(`PROP_...`, mapped to a T3 tier entry by the fixture) so that no undelivered
row is ever left behind for a REAL constant that tests/test_healing_t3_notify.py
- which runs after this file and mocks only the store - would then find pending.
"""

import ast
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from naukri_server.events import EndpointDriftDetected
from naukri_server.healing import tier_registry

PKG = Path(__file__).resolve().parent.parent / "naukri_server"

# Every constant used below starts with this. The fixture maps the whole
# namespace to a synthetic T3 entry so real registry names stay untouched.
SYNTH = "PROP_"


@pytest.fixture(autouse=True)
def _synthetic_t3_registry():
    """Map PROP_* constants to a T3 tier entry, pass everything else through.

    Both writers under test are reached only for T3 endpoints, and the real
    registry has 12 of them - all of which other test files use against this
    same session DB. Synthetic names keep the storm rows in a namespace nobody
    else reads.
    """
    real_tier_for = tier_registry.tier_for
    entry = tier_registry.TierEntry(
        tier_registry.TIER_T3,
        "naukri_server.tools.apply",
        "synthetic T3 entry for the proposal-storm tests",
    )

    def fake_tier_for(constant_name):
        if constant_name and constant_name.startswith(SYNTH):
            return entry
        return real_tier_for(constant_name)

    with patch("naukri_server.healing.tier_registry.tier_for", new=fake_tier_for):
        yield


def _drift(constant_name, **kw):
    kw.setdefault("severity", "added")
    kw.setdefault("drift_type", "field")
    kw.setdefault("changed_fields", {"data.title": "appeared"})
    kw.setdefault("snapshot_age_days", 3.0)
    return EndpointDriftDetected(constant_name=constant_name, **kw)


def _candidate(constant_name, **kw):
    from naukri_server.healing.synthesis import FixCandidate
    kw.setdefault("drift_type", "field")
    kw.setdefault("canonical_key", "title")
    kw.setdefault("new_alias", "newJobTitle")
    kw.setdefault("evidence", "sibling path data.newJobTitle matched canonical 'title'")
    kw.setdefault("confidence", 1.0)
    kw.setdefault("dry_run_passed", True)
    return FixCandidate(constant_name=constant_name, **kw)


async def _count(event_type, constant_name, undelivered_only=True):
    """Notification rows of one type for one constant, straight from SQLite."""
    from naukri_server.database import get_db
    sql = ("SELECT COUNT(*) FROM notifications WHERE event_type = ? "
           "AND json_extract(metadata, '$.constant_name') = ?")
    if undelivered_only:
        sql += " AND delivered_via IS NULL"
    db = await get_db()
    try:
        cur = await db.execute(sql, (event_type, constant_name))
        return (await cur.fetchone())[0]
    finally:
        await db.close()


async def _proposals(constant_name):
    return await _count("HealingProposal", constant_name)


async def _drifts(constant_name):
    return await _count("EndpointDrift", constant_name)


async def _proposal_ids(constant_name):
    from naukri_server.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id FROM notifications WHERE event_type = 'HealingProposal' "
            "AND json_extract(metadata, '$.constant_name') = ?",
            (constant_name,),
        )
        return [r[0] for r in await cur.fetchall()]
    finally:
        await db.close()


async def _notify(constant_name, before="WORKFLOW = '/old'\n", after="WORKFLOW = '/new'\n"):
    """One T3 proposal through the real writer, with a real non-empty diff."""
    from naukri_server.healing.t3_notify import notify_t3_proposal
    return await notify_t3_proposal(
        constant_name,
        file_path="naukri_server/tools/apply.py",
        before_source=before,
        after_source=after,
        drift_summary="severity=added, drift_type=field",
    )


async def _synth(constant_name, shadow=True, reason="shadow mode - not applied"):
    """One synthesis proposal through the real writer."""
    from naukri_server.healing.router import _store_synthesis_proposal
    await _store_synthesis_proposal(
        _candidate(constant_name), shadow=shadow, reason=reason,
    )


def _tmp_repo(tmp_path, config_body):
    """A repo root carrying just naukri_server/config.py - what _notify_t3 reads."""
    pkg = tmp_path / "naukri_server"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "config.py").write_text(config_body, encoding="utf-8")
    return tmp_path


def _url_drift(constant_name, new_url="/new/path"):
    return _drift(
        constant_name,
        drift_type="url",
        changed_fields={"__new_url__": new_url, "endpoint": "moved"},
    )


# =====================================================================
# 1. THE STORM, reproduced against real SQLite through both real writers
#
# The DB fixture carries the real schema, so these exercise the actual
# json_extract query - the part most likely to be quietly wrong, since
# `metadata` is a JSON TEXT column with no constant_name column to match on.
# =====================================================================

class TestTheStorm:

    @pytest.mark.asyncio
    async def test_twelve_t3_notifies_bank_one_row(self):
        """RED before the fix: 12 identical T3 drift reports banked 12 rows.

        Twelve is the measured EndpointDrift rate for a little over three
        hours on the same probe cadence; the real per-constant counts there
        were 33, 25 and 23.
        """
        name = SYNTH + "STORM_T3_API"

        for _ in range(12):
            await _notify(name)

        assert await _proposals(name) == 1, (
            "one drifted T3 constant is ONE fact; %d rows means the storm is live"
            % await _proposals(name)
        )

    @pytest.mark.asyncio
    async def test_twelve_synthesis_proposals_bank_one_row(self):
        """RED before the fix: the shadow-mode writer stored unconditionally too."""
        name = SYNTH + "STORM_SYNTH_API"

        for _ in range(12):
            await _synth(name)

        assert await _proposals(name) == 1, (
            "one synthesized candidate is ONE fact; %d rows means the storm is live"
            % await _proposals(name)
        )

    @pytest.mark.asyncio
    async def test_CONTROL_the_first_t3_proposal_is_always_stored(self):
        """CONTROL. The cheapest way to pass the test above is to store
        nothing, which would mean he never sees a T3 proposal again."""
        name = SYNTH + "FIRST_T3_API"

        assert await _proposals(name) == 0
        outcome = await _notify(name)
        assert outcome.delivered is True
        assert await _proposals(name) == 1

    @pytest.mark.asyncio
    async def test_CONTROL_the_first_synthesis_proposal_is_always_stored(self):
        """CONTROL for the synthesis writer. A withheld shadow-mode proposal is
        the ONLY way he can watch the healer before enabling it."""
        name = SYNTH + "FIRST_SYNTH_API"

        assert await _proposals(name) == 0
        await _synth(name)
        assert await _proposals(name) == 1

    @pytest.mark.asyncio
    async def test_delivery_re_arms_the_t3_proposal(self):
        """The predicate is `delivered_via IS NULL`, so the brief delivering a
        proposal lets the next probe run mint a fresh one. Without this the
        signal is one-shot for the life of the database."""
        from naukri_server.database import mark_notifications_delivered
        name = SYNTH + "REARM_T3_API"

        await _notify(name)
        await _notify(name)
        assert len(await _proposal_ids(name)) == 1

        await mark_notifications_delivered(await _proposal_ids(name))
        assert await _proposals(name) == 0

        await _notify(name)
        assert await _proposals(name) == 1, "delivery must re-arm the signal"
        assert len(await _proposal_ids(name)) == 2

    @pytest.mark.asyncio
    async def test_delivery_re_arms_the_synthesis_proposal(self):
        from naukri_server.database import mark_notifications_delivered
        name = SYNTH + "REARM_SYNTH_API"

        await _synth(name)
        await _synth(name)
        assert len(await _proposal_ids(name)) == 1

        await mark_notifications_delivered(await _proposal_ids(name))
        assert await _proposals(name) == 0

        await _synth(name)
        assert await _proposals(name) == 1, "delivery must re-arm the signal"
        assert len(await _proposal_ids(name)) == 2

    @pytest.mark.asyncio
    async def test_both_writers_put_the_dedupe_key_in_the_row(self):
        """The dedupe key has to be IN the row it dedupes against. If either
        writer stops writing `metadata.constant_name` the predicate silently
        matches nothing and every run banks again."""
        from naukri_server.database import get_db
        t3_name = SYNTH + "META_T3_API"
        synth_name = SYNTH + "META_SYNTH_API"

        await _notify(t3_name)
        await _synth(synth_name)

        db = await get_db()
        try:
            cur = await db.execute(
                "SELECT json_extract(metadata, '$.constant_name'), priority "
                "FROM notifications WHERE event_type = 'HealingProposal' "
                "AND json_extract(metadata, '$.constant_name') IN (?, ?) "
                "ORDER BY id",
                (t3_name, synth_name),
            )
            rows = await cur.fetchall()
        finally:
            await db.close()

        assert [r[0] for r in rows] == [t3_name, synth_name]
        assert rows[0][1] == "high", "a T3 proposal is high priority"
        assert rows[1][1] == "medium"


# =====================================================================
# 2. THE PROPERTY MOST LIKELY TO BE BROKEN, end to end
#
# RECOMMENDED_JOBS_API started drifting mid-audit while 58 rows for two OTHER
# constants sat undelivered. A dedupe keyed on anything coarser than the
# constant - a global "one proposal pending" flag, a per-tier key, a time
# window - would have swallowed it. Proven here through the real writers and
# the real router against real SQLite, not at the mock layer.
# =====================================================================

class TestAThirdConstantStillAnnounces:

    @pytest.mark.asyncio
    async def test_a_third_constant_announces_despite_a_t3_backlog(self):
        first, second, third = (SYNTH + "TRIO_A", SYNTH + "TRIO_B", SYNTH + "TRIO_C")

        for _ in range(8):
            await _notify(first)
        for _ in range(6):
            await _notify(second)

        assert await _proposals(first) == 1
        assert await _proposals(second) == 1
        assert await _proposals(third) == 0

        outcome = await _notify(third)

        assert outcome.delivered is True, (
            "a proposal for a constant with nothing pending MUST be delivered - "
            "this is the signal the whole subsystem exists to carry"
        )
        assert await _proposals(third) == 1
        assert await _proposals(first) == 1, "the third constant must not disturb the backlog"
        assert await _proposals(second) == 1

    @pytest.mark.asyncio
    async def test_a_third_constant_announces_despite_a_synthesis_backlog(self):
        first, second, third = (SYNTH + "TRIOS_A", SYNTH + "TRIOS_B", SYNTH + "TRIOS_C")

        for _ in range(8):
            await _synth(first)
        for _ in range(6):
            await _synth(second)

        assert await _proposals(first) == 1
        assert await _proposals(second) == 1
        assert await _proposals(third) == 0

        await _synth(third)

        assert await _proposals(third) == 1, (
            "a first-ever synthesized proposal must be stored even with a backlog"
        )
        assert await _proposals(first) == 1
        assert await _proposals(second) == 1

    @pytest.mark.asyncio
    async def test_the_two_writers_do_not_silence_each_other_across_constants(self):
        """Mixed backlog: one constant banked by each writer. A first-ever
        proposal on a third constant must still land."""
        first, second, third = (SYNTH + "MIX_A", SYNTH + "MIX_B", SYNTH + "MIX_C")

        for _ in range(5):
            await _notify(first)
        for _ in range(5):
            await _synth(second)

        assert (await _proposals(first), await _proposals(second)) == (1, 1)

        await _synth(third)
        assert await _proposals(third) == 1

    @pytest.mark.asyncio
    async def test_end_to_end_through_the_real_router(self, tmp_path):
        """Not the choke point in isolation - router._notify_t3, the function
        the EndpointDriftDetected subscriber actually calls, driven with a real
        repo tree so the diff is computed by the real patcher."""
        from naukri_server.healing.router import _notify_t3
        name = SYNTH + "E2E_ROUTER_API"
        other = SYNTH + "E2E_ROUTER_OTHER_API"
        repo = _tmp_repo(tmp_path, '%s = "/old/path"\n%s = "/old/other"\n' % (name, other))

        for _ in range(5):
            await _notify_t3(_url_drift(name), repo)

        assert await _proposals(name) == 1
        assert await _drifts(name) == 0, (
            "five probe runs must not bank a single plain drift row on this path"
        )

        await _notify_t3(_url_drift(other, "/new/other"), repo)
        assert await _proposals(other) == 1, (
            "the backlog for one constant must not silence another"
        )


# =====================================================================
# 3. THE T3 FALLBACK TRAP
#
# _notify_t3's fallback turns a not-delivered outcome into a plain
# EndpointDrift row. If a suppressed duplicate reports delivered=False with no
# further signal, the HealingProposal storm becomes an EndpointDrift storm and
# every test in section 1 still passes.
# =====================================================================

class TestTheT3FallbackTrap:

    @pytest.mark.asyncio
    async def test_a_suppressed_duplicate_is_a_no_op_not_a_fallback(self, tmp_path):
        """THE test. Second identical probe run: no second proposal AND no
        plain drift row. A naive delivered=False dedupe passes the first
        assertion and fails the second."""
        from naukri_server.healing.router import _notify_t3
        name = SYNTH + "TRAP_NOOP_API"
        repo = _tmp_repo(tmp_path, '%s = "/old/path"\n' % name)

        await _notify_t3(_url_drift(name), repo)
        assert await _proposals(name) == 1
        assert await _drifts(name) == 0

        await _notify_t3(_url_drift(name), repo)

        assert await _proposals(name) == 1, "the duplicate proposal must be suppressed"
        assert await _drifts(name) == 0, (
            "a suppressed duplicate must be a NO-OP; banking an EndpointDrift row "
            "instead just moves the storm to another event_type"
        )

    @pytest.mark.asyncio
    async def test_the_outcome_distinguishes_suppressed_from_undelivered(self):
        """The router can only make the distinction if the outcome carries it."""
        name = SYNTH + "TRAP_FLAG_API"

        first = await _notify(name)
        assert first.delivered is True
        assert first.suppressed is False

        second = await _notify(name)
        assert second.delivered is False, "nothing new was written"
        assert second.suppressed is True, (
            "a duplicate is suppressed, not merely undelivered - the router's "
            "fallback keys off exactly this"
        )

    @pytest.mark.asyncio
    async def test_CONTROL_a_genuinely_undelivered_outcome_still_falls_back(self, tmp_path):
        """CONTROL for the assertion above. `never fall back` would pass the
        no-op test trivially and would silently lose every real T3 drift whose
        proposal could not be built. Both arms of the router's condition are
        exercised here with the collaborator stubbed, because a real
        notify_t3_proposal cannot return delivered=False from this path once
        the diff is non-empty."""
        from naukri_server.healing import t3_notify
        from naukri_server.healing.router import _notify_t3
        undeliverable = SYNTH + "TRAP_CTRL_UNDELIVERED_API"
        suppressed = SYNTH + "TRAP_CTRL_SUPPRESSED_API"
        repo = _tmp_repo(
            tmp_path, '%s = "/old/a"\n%s = "/old/b"\n' % (undeliverable, suppressed))

        stub = t3_notify.T3NotifyOutcome(
            delivered=False, constant_name=undeliverable,
            skipped_reason="proposed patch is empty (no diff)",
        )
        with patch("naukri_server.healing.t3_notify.notify_t3_proposal",
                   new=AsyncMock(return_value=stub)):
            await _notify_t3(_url_drift(undeliverable), repo)
        assert await _drifts(undeliverable) == 1, (
            "an undelivered proposal MUST still reach him as a plain drift row"
        )

        stub = t3_notify.T3NotifyOutcome(
            delivered=False, suppressed=True, constant_name=suppressed,
            skipped_reason="a HealingProposal is already pending",
        )
        with patch("naukri_server.healing.t3_notify.notify_t3_proposal",
                   new=AsyncMock(return_value=stub)):
            await _notify_t3(_url_drift(suppressed), repo)
        assert await _drifts(suppressed) == 0

    @pytest.mark.asyncio
    async def test_CONTROL_the_real_fallback_path_is_alive(self, tmp_path):
        """The no-op assertions are only worth something if _notify_t3 can bank
        an EndpointDrift row at all against this tmp repo. Give it a constant
        the config file does not define: the patcher returns None, no proposal
        is computed, and the plain-drift fallback fires for real."""
        from naukri_server.healing.router import _notify_t3
        name = SYNTH + "TRAP_ALIVE_API"
        repo = _tmp_repo(tmp_path, 'SOMETHING_ELSE_API = "/old/path"\n')

        await _notify_t3(_url_drift(name), repo)

        assert await _drifts(name) == 1
        assert await _proposals(name) == 0


# =====================================================================
# 4. The dedupe decision itself, at the mock layer, for each writer
# =====================================================================

class TestDedupeDecision:

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_t3_suppressed_when_one_is_already_pending(self, mock_has, mock_store):
        """RED before the fix: the writer stored unconditionally."""
        name = SYNTH + "MOCK_T3_API"
        mock_has.return_value = True

        outcome = await _notify(name)

        mock_store.assert_not_called()
        mock_has.assert_awaited_once_with(
            "HealingProposal", name, metadata_key="constant_name")
        assert outcome.suppressed is True

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_CONTROL_t3_stores_when_nothing_is_pending(self, mock_has, mock_store):
        name = SYNTH + "MOCK_T3_CTRL_API"
        mock_has.return_value = False
        mock_store.return_value = 7

        outcome = await _notify(name)

        mock_store.assert_called_once()
        stored = mock_store.call_args[0][0]
        assert stored["event_type"] == "HealingProposal"
        assert stored["metadata"]["constant_name"] == name
        assert outcome.delivered is True
        assert outcome.suppressed is False

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_synthesis_suppressed_when_one_is_already_pending(self, mock_has, mock_store):
        name = SYNTH + "MOCK_SYNTH_API"
        mock_has.return_value = True

        await _synth(name)

        mock_store.assert_not_called()
        mock_has.assert_awaited_once_with(
            "HealingProposal", name, metadata_key="constant_name")

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_CONTROL_synthesis_stores_when_nothing_is_pending(self, mock_has, mock_store):
        name = SYNTH + "MOCK_SYNTH_CTRL_API"
        mock_has.return_value = False

        await _synth(name)

        mock_store.assert_called_once()
        stored = mock_store.call_args[0][0]
        assert stored["event_type"] == "HealingProposal"
        assert stored["metadata"]["constant_name"] == name

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_the_dedupe_is_scoped_to_the_constant(self, mock_has, mock_store):
        pending = {SYNTH + "SCOPE_PENDING_API"}
        mock_has.side_effect = lambda event_type, key, metadata_key=None: key in pending

        await _notify(SYNTH + "SCOPE_PENDING_API")
        await _notify(SYNTH + "SCOPE_FRESH_API")
        await _synth(SYNTH + "SCOPE_PENDING_API")
        await _synth(SYNTH + "SCOPE_FRESH_API")

        assert mock_store.call_count == 2
        stored = [c[0][0]["metadata"]["constant_name"] for c in mock_store.call_args_list]
        assert stored == [SYNTH + "SCOPE_FRESH_API", SYNTH + "SCOPE_FRESH_API"]

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_t3_dedupe_failure_fails_open(self, mock_has, mock_store):
        """A broken predicate degrades to the old noise, which is recoverable.
        Dropping a genuine new proposal is not."""
        mock_has.side_effect = RuntimeError("no such column: constant_name")
        mock_store.return_value = 11

        outcome = await _notify(SYNTH + "FAILOPEN_T3_API")

        mock_store.assert_called_once()
        assert outcome.delivered is True
        assert outcome.suppressed is False

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_synthesis_dedupe_failure_fails_open(self, mock_has, mock_store):
        mock_has.side_effect = RuntimeError("no such column: constant_name")

        await _synth(SYNTH + "FAILOPEN_SYNTH_API")

        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_an_empty_constant_name_is_not_deduped_away(self, mock_has, mock_store):
        """An empty key cannot be matched against anything, so a predicate that
        returns True for '' must not silence the proposal. (The T3 writer never
        sees one - tier_for rejects it first - but the synthesis writer can.)"""
        mock_has.return_value = True

        await _synth("")

        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_a_synthesis_store_failure_never_escapes(self, mock_has, mock_store):
        """The writer is called from an event-bus handler. A notification
        failure must not crash dispatch."""
        mock_has.return_value = False
        mock_store.side_effect = RuntimeError("disk full")

        await _synth(SYNTH + "STOREFAIL_SYNTH_API")  # must not raise

    @pytest.mark.asyncio
    @patch("naukri_server.database.store_notification", new_callable=AsyncMock)
    @patch("naukri_server.database.has_pending_notification", new_callable=AsyncMock)
    async def test_a_t3_store_failure_is_undelivered_not_suppressed(self, mock_has, mock_store):
        """A store failure must keep the router's fallback armed."""
        mock_has.return_value = False
        mock_store.side_effect = RuntimeError("disk full")

        outcome = await _notify(SYNTH + "STOREFAIL_T3_API")

        assert outcome.delivered is False
        assert outcome.suppressed is False, (
            "a failed store is exactly the case the plain-drift fallback exists for"
        )


# =====================================================================
# 5. CROSS-TYPE ISOLATION
#
# EndpointDrift and HealingProposal are two different facts about the same
# constant, and both dedupe on the same metadata key. A predicate that ignored
# event_type would let one silence the other.
# =====================================================================

class TestCrossTypeIsolation:

    @pytest.mark.asyncio
    async def test_a_pending_drift_row_does_not_silence_a_proposal(self):
        from naukri_server.healing.router import _store_drift_notification
        name = SYNTH + "XTYPE_DRIFT_FIRST_API"

        await _store_drift_notification(name, "healer disabled", _drift(name))
        assert await _drifts(name) == 1
        assert await _proposals(name) == 0

        await _notify(name)
        await _synth(name)

        assert await _proposals(name) == 1, (
            "a pending EndpointDrift must not suppress the HealingProposal - "
            "the proposal carries the diff, the drift row does not"
        )
        assert await _drifts(name) == 1

    @pytest.mark.asyncio
    async def test_a_pending_proposal_does_not_silence_a_drift_row(self):
        from naukri_server.healing.router import _store_drift_notification
        name = SYNTH + "XTYPE_PROPOSAL_FIRST_API"

        await _notify(name)
        assert await _proposals(name) == 1
        assert await _drifts(name) == 0

        await _store_drift_notification(name, "healer disabled", _drift(name))

        assert await _drifts(name) == 1, (
            "a pending HealingProposal must not suppress a plain drift row"
        )
        assert await _proposals(name) == 1

    @pytest.mark.asyncio
    async def test_the_predicate_itself_separates_the_two_types(self):
        """Straight at has_pending_notification, on one shared constant."""
        from naukri_server.database import store_notification, has_pending_notification
        name = SYNTH + "XTYPE_PREDICATE_API"
        await store_notification({
            "event_type": "HealingProposal", "title": "t", "body": "b",
            "priority": "high", "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"constant_name": name},
        })
        assert await has_pending_notification(
            "HealingProposal", name, metadata_key="constant_name") is True
        assert await has_pending_notification(
            "EndpointDrift", name, metadata_key="constant_name") is False


# =====================================================================
# 6. SOURCE GUARD - exactly TWO functions may bank a HealingProposal row
#
# A behavioural test cannot see a THIRD writer added next month next to the two
# deduped ones. The healing package already has several call sites funnelling
# into these two; a third that calls store_notification directly would restart
# the storm with every behavioural test above still green.
# =====================================================================

def proposal_writers(root: Path) -> set:
    """Every (file, function) that calls store_notification with a literal
    event_type of "HealingProposal"."""
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
                                    and v.value == "HealingProposal"):
                                found.add((relpath, stack[-1] if stack else "<module>"))
                self.generic_visit(node)

        _V().visit(tree)
    return found


def function_source(path: Path, func_name: str) -> str:
    """Source text of one top-level function, by name."""
    src = path.read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == func_name):
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("%s not found in %s" % (func_name, path))


_THREE_WRITERS = '''
async def notify_t3_proposal(name):
    await store_notification({"event_type": "HealingProposal", "title": name})

async def _store_synthesis_proposal(name):
    await store_notification({"event_type": "HealingProposal", "title": name})

async def _some_new_path(name):
    """The regression this guard exists to catch."""
    await store_notification({"event_type": "HealingProposal", "title": name})
'''

_OTHER_EVENT = '''
async def _store_drift_notification(name):
    await store_notification({"event_type": "EndpointDrift", "title": name})
'''

EXPECTED_WRITERS = {
    ("healing/t3_notify.py", "notify_t3_proposal"),
    ("healing/router.py", "_store_synthesis_proposal"),
}


class TestTheGuardItself:

    def test_CONTROL_detects_a_third_writer(self, tmp_path):
        (tmp_path / "three.py").write_text(_THREE_WRITERS, encoding="utf-8")
        assert proposal_writers(tmp_path) == {
            ("three.py", "notify_t3_proposal"),
            ("three.py", "_store_synthesis_proposal"),
            ("three.py", "_some_new_path"),
        }

    def test_CONTROL_is_silent_on_another_event_type(self, tmp_path):
        """No false positives, or the guard is noise. EndpointDrift has its own
        writer and its own guard in test_endpoint_drift_storm.py."""
        (tmp_path / "other.py").write_text(_OTHER_EVENT, encoding="utf-8")
        assert proposal_writers(tmp_path) == set()

    def test_exactly_two_deduped_writers_in_the_package(self):
        assert proposal_writers(PKG) == EXPECTED_WRITERS, (
            "HealingProposal rows must be banked from exactly two functions, "
            "because exactly those two hold the dedupe. Found: %s"
            % sorted(proposal_writers(PKG))
        )

    def test_both_writers_consult_the_dedupe(self):
        """Pairs with the test above: two writers are only worth pinning if
        BOTH of them are the deduped ones."""
        for relpath, func_name in sorted(EXPECTED_WRITERS):
            body = function_source(PKG / relpath, func_name)
            assert "has_pending_notification" in body, (
                "%s::%s does not consult the dedupe predicate" % (relpath, func_name))
            assert 'metadata_key="constant_name"' in body, (
                "%s::%s must dedupe on constant_name, not the job_id default"
                % (relpath, func_name))
            assert '"HealingProposal"' in body, (
                "%s::%s must dedupe on its OWN event type" % (relpath, func_name))
