"""A cap is not a count: `pending_notifications` must not publish its LIMIT.

REPRODUCED LIVE 2026-08-21. `naukri_daily_brief().pending_notifications` was a
LIST of at most ten rows, so `len()` read 10 and looked like a total. It was
not: `naukri_health_check` reported 156 undelivered from
`count_undelivered_notifications()` at the same moment. A reviewer read the ten
and concluded a prune had over-deleted.

Same defect family as `recruiter_history` publishing its SQL `LIMIT 20` as
`total_companies`. The fix is not a bigger limit -- it is naming the cap as a
cap and reporting the real total beside it.

ORDERING IS LOAD-BEARING. `_fetch_pending_notifications` MARKS the rows it
returns as delivered. A total counted after that mark is a different number
than the one the caller is being shown a slice of, so the count must happen
BEFORE the mark. `test_the_total_is_counted_before_delivery_not_after` is the
regression guard for exactly that.

These tests use the session-scoped isolated DB from conftest
(`_isolated_test_db`); the `clean_notifications` fixture empties the table so
each test owns the totals it asserts on. No network, no browser.
"""

from datetime import datetime, timezone

import pytest

# pytest.ini sets asyncio_mode = auto, so async tests need no marker and a
# module-level one would only warn on the sync tests below.


@pytest.fixture
async def clean_notifications():
    """Empty the notifications table so a test owns every count it asserts."""
    from naukri_server.database import get_db

    async def _wipe():
        db = await get_db()
        try:
            await db.execute("DELETE FROM notifications")
            await db.commit()
        finally:
            await db.close()

    await _wipe()
    yield
    await _wipe()


async def _seed(n, priority="medium", prefix="CAP"):
    """Insert `n` undelivered notifications. Returns their row ids."""
    from naukri_server.database import store_notification

    ids = []
    for i in range(n):
        ids.append(await store_notification({
            "event_type": "ApplicationStale",
            "title": "%s-%d" % (prefix, i),
            "body": "body %d" % i,
            "priority": priority,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {"job_id": "%s-%d" % (prefix, i)},
        }))
    return ids


class TestTheCapIsNamedAsACap:

    async def test_a_capped_fetch_reports_the_real_total_not_the_slice(
            self, clean_notifications):
        """The live defect: ten rows read as "ten undelivered". They were 156.

        Seeds strictly more than the cap so `delivered_count` and
        `total_undelivered` MUST disagree, and asserts the payload says so
        rather than leaving the caller to infer it from a list length.
        """
        from naukri_server.tools.daily_brief import (
            _fetch_pending_notifications,
            PENDING_NOTIFICATION_DELIVERY_CAP as CAP,
        )

        seeded = CAP + 7
        await _seed(seeded)

        out = await _fetch_pending_notifications()

        assert isinstance(out, dict), (
            "a bare list republishes the cap as a count -- that is the defect"
        )
        assert out["total_undelivered"] == seeded
        assert out["delivered_count"] == CAP
        assert out["total_undelivered"] > out["delivered_count"]
        assert out["capped"] is True
        assert out["still_undelivered"] == (
            out["total_undelivered"] - out["delivered_count"]
        )
        assert len(out["delivered"]) == out["delivered_count"]

    async def test_an_uncapped_fetch_says_capped_is_false(
            self, clean_notifications):
        """The control. `capped` must track the comparison, not be a constant."""
        from naukri_server.tools.daily_brief import (
            _fetch_pending_notifications,
            PENDING_NOTIFICATION_DELIVERY_CAP as CAP,
        )

        seeded = max(1, CAP - 3)
        await _seed(seeded)

        out = await _fetch_pending_notifications()

        assert out["total_undelivered"] == seeded
        assert out["delivered_count"] == seeded
        assert out["capped"] is False
        assert out["still_undelivered"] == 0

    async def test_the_delivery_cap_equals_the_limit_actually_passed(
            self, clean_notifications):
        """`delivery_cap` must be MEASURED from the call, not asserted alongside it.

        A published constant that drifts from the argument is the same class of
        lie as the count -- so this spies the real `limit=` keyword that reaches
        `list_undelivered_notifications` and compares the payload against it.
        """
        import naukri_server.tools.daily_brief as db_mod
        from naukri_server.database import list_undelivered_notifications

        seen = {}

        async def _spy(*args, **kwargs):
            seen["limit"] = kwargs.get("limit", args[0] if args else None)
            return await list_undelivered_notifications(**kwargs)

        await _seed(3)

        import naukri_server.database as database_mod
        real = database_mod.list_undelivered_notifications
        database_mod.list_undelivered_notifications = _spy
        try:
            out = await db_mod._fetch_pending_notifications()
        finally:
            database_mod.list_undelivered_notifications = real

        assert seen["limit"] is not None, "no limit was passed at all"
        assert out["delivery_cap"] == seen["limit"], (
            "the published cap (%r) is not the limit the query used (%r)"
            % (out["delivery_cap"], seen["limit"])
        )
        assert out["delivery_cap"] == db_mod.PENDING_NOTIFICATION_DELIVERY_CAP


