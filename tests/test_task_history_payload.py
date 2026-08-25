"""Tests for the naukri_task_history payload contract.

Every test is PURE: no network, no browser, no file I/O, no live database.
The database read is patched with a plain in-memory list of rows.

WHAT THESE GUARD, and why each one can fail
-------------------------------------------
Measured against the live naukri.db on 2026-08-25, before the fix:

  * `naukri_task_history(limit=25)` returned 15,168 bytes on the wire
    (~3,800 tokens) to report 25 scheduled runs, 20 of which said the same
    thing: `confirmed: 0`.
  * The storage site capped each result with `json.dumps(result)[:2000]`.
    A slice of a JSON document is not a JSON document. 137 of the 1,585 rows
    had hit that cap, and ALL 137 were unparseable -- 137 of 137. A
    reminder_check row ended `..."remind_at": "2026-03-17T` and raised
    `Unterminated string`. It was at once the biggest thing in the table and
    the only thing in it that could not be read.

Each test below carries an explicit CONTROL showing the old behaviour failing
the same assertion, so none of these can silently become a check that cannot
fail.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from naukri_server.scheduler import (
    RESULT_STORAGE_CAP_BYTES,
    reduce_result_for_storage,
    salvage_json,
    summarize_result_blob,
)
from naukri_server.tools.scheduler_tool import (
    HISTORY_MAX_LIMIT,
    HISTORY_PAYLOAD_CAP_BYTES,
    _scheduler_history,
    _scheduler_one_run,
)

# pytest.ini sets asyncio_mode = auto, so async tests need no marker and sync
# ones must not carry a module-level asyncio mark.


# =====================================================================
# Fixtures -- shapes copied from the live table, not invented
# =====================================================================

def _reminder_result(n=50):
    """The shape that produced every severed reminder_check row."""
    return {
        "status": "success",
        "total": n,
        "due_count": n,
        "reminders": [
            {
                "job_id": f"0203265026{i:02d}",
                "title": "Staff Software Engineer - NodeJS/GraphQL - Sports Team",
                "company": "Warner Bros. Discovery",
                "remind_at": "2026-03-17T05:17:02.650165+00:00",
                "note": "Follow up on application to unknown",
                "is_due": True,
                "days_until_due": -161,
                "created_at": "2026-03-03T05:17:02.650165+00:00",
            }
            for i in range(n)
        ],
    }


def _row(run_id, task, started, status="completed", result=None, error=None,
         duration=10.0):
    return {
        "id": run_id,
        "task_name": task,
        "started_at": started,
        "finished_at": started,
        "status": status,
        "result": result,
        "error": error,
        "duration_ms": duration,
    }


def _patch_db(rows):
    return patch("naukri_server.database.list_scheduled_runs",
                 new_callable=AsyncMock, return_value=rows)


def _old_storage(result):
    """EXACTLY what scheduler.py:344 used to do. The control."""
    result_json = json.dumps(result) if isinstance(result, dict) else str(result)
    return result_json[:2000]


# =====================================================================
# 1. No severed JSON can be stored
# =====================================================================

class TestStorageNeverSevers:

    def test_oversized_result_stores_valid_json(self):
        """The exact live shape that produced 137 unparseable rows."""
        result = _reminder_result(50)

        stored = reduce_result_for_storage(result)
        json.loads(stored)  # must not raise
        assert len(stored) <= RESULT_STORAGE_CAP_BYTES

        # CONTROL: the old path produces a document that cannot be parsed.
        with pytest.raises(ValueError):
            json.loads(_old_storage(result))

    def test_cut_point_sweep_never_produces_invalid_json(self):
        """Sweep the payload length across the cap boundary.

        The old slice failed whenever the cut landed inside a string, which is
        most offsets. Walking the length one byte at a time puts the cut at
        every possible offset in turn.
        """
        old_failures = 0
        for pad in range(0, 240):
            result = {
                "status": "success",
                "total": pad,
                "items": [{"name": "x" * 40, "note": "a,b{c}d\"e", "n": i}
                          for i in range(30)],
                "tail": "y" * pad,
            }
            stored = reduce_result_for_storage(result)
            json.loads(stored)  # must not raise, at any offset
            assert len(stored) <= RESULT_STORAGE_CAP_BYTES

            try:
                json.loads(_old_storage(result))
            except ValueError:
                old_failures += 1

        # CONTROL: the old path was broken across essentially the whole sweep.
        assert old_failures > 200, (
            f"control is not exercising the defect: only {old_failures} old failures")

    def test_non_dict_result_stores_valid_json(self):
        """A non-dict result used to be stored as a Python repr, not JSON."""
        for value in ([1, 2, 3], "plain text", None, 42, True):
            stored = reduce_result_for_storage(value)
            json.loads(stored)  # must not raise

        # CONTROL: `str([1, 2, 3])` is "[1, 2, 3]" which happens to parse, but
        # a list of strings uses single quotes and does not.
        with pytest.raises(ValueError):
            json.loads(_old_storage(["a", "b"]))

    def test_reduction_reports_what_it_dropped(self):
        """Dropped data is COUNTED, never silently lost."""
        stored = json.loads(reduce_result_for_storage(_reminder_result(50)))

        assert stored["truncated"] is True
        assert stored["original_bytes"] > RESULT_STORAGE_CAP_BYTES
        # The scalars that carry the meaning survive intact.
        assert stored["status"] == "success"
        assert stored["total"] == 50
        assert stored["due_count"] == 50
        # The list is gone but its size is not.
        assert stored["reminders"]["omitted"] == "list"
        assert stored["reminders"]["items"] == 50
        # And one derived fact the list used to hold.
        assert stored["reminders"]["days_until_due_min"] == -161

    def test_pathological_width_still_fits_and_parses(self):
        """Hundreds of long scalar keys -- pass 1 cannot help, pass 2/3 must."""
        result = {f"field_{i}": "v" * 300 for i in range(400)}
        stored = reduce_result_for_storage(result)
        parsed = json.loads(stored)
        assert len(stored) <= RESULT_STORAGE_CAP_BYTES
        assert parsed["truncated"] is True
        assert parsed["original_bytes"] > RESULT_STORAGE_CAP_BYTES

    def test_small_result_is_stored_verbatim(self):
        """The common case must not be disturbed."""
        result = {"status": "completed", "confirmed": 0, "reverted": 0}
        assert json.loads(reduce_result_for_storage(result)) == result

    def test_unserializable_result_still_stores_valid_json(self):
        class Opaque:
            def __repr__(self):
                return "<opaque>"

        json.loads(reduce_result_for_storage({"status": "ok", "obj": Opaque()}))


# =====================================================================
# 2. The summary default carries no result blob
# =====================================================================

class TestSummaryDefaultCarriesNoBlob:

    async def test_listing_contains_no_result_field_anywhere(self):
        blob = json.dumps(_reminder_result(50))
        rows = [_row(100 + i, f"task_{i}", f"2026-08-25T1{i}:00:00", result=blob)
                for i in range(9)]

        with _patch_db(rows):
            result = await _scheduler_history(limit=20)

        for run in result["runs"]:
            assert "result" not in run
            assert "raw_result" not in run

        # Nothing anywhere in the serialised payload either.
        payload = json.dumps(result)
        assert '"result"' not in payload
        assert "Warner Bros" not in payload

        # CONTROL: the old shape returned every column of every row.
        old_payload = json.dumps({"status": "success", "total": len(rows), "runs": rows})
        assert '"result"' in old_payload
        assert "Warner Bros" in old_payload
        assert len(payload) < len(old_payload) / 4

    async def test_summary_replaces_the_blob_with_counts(self):
        """The summary must be strictly more informative than the blob was."""
        stored = reduce_result_for_storage(_reminder_result(50))
        rows = [_row(1, "reminder_check", "2026-08-25T14:44:01", result=stored)]

        with _patch_db(rows):
            result = await _scheduler_history(limit=20)

        summary = result["runs"][0]["summary"]
        assert "total=50" in summary
        assert "due_count=50" in summary
        assert "oldest overdue 161d" in summary
        assert len(summary) < 200

    async def test_listing_is_bounded_regardless_of_limit(self):
        """An AGGREGATE cap, not only a per-row one.

        A per-row cap is not a bound: 100 rows x 2000 chars is 200,000 chars.
        """
        blob = json.dumps(_reminder_result(50))
        rows = [_row(i, f"task_{i}", f"2026-08-25T{i:02d}:00:00", result=blob)
                for i in range(100)]

        with _patch_db(rows):
            result = await _scheduler_history(limit=100)

        assert len(json.dumps(result)) <= HISTORY_PAYLOAD_CAP_BYTES + 500
        # CONTROL: unbounded, the same rows are two orders of magnitude bigger.
        assert len(json.dumps({"runs": rows})) > 100_000

    async def test_elided_rows_are_reported_not_silently_dropped(self):
        rows = [_row(i, f"task_{i}", f"2026-08-25T{i:02d}:00:00",
                     result=json.dumps({"status": "ok", "note": "n" * 400}))
                for i in range(100)]

        with _patch_db(rows):
            result = await _scheduler_history(limit=100)

        assert result["elided"] > 0
        assert "payload cap" in result["elided_reason"]
        assert result["elided"] == 100 - len(result["runs"])

    async def test_limit_is_clamped_and_the_clamp_is_reported(self):
        rows = [_row(1, "t", "2026-08-25T10:00:00")]
        with _patch_db(rows) as mock_list:
            result = await _scheduler_history(limit=10_000)

        assert mock_list.await_args.kwargs["limit"] == HISTORY_MAX_LIMIT
        assert result["limit_clamped_to"] == HISTORY_MAX_LIMIT

    async def test_zero_and_negative_limits_do_not_explode(self):
        rows = [_row(1, "t", "2026-08-25T10:00:00")]
        for bad in (0, -5):
            with _patch_db(rows):
                result = await _scheduler_history(limit=bad)
            assert result["status"] == "success"


# =====================================================================
# 3. The opt-in returns exactly ONE run's result
# =====================================================================

class TestSingleRunOptIn:

    async def test_run_id_returns_exactly_one_run_with_its_result(self):
        rows = [
            _row(1, "reminder_check", "2026-08-25T14:00:00",
                 result=json.dumps({"status": "success", "total": 7, "who": "first"})),
            _row(2, "stale_check", "2026-08-25T15:00:00",
                 result=json.dumps({"status": "success", "total": 9, "who": "second"})),
            _row(3, "agent_cycle", "2026-08-25T16:00:00",
                 result=json.dumps({"status": "skipped", "who": "third"})),
        ]

        with _patch_db(rows):
            result = await _scheduler_one_run(2)

        assert result["total"] == 1
        assert "runs" not in result           # a single run, never a list
        assert result["run"]["id"] == 2
        assert result["run"]["result"]["who"] == "second"
        # Exactly one run's payload -- the siblings are absent.
        payload = json.dumps(result)
        assert "first" not in payload
        assert "third" not in payload

    async def test_run_id_cannot_be_used_to_dump_everything(self):
        """There is no run_id value that returns more than one run."""
        rows = [_row(i, "t", f"2026-08-25T{i:02d}:00:00",
                     result=json.dumps({"n": i})) for i in range(1, 20)]

        for probe in (None, 0, -1, 999999):
            with _patch_db(rows):
                result = await _scheduler_history(run_id=probe) if probe is not None \
                    else await _scheduler_history()
            if probe is None:
                continue
            # Every non-matching probe is an error, never a dump.
            assert result.get("error_code") == "NOT_FOUND"
            assert "run" not in result
            assert "runs" not in result

    async def test_unknown_run_id_says_so(self):
        with _patch_db([_row(1, "t", "2026-08-25T10:00:00")]):
            result = await _scheduler_one_run(4242)
        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"

    async def test_run_id_takes_precedence_over_listing(self):
        rows = [_row(1, "t", "2026-08-25T10:00:00", result=json.dumps({"n": 1}))]
        with _patch_db(rows):
            result = await _scheduler_history(limit=50, run_id=1)
        assert result["total"] == 1
        assert result["run"]["id"] == 1


# =====================================================================
# 4. Collapsing must never hide an anomaly
# =====================================================================

class TestCollapseSafety:

    async def test_identical_consecutive_runs_fold_with_a_count(self):
        rows = [_row(i, "t2_verify_pending", f"2026-08-25T{i:02d}:00:00",
                     result=json.dumps({"status": "completed", "confirmed": 0}))
                for i in range(20, 8, -1)]

        with _patch_db(rows):
            result = await _scheduler_history(limit=20)

        assert len(result["runs"]) == 1
        assert result["runs"][0]["runs"] == 12
        assert result["runs_covered"] == 12
        assert result["runs"][0]["latest_id"] == 20   # drill-in still reachable
        assert "collapsed" in result

    async def test_a_failed_run_is_never_absorbed_into_a_group(self):
        """The one property that makes collapsing safe."""
        rows = (
            [_row(i, "t2", f"2026-08-25T{i:02d}:00:00",
                  result=json.dumps({"confirmed": 0})) for i in (20, 19, 18)]
            + [_row(17, "t2", "2026-08-25T17:00:00", status="failed",
                    result=None, error="boom")]
            + [_row(i, "t2", f"2026-08-25T{i:02d}:00:00",
                    result=json.dumps({"confirmed": 0})) for i in (16, 15)]
        )

        with _patch_db(rows):
            result = await _scheduler_history(limit=20)

        failed = [r for r in result["runs"] if r.get("status") == "failed"]
        assert len(failed) == 1
        assert failed[0]["error"] == "boom"
        assert failed[0]["id"] == 17          # its own row, with its own id
        assert result["runs_covered"] == 6    # nothing lost

    async def test_a_differing_verdict_is_never_absorbed(self):
        rows = (
            [_row(3, "t2", "2026-08-25T13:00:00",
                  result=json.dumps({"confirmed": 0}))]
            + [_row(2, "t2", "2026-08-25T12:00:00",
                    result=json.dumps({"confirmed": 7}))]
            + [_row(1, "t2", "2026-08-25T11:00:00",
                    result=json.dumps({"confirmed": 0}))]
        )

        with _patch_db(rows):
            result = await _scheduler_history(limit=20)

        assert len(result["runs"]) == 3
        assert any("confirmed=7" in (r.get("summary") or "") for r in result["runs"])

    async def test_collapse_false_gives_one_row_per_run(self):
        rows = [_row(i, "t2", f"2026-08-25T{i:02d}:00:00",
                     result=json.dumps({"confirmed": 0})) for i in (13, 12, 11)]

        with _patch_db(rows):
            result = await _scheduler_history(limit=20, collapse=False)

        assert len(result["runs"]) == 3
        assert all("id" in r for r in result["runs"])


# =====================================================================
# 5. Legacy severed rows are salvaged, not just reported dead
# =====================================================================

class TestSalvageOfLegacyRows:

    def test_severed_blob_recovers_its_leading_counts(self):
        severed = _old_storage(_reminder_result(50))

        # CONTROL: this is what the live table holds, and it does not parse.
        with pytest.raises(ValueError):
            json.loads(severed)

        obj, state = salvage_json(severed)
        assert state == "salvaged"
        assert obj["total"] == 50
        assert obj["due_count"] == 50
        assert isinstance(obj["reminders"], list)

    def test_salvage_is_flagged_in_the_summary(self):
        summary = summarize_result_blob(_old_storage(_reminder_result(50)))
        assert "salvaged" in summary
        assert "total=50" in summary

    def test_intact_blob_is_not_reported_as_salvaged(self):
        obj, state = salvage_json(json.dumps({"status": "ok", "n": 1}))
        assert state == "ok"
        assert obj["n"] == 1

    def test_unsalvageable_blob_is_reported_not_crashed(self):
        obj, state = salvage_json('{"status": "unterminat')
        assert state == "unreadable"
        assert obj is None
        assert "unreadable" in summarize_result_blob('{"status": "unterminat')

    def test_empty_and_none_blobs_are_handled(self):
        assert summarize_result_blob(None) is None
        assert summarize_result_blob("") is None
        assert salvage_json(None) == (None, "unreadable")
