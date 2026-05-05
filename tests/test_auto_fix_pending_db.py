"""Tests for the auto_fix_pending table CRUD helpers in naukri_server.database."""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest


async def _init_db_in(tmp_path):
    """Initialize a fresh DB at tmp_path/naukri.db and return the path."""
    db_path = tmp_path / "naukri.db"
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import init_db
        await init_db()
    return db_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_insert_returns_row_id(tmp_path):
    db_path = await _init_db_in(tmp_path)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import insert_auto_fix_pending
        rid = await insert_auto_fix_pending(
            commit_sha="abc123",
            pre_fix_sha="def456",
            constant_name="SEARCH_API",
            applied_at=_now_iso(),
            verify_at=_now_iso(),
        )
    assert isinstance(rid, int) and rid > 0


@pytest.mark.asyncio
async def test_get_returns_row(tmp_path):
    db_path = await _init_db_in(tmp_path)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import insert_auto_fix_pending, get_auto_fix_pending
        rid = await insert_auto_fix_pending(
            commit_sha="abc", pre_fix_sha="pre", constant_name="X_API",
            applied_at=_now_iso(), verify_at=_now_iso(),
        )
        row = await get_auto_fix_pending(rid)
    assert row is not None
    assert row["commit_sha"] == "abc"
    assert row["pre_fix_sha"] == "pre"
    assert row["constant_name"] == "X_API"
    assert row["status"] == "pending"
    assert row["error"] is None


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown(tmp_path):
    db_path = await _init_db_in(tmp_path)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import get_auto_fix_pending
        row = await get_auto_fix_pending(99999)
    assert row is None


@pytest.mark.asyncio
async def test_list_filters_by_status(tmp_path):
    db_path = await _init_db_in(tmp_path)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import (
            insert_auto_fix_pending, list_auto_fix_pending, update_auto_fix_status,
        )
        r1 = await insert_auto_fix_pending(
            commit_sha="c1", pre_fix_sha="p1", constant_name="A",
            applied_at=_now_iso(), verify_at=_now_iso(),
        )
        r2 = await insert_auto_fix_pending(
            commit_sha="c2", pre_fix_sha="p2", constant_name="B",
            applied_at=_now_iso(), verify_at=_now_iso(),
        )
        await update_auto_fix_status(r2, "confirmed")

        pending = await list_auto_fix_pending(status="pending")
        confirmed = await list_auto_fix_pending(status="confirmed")

    assert len(pending) == 1 and pending[0]["commit_sha"] == "c1"
    assert len(confirmed) == 1 and confirmed[0]["commit_sha"] == "c2"


@pytest.mark.asyncio
async def test_list_filters_by_due_before(tmp_path):
    """Only rows whose verify_at is on/before the cutoff should be returned."""
    db_path = await _init_db_in(tmp_path)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import insert_auto_fix_pending, list_auto_fix_pending

        # Row 1: due 1h ago (should match cutoff = now)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        # Row 2: due 1h in future (should NOT match)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        await insert_auto_fix_pending(
            commit_sha="due", pre_fix_sha="p1", constant_name="A",
            applied_at=_now_iso(), verify_at=past,
        )
        await insert_auto_fix_pending(
            commit_sha="not_due", pre_fix_sha="p2", constant_name="B",
            applied_at=_now_iso(), verify_at=future,
        )
        due = await list_auto_fix_pending(status="pending", due_before=_now_iso())

    assert len(due) == 1
    assert due[0]["commit_sha"] == "due"


@pytest.mark.asyncio
async def test_update_records_error_message(tmp_path):
    db_path = await _init_db_in(tmp_path)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import (
            insert_auto_fix_pending, update_auto_fix_status, get_auto_fix_pending,
        )
        rid = await insert_auto_fix_pending(
            commit_sha="x", pre_fix_sha="y", constant_name="Z",
            applied_at=_now_iso(), verify_at=_now_iso(),
        )
        await update_auto_fix_status(rid, "revert_failed", error="boom: detached HEAD")
        row = await get_auto_fix_pending(rid)

    assert row["status"] == "revert_failed"
    assert row["error"] == "boom: detached HEAD"


@pytest.mark.asyncio
async def test_list_respects_limit(tmp_path):
    db_path = await _init_db_in(tmp_path)
    with patch("naukri_server.database.DB_PATH", db_path):
        from naukri_server.database import insert_auto_fix_pending, list_auto_fix_pending
        for i in range(5):
            await insert_auto_fix_pending(
                commit_sha=f"c{i}", pre_fix_sha=f"p{i}", constant_name="N",
                applied_at=_now_iso(), verify_at=_now_iso(),
            )
        rows = await list_auto_fix_pending(limit=3)
    assert len(rows) == 3