class TestTheTotalIsCountedBeforeDelivery:

    async def test_the_total_is_counted_before_delivery_not_after(
            self, clean_notifications):
        """Count after the mark and the number moves under the caller.

        Delivery sets `delivered_via`, which is the very predicate
        `count_undelivered_notifications()` filters on. Counting afterwards
        would report `seeded - CAP` and call it the total -- a number that is
        neither the total nor the slice, and that shrinks every time the brief
        is run.
        """
        from naukri_server.tools.daily_brief import (
            _fetch_pending_notifications,
            PENDING_NOTIFICATION_DELIVERY_CAP as CAP,
        )
        from naukri_server.database import count_undelivered_notifications

        seeded = CAP + 5
        await _seed(seeded)

        before = await count_undelivered_notifications()
        assert before == seeded

        out = await _fetch_pending_notifications()

        after = await count_undelivered_notifications()
        assert after == seeded - CAP, "the fetch did not mark its rows delivered"

        assert out["total_undelivered"] == before, (
            "total_undelivered is the POST-delivery count (%r); it must be the "
            "PRE-delivery count (%r)" % (out["total_undelivered"], before)
        )
        assert out["total_undelivered"] != after

    async def test_the_ordering_is_enforced_by_call_sequence_not_by_luck(
            self, clean_notifications):
        """Records the actual order of the three DB calls and asserts it.

        The value assertion above can be satisfied by accident (e.g. by caching
        a stale count). This one names the requirement directly: COUNT happens
        before MARK.
        """
        import naukri_server.database as database_mod
        from naukri_server.tools.daily_brief import (
            _fetch_pending_notifications,
            PENDING_NOTIFICATION_DELIVERY_CAP as CAP,
        )

        order = []
        real_count = database_mod.count_undelivered_notifications
        real_mark = database_mod.mark_notifications_delivered

        async def _count(*a, **k):
            order.append("count")
            return await real_count(*a, **k)

        async def _mark(*a, **k):
            order.append("mark")
            return await real_mark(*a, **k)

        await _seed(CAP + 2)

        database_mod.count_undelivered_notifications = _count
        database_mod.mark_notifications_delivered = _mark
        try:
            await _fetch_pending_notifications()
        finally:
            database_mod.count_undelivered_notifications = real_count
            database_mod.mark_notifications_delivered = real_mark

        assert "count" in order, "the total was never counted"
        assert "mark" in order, "the rows were never marked delivered"
        assert order.index("count") < order.index("mark"), (
            "counted AFTER marking delivered -- the total moves under the "
            "caller; observed order: %r" % (order,)
        )


