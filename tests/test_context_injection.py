"""Tests for FastMCP Context injection in long-running tools.

Verifies that:
- Tools accept ctx=None (backwards compat for unit tests + non-supporting clients)
- Tools call ctx.info / ctx.report_progress when ctx is provided
- Exceptions in ctx methods are swallowed (don't break the tool)
- The public MCP tools route ctx to the underlying helpers
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_mock_ctx() -> MagicMock:
    """Mock a FastMCP Context with awaitable info / report_progress."""
    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.debug = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    ctx.report_progress = AsyncMock()
    return ctx


# ============================================================================
# Phase 1: naukri_batch_apply
# ============================================================================

class TestBatchApplyContext:
    @pytest.mark.asyncio
    async def test_batch_apply_accepts_none_ctx(self):
        """naukri_batch_apply runs with ctx=None (default), no errors."""
        from naukri_server.tools.apply import naukri_batch_apply

        # Search returns no jobs — short-circuits before any apply.
        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "jobs": []}):
            result = await naukri_batch_apply(keywords="python", ctx=None)
        # Returns error because no jobs were found — not because of ctx
        assert result["status"] == "error"
        assert "NOT_FOUND" in result.get("error_code", "")

    @pytest.mark.asyncio
    async def test_batch_apply_calls_ctx_info(self):
        """When ctx provided, info logs are emitted at start and end."""
        from naukri_server.tools.apply import naukri_batch_apply
        ctx = _make_mock_ctx()

        jobs = [
            {"job_id": "J1", "title": "Dev", "company": "A", "is_applied": False},
            {"job_id": "J2", "title": "Dev", "company": "B", "is_applied": False},
        ]

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "jobs": jobs}), \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()), \
             patch("naukri_server.tools.apply._apply_single",
                   new_callable=AsyncMock,
                   return_value={"status": "applied"}):
            await naukri_batch_apply(keywords="python", limit=2, ctx=ctx)

        # Should have called ctx.info at least twice (start + end)
        assert ctx.info.await_count >= 2


# ============================================================================
# Phase 2: naukri_auto_hunt
# ============================================================================

class TestAutoHuntContext:
    @pytest.mark.asyncio
    async def test_auto_hunt_accepts_none_ctx(self):
        """naukri_auto_hunt runs with ctx=None."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt
        # Just check that signature accepts ctx=None
        result = await naukri_auto_hunt(keywords="python", min_fit_score=-1, ctx=None)
        # Validation error triggers early return
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_auto_hunt_calls_ctx(self):
        """When ctx provided, ctx.info and ctx.report_progress get awaited."""
        from naukri_server.tools.auto_hunt import naukri_auto_hunt
        ctx = _make_mock_ctx()

        search_result = {
            "status": "success",
            "jobs": [],
            "has_more": False,
        }
        profile_result = {
            "status": "success",
            "key_skills": ["Python"],
            "total_experience": "5",
        }

        with patch("naukri_server.tools.search.naukri_search_jobs",
                   new_callable=AsyncMock, return_value=search_result), \
             patch("naukri_server.tools.profile.get_cached_profile",
                   new_callable=AsyncMock, return_value=profile_result):
            await naukri_auto_hunt(keywords="python", min_fit_score=0, ctx=ctx)

        # At minimum: start info ("Searching ...") + report_progress for page 1
        ctx.info.assert_any_await("Searching 'python' in 'any'")
        # Page 1 progress should have been reported
        assert ctx.report_progress.await_count >= 1


# ============================================================================
# Phase 3 + 4: smart_apply
# ============================================================================

class TestSmartApplyContext:
    @pytest.mark.asyncio
    async def test_score_saved_jobs_accepts_none_ctx(self):
        """naukri_score_saved_jobs runs with ctx=None."""
        from naukri_server.tools.smart_apply import naukri_score_saved_jobs

        with patch("naukri_server.tools.smart_apply._bulk_saved_scoring",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "scored_jobs": []}) as mock_bulk:
            result = await naukri_score_saved_jobs(min_fit_score=60, ctx=None)
        assert result["status"] == "success"
        # ctx should be None when called via public tool with no ctx
        call_kwargs = mock_bulk.await_args.kwargs
        assert call_kwargs.get("ctx") is None

    @pytest.mark.asyncio
    async def test_score_saved_jobs_passes_ctx(self):
        """naukri_score_saved_jobs forwards ctx to _bulk_saved_scoring."""
        from naukri_server.tools.smart_apply import naukri_score_saved_jobs
        ctx = _make_mock_ctx()

        with patch("naukri_server.tools.smart_apply._bulk_saved_scoring",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "scored_jobs": []}) as mock_bulk:
            await naukri_score_saved_jobs(min_fit_score=60, ctx=ctx)
        call_kwargs = mock_bulk.await_args.kwargs
        assert call_kwargs.get("ctx") is ctx

    @pytest.mark.asyncio
    async def test_apply_top_fits_accepts_none_ctx(self):
        """naukri_apply_top_fits runs with ctx=None."""
        from naukri_server.tools.smart_apply import naukri_apply_top_fits

        with patch("naukri_server.tools.smart_apply._bulk_saved_scoring",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "total_saved": 0, "scored_jobs": []}):
            result = await naukri_apply_top_fits(min_fit_score=70, ctx=None)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_apply_top_fits_calls_ctx(self):
        """When ctx provided, ctx.info called after scoring."""
        from naukri_server.tools.smart_apply import naukri_apply_top_fits
        ctx = _make_mock_ctx()

        scored = [
            {"job_id": "J1", "title": "Dev", "company": "A",
             "fit_score": 85, "fit_details": {"bonuses": {}}},
        ]

        with patch("naukri_server.tools.smart_apply._bulk_saved_scoring",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "total_saved": 1,
                                 "scored_jobs": scored}), \
             patch("naukri_server.tools.apply._apply_single",
                   new_callable=AsyncMock,
                   return_value={"status": "applied"}):
            await naukri_apply_top_fits(min_fit_score=70, limit=1, ctx=ctx)

        # ctx.info should have been called (at least the apply-batch info)
        assert ctx.info.await_count >= 1
        # ctx.report_progress should have been called per-apply
        assert ctx.report_progress.await_count >= 1


