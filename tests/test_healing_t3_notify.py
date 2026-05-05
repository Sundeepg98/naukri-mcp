"""Tests for naukri_server.healing.t3_notify — diff + notification, never auto-fix."""

from unittest.mock import AsyncMock, patch

import pytest

from naukri_server.healing import t3_notify
from naukri_server.healing.t3_notify import build_unified_diff, notify_t3_proposal


# ---------------------------------------------------------------------------
# build_unified_diff — pure function
# ---------------------------------------------------------------------------


def test_build_unified_diff_basic():
    diff = build_unified_diff(
        "naukri_server/tools/settings.py",
        "x = 1\ny = 2\n",
        "x = 1\ny = 3\n",
    )
    assert "--- a/naukri_server/tools/settings.py" in diff
    assert "+++ b/naukri_server/tools/settings.py" in diff
    assert "-y = 2" in diff
    assert "+y = 3" in diff


def test_build_unified_diff_identical_returns_empty():
    """No drift = empty diff. Caller uses this to skip the notification."""
    src = "x = 1\n"
    diff = build_unified_diff("f.py", src, src)
    assert diff == ""


def test_build_unified_diff_respects_context_n():
    before = "\n".join(f"line {i}" for i in range(20)) + "\n"
    after = before.replace("line 10", "line ten")
    diff_n3 = build_unified_diff("f.py", before, after, n=3)
    diff_n0 = build_unified_diff("f.py", before, after, n=0)
    # Smaller n = fewer context lines = shorter diff
    assert len(diff_n0) < len(diff_n3)


# ---------------------------------------------------------------------------
# notify_t3_proposal — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_t3_proposal_persists_notification():
    """Happy path: diff built, store_notification called with correct payload."""
    captured = {}

    async def fake_store(notif):
        captured.update(notif)
        return 42

    with patch("naukri_server.database.store_notification",
               new=AsyncMock(side_effect=fake_store)):
        out = await notify_t3_proposal(
            "APPLY_WORKFLOW_API",
            file_path="naukri_server/tools/apply.py",
            before_source="WORKFLOW = '/old'\n",
            after_source="WORKFLOW = '/new'\n",
            drift_summary="severity=removed, 2 fields changed",
        )

    assert out.delivered is True
    assert out.notification_id == 42
    assert out.constant_name == "APPLY_WORKFLOW_API"

    assert captured["event_type"] == "HealingProposal"
    assert "APPLY_WORKFLOW_API" in captured["title"]
    assert "T3" in captured["title"]
    assert captured["priority"] == "high"
    assert "Proposed patch" in captured["body"]
    assert "WORKFLOW = '/new'" in captured["body"]
    assert "severity=removed" in captured["body"]
    # Metadata block carries enough for the daily-brief renderer
    md = captured["metadata"]
    assert md["constant_name"] == "APPLY_WORKFLOW_API"
    assert md["tier"] == "T3"
    assert md["file_path"].endswith("apply.py")


# ---------------------------------------------------------------------------
# notify_t3_proposal — guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_t3_proposal_refuses_t1_endpoint():
    """A T1 constant must not reach this path — would mask a router bug."""
    out = await notify_t3_proposal(
        "NOTIFICATION_FEED_API",
        file_path="x.py", before_source="a\n", after_source="b\n",
    )
    assert out.delivered is False
    assert "T1" in (out.skipped_reason or "")


@pytest.mark.asyncio
async def test_notify_t3_proposal_refuses_t2_endpoint():
    out = await notify_t3_proposal(
        "SEARCH_API",
        file_path="x.py", before_source="a\n", after_source="b\n",
    )
    assert out.delivered is False
    assert "T2" in (out.skipped_reason or "")


@pytest.mark.asyncio
async def test_notify_t3_proposal_unmapped_constant():
    out = await notify_t3_proposal(
        "BOGUS_API",
        file_path="x.py", before_source="a\n", after_source="b\n",
    )
    assert out.delivered is False
    assert "no tier mapping" in (out.skipped_reason or "")


@pytest.mark.asyncio
async def test_notify_t3_proposal_skips_empty_diff():
    """If proposed patch produces no diff, don't send a noop notification."""
    out = await notify_t3_proposal(
        "APPLY_WORKFLOW_API",
        file_path="x.py", before_source="same\n", after_source="same\n",
    )
    assert out.delivered is False
    assert "no diff" in (out.skipped_reason or "")


# ---------------------------------------------------------------------------
# notify_t3_proposal — failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_t3_proposal_handles_store_failure():
    """If store_notification raises, return error — never propagate to caller."""
    async def bad_store(_notif):
        raise RuntimeError("disk full")

    with patch("naukri_server.database.store_notification",
               new=AsyncMock(side_effect=bad_store)):
        out = await notify_t3_proposal(
            "APPLY_WORKFLOW_API",
            file_path="x.py", before_source="a\n", after_source="b\n",
        )

    assert out.delivered is False
    assert "store_notification" in (out.error or "")
    assert "disk full" in (out.error or "")