class TestTheConsumerReadsTheNewShape:
    """`build_recommended_actions` iterated the value directly.

    Handed a dict it would iterate KEYS -- strings -- and `"delivered".get`
    raises AttributeError, taking the whole daily brief down. Pure: no DB.
    """

    def _high(self):
        return {"priority": "high", "title": "Interview scheduled"}

    def test_a_high_priority_notification_still_surfaces_through_the_envelope(self):
        from naukri_server.services.daily_brief_service import (
            build_recommended_actions,
        )

        brief = {"pending_notifications": {
            "delivered": [self._high(), {"priority": "low", "title": "meh"}],
            "delivered_count": 2,
            "delivery_cap": 10,
            "capped": True,
            "total_undelivered": 42,
            "still_undelivered": 40,
        }}

        actions = build_recommended_actions(brief)
        hits = [a for a in actions if "high-priority notification" in a["action"]]
        assert hits, "the high-priority notification was dropped by the new shape"
        assert hits[0]["priority"] == "high"
        assert "Interview scheduled" in hits[0]["action"]

    def test_the_old_list_shape_still_works(self):
        """Defensive: a stored brief, or a caller mid-upgrade, still passes a list."""
        from naukri_server.services.daily_brief_service import (
            build_recommended_actions,
        )

        actions = build_recommended_actions(
            {"pending_notifications": [self._high()]})
        assert [a for a in actions if "high-priority notification" in a["action"]]

    def test_it_does_not_crash_on_a_dict_without_delivered(self):
        """The envelope key absent is a shape it must survive, not assume."""
        from naukri_server.services.daily_brief_service import (
            build_recommended_actions,
        )

        for shape in ({"total_undelivered": 3}, {}, None, []):
            actions = build_recommended_actions({"pending_notifications": shape})
            assert isinstance(actions, list)

    def test_a_dict_of_rows_is_not_iterated_as_keys(self):
        """The exact crash: iterating a dict yields str, and str has no .get.

        Guards the fix rather than the symptom -- a `for n in pending_notifs`
        left in place passes every other test in this class if `delivered` is
        the only key it ever sees.
        """
        from naukri_server.services.daily_brief_service import (
            build_recommended_actions,
        )

        brief = {"pending_notifications": {
            "delivered": [], "delivered_count": 0, "delivery_cap": 10,
            "capped": False, "total_undelivered": 0, "still_undelivered": 0,
        }}
        actions = build_recommended_actions(brief)  # must not raise
        assert not [a for a in actions if "high-priority notification" in a["action"]]


class TestOneNameOneMeaning:

    async def test_health_reports_a_total_under_a_name_that_says_total(
            self, clean_notifications):
        """`naukri_health_check` published a SCALAR count under the SAME key
        daily_brief used for an ENVELOPE. One name, two meanings, across two
        tools -- which is how the 10-versus-156 confusion survived being looked
        at twice. Health's key is `pending_notifications_total`.
        """
        from naukri_server.tools import health

        await _seed(4)

        result = {}
        from naukri_server.database import (
            get_event_stats, count_undelivered_notifications,
        )
        # Exercise the real block rather than the whole tool: naukri_health_check
        # touches the browser and the network, and this assertion is about a key
        # name and its value, both of which live in this snippet.
        event_stats = await get_event_stats(hours=24)
        notif_count = await count_undelivered_notifications()
        result["event_stats_24h"] = event_stats
        result["pending_notifications_total"] = notif_count

        assert result["pending_notifications_total"] == 4

        import inspect
        src = inspect.getsource(health)
        assert 'result["pending_notifications_total"] = notif_count' in src, (
            "health.py still writes the scalar under the envelope's name"
        )
        assert 'result["pending_notifications"] = notif_count' not in src


def test_the_cap_constant_is_named_not_a_literal():
    """A bare `10` in the call site is how a cap gets mistaken for a count."""
    import inspect

    import naukri_server.tools.daily_brief as db_mod

    assert isinstance(db_mod.PENDING_NOTIFICATION_DELIVERY_CAP, int)
    assert db_mod.PENDING_NOTIFICATION_DELIVERY_CAP > 0

    src = inspect.getsource(db_mod._fetch_pending_notifications)
    assert "limit=10" not in src, "the literal cap is still in the call site"
    assert "PENDING_NOTIFICATION_DELIVERY_CAP" in src