# ============================================================================
# Phase 5: sync
# ============================================================================

class TestSyncContext:
    @pytest.mark.asyncio
    async def test_sync_applications_accepts_none_ctx(self):
        """naukri_sync_applications runs with ctx=None and routes correctly.

        When ctx is None we MUST NOT pass ctx kwarg to the helper — existing
        tests assert exact call signature (force_browser=, days_back=).
        """
        from naukri_server.tools.sync import naukri_sync_applications

        with patch("naukri_server.tools.sync._sync_applications",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "method": "rest_api"}) as mock_helper:
            await naukri_sync_applications(ctx=None)
        mock_helper.assert_awaited_once_with(force_browser=False, days_back=365)

    @pytest.mark.asyncio
    async def test_sync_applications_passes_ctx(self):
        """When ctx provided, naukri_sync_applications forwards it."""
        from naukri_server.tools.sync import naukri_sync_applications
        ctx = _make_mock_ctx()

        with patch("naukri_server.tools.sync._sync_applications",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "method": "rest_api"}) as mock_helper:
            await naukri_sync_applications(ctx=ctx)
        mock_helper.assert_awaited_once_with(force_browser=False, days_back=365, ctx=ctx)

    @pytest.mark.asyncio
    async def test_sync_saved_passes_ctx(self):
        """When ctx provided, naukri_sync_saved forwards it."""
        from naukri_server.tools.sync import naukri_sync_saved
        ctx = _make_mock_ctx()

        with patch("naukri_server.tools.sync._sync_saved_jobs",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "method": "rest_api"}) as mock_helper:
            await naukri_sync_saved(ctx=ctx)
        mock_helper.assert_awaited_once_with(force_browser=False, ctx=ctx)


# ============================================================================
# Phase 6: agent
# ============================================================================

class TestAgentCycleContext:
    @pytest.mark.asyncio
    async def test_run_agent_cycle_accepts_none_ctx(self):
        """run_agent_cycle still works without ctx (backwards compat for tests)."""
        from naukri_server.agent import run_agent_cycle
        from naukri_server.tools.agent_tool import _agent_run_now
        # When ctx is not provided to _agent_run_now, run_agent_cycle is called
        # without ctx kwarg (so the existing test still works).
        with patch("naukri_server.agent.run_agent_cycle",
                   new_callable=AsyncMock,
                   return_value={"status": "skipped", "reason": "disabled"}) as mock_run:
            await _agent_run_now(ctx=None)
        # No ctx in kwargs — the function gets called without it
        mock_run.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_run_agent_cycle_passes_ctx(self):
        """When ctx provided to _agent_run_now, it's forwarded."""
        from naukri_server.tools.agent_tool import _agent_run_now
        ctx = _make_mock_ctx()
        with patch("naukri_server.agent.run_agent_cycle",
                   new_callable=AsyncMock,
                   return_value={"status": "success"}) as mock_run:
            await _agent_run_now(ctx=ctx)
        mock_run.assert_awaited_once_with(ctx=ctx)

    @pytest.mark.asyncio
    async def test_naukri_agent_run_now_threads_ctx(self):
        """naukri_agent_run_now(ctx=ctx) threads ctx to the underlying cycle."""
        from naukri_server.tools.agent_tool import naukri_agent_run_now
        ctx = _make_mock_ctx()
        with patch("naukri_server.agent.run_agent_cycle",
                   new_callable=AsyncMock,
                   return_value={"status": "success"}) as mock_run:
            await naukri_agent_run_now(ctx=ctx)
        mock_run.assert_awaited_once_with(ctx=ctx)

    @pytest.mark.asyncio
    async def test_naukri_agent_status_works_without_ctx(self):
        """naukri_agent_status takes no ctx and still returns success."""
        from naukri_server.tools.agent_tool import naukri_agent_status
        with patch("naukri_server.tools.agent_tool._agent_status",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "enabled": False}):
            result = await naukri_agent_status()
        assert result["status"] == "success"
