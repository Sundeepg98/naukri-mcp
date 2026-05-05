"""MCP tools for controlling the autonomous job-hunting agent."""

import asyncio
import json
import logging

from mcp.server.fastmcp import Context

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action

logger = logging.getLogger(__name__)


async def _agent_status() -> dict:
    """Get agent configuration and recent run summary."""
    from naukri_server.agent import load_agent_config
    from naukri_server.database import list_agent_runs

    config = await asyncio.to_thread(load_agent_config)
    recent = await list_agent_runs(limit=5)
    return {
        "status": "success",
        "enabled": config.get("enabled", False),
        "mode": config.get("mode", "approval"),
        "max_daily": config.get("max_daily_applications", 15),
        "min_fit_score": config.get("min_fit_score", 70),
        "searches": len([s for s in config.get("searches", []) if s.get("enabled")]),
        "blocklist_companies": len(config.get("blocklist", {}).get("companies", [])),
        "quiet_hours": config.get("quiet_hours", {}),
        "recent_runs": [{
            "cycle_id": r.get("cycle_id"),
            "started_at": r.get("started_at"),
            "status": r.get("status"),
            "applied": r.get("applied_count"),
            "skipped": r.get("skipped_count"),
            "duration_ms": r.get("duration_ms"),
        } for r in recent],
    }


async def _agent_config() -> dict:
    """Get full agent configuration."""
    from naukri_server.agent import load_agent_config
    config = await asyncio.to_thread(load_agent_config)
    return {"status": "success", "config": config}


async def _agent_update_config(updates: str) -> dict:
    """Update agent configuration with JSON patch."""
    from naukri_server.agent import load_agent_config, save_agent_config, validate_agent_config
    config = await asyncio.to_thread(load_agent_config)
    try:
        patch = json.loads(updates)
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON: {e}", "error_code": "VALIDATION_ERROR"}

    # Warn about unknown keys
    known_keys = {"enabled", "mode", "max_daily_applications", "min_fit_score", "quiet_hours", "searches", "blocklist", "reminder_days"}
    unknown = set(patch.keys()) - known_keys

    config.update(patch)
    errors = validate_agent_config(config)
    if errors:
        return {"status": "error", "message": "Validation failed", "errors": errors, "error_code": "VALIDATION_ERROR"}

    await asyncio.to_thread(save_agent_config, config)
    result = {"status": "success", "message": "Config updated", "config": config}
    if unknown:
        result["warnings"] = [f"Unknown config key: {k}" for k in unknown]
    return result


async def _agent_run_now(ctx: Context | None = None) -> dict:
    """Run one agent cycle immediately."""
    from naukri_server.agent import run_agent_cycle
    if ctx is not None:
        return await run_agent_cycle(ctx=ctx)
    return await run_agent_cycle()


async def _agent_approve(cycle_id: str, approve: bool = True) -> dict:
    """Approve or reject a pending agent cycle's decisions."""
    from naukri_server.database import get_agent_run, list_agent_decisions, update_agent_decision

    run = await get_agent_run(cycle_id)
    if not run:
        return {"status": "error", "message": f"Unknown cycle: {cycle_id}", "error_code": "NOT_FOUND"}

    decisions = await list_agent_decisions(cycle_id)
    pending = [d for d in decisions if d.get("decision") == "apply" and d.get("apply_status") == "pending"]

    if not pending:
        return {"status": "error", "message": "No pending decisions to approve", "error_code": "VALIDATION_ERROR"}

    if not approve:
        for d in pending:
            await update_agent_decision(cycle_id, d["job_id"], "rejected")
        return {"status": "success", "message": f"Rejected {len(pending)} decisions", "rejected": len(pending)}

    # Approve = apply
    from naukri_server.tools.apply import _apply_single
    applied = 0
    errors = []
    for d in pending:
        try:
            result = await _apply_single(
                job_id=d["job_id"], title=d.get("title"), company=d.get("company"),
                tracking_extra={"source": "agent_approved", "cycle_id": cycle_id},
            )
            status = result.get("status", "error")
            await update_agent_decision(cycle_id, d["job_id"], status)
            if status == "applied":
                applied += 1
                # Emit AgentJobApplied with agent-specific context (cycle_id, fit_score).
                # Note: _apply_single already emits ApplicationSubmitted for the generic
                # apply event; this AgentJobApplied carries the agent-loop context.
                try:
                    from naukri_server.events import event_bus, AgentJobApplied
                    await event_bus.emit(AgentJobApplied(
                        cycle_id=cycle_id,
                        job_id=str(d.get("job_id", "")),
                        company=str(d.get("company", "") or ""),
                        title=str(d.get("title", "") or ""),
                        fit_score=int(d.get("fit_score", 0) or 0),
                    ))
                except Exception as ee:
                    logger.warning("Failed to emit AgentJobApplied: %s", ee)
        except Exception as e:
            errors.append(str(e))
            await update_agent_decision(cycle_id, d["job_id"], "error")

    return {"status": "success", "applied": applied, "failed": len(errors), "errors": errors[:5]}


async def _agent_history(limit: int = 10) -> dict:
    """Get agent run history."""
    from naukri_server.database import list_agent_runs
    runs = await list_agent_runs(limit=limit)
    return {"status": "success", "total": len(runs), "runs": runs}


async def _agent_decisions(cycle_id: str) -> dict:
    """Get decisions for a specific agent cycle."""
    from naukri_server.database import list_agent_decisions
    decisions = await list_agent_decisions(cycle_id)
    return {"status": "success", "cycle_id": cycle_id, "total": len(decisions), "decisions": decisions}


# ---------------------------------------------------------------------------
# Atomic single-purpose MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_agent_status() -> dict:
    """Get agent state, last 5 runs, and config summary.

    Returns:
        {status, enabled, mode, max_daily, min_fit_score, searches,
         blocklist_companies, quiet_hours, recent_runs: [...]}
    """
    return await handle_tool_action(_agent_status, "agent.status")


@mcp.tool()
async def naukri_agent_config() -> dict:
    """Get full agent configuration (searches, blocklist, mode, limits).

    Returns:
        {status, config: {...full config...}}
    """
    return await handle_tool_action(_agent_config, "agent.config")


@mcp.tool()
async def naukri_agent_update_config(updates: str) -> dict:
    """Merge JSON updates into agent config.

    Args:
        updates: JSON string of fields to update (e.g. '{"mode": "auto", "min_fit_score": 75}')

    Returns:
        {status, message, config: {...updated...}, warnings?}
    """
    return await handle_tool_action(
        lambda: _agent_update_config(updates=updates or "{}"),
        "agent.update_config",
    )


@mcp.tool()
async def naukri_agent_run_now(ctx: Context | None = None) -> dict:
    """Execute one agent observe→decide→act→learn cycle immediately.

    Args:
        ctx: Optional MCP context for progress reporting

    Returns:
        {status, cycle_id, applied, skipped, mode}
    """
    return await handle_tool_action(
        lambda: _agent_run_now(ctx=ctx),
        "agent.run_now",
    )


@mcp.tool()
async def naukri_agent_approve(cycle_id: str) -> dict:
    """Apply to pending jobs from an approval-mode agent cycle.

    Args:
        cycle_id: The agent cycle ID with pending decisions

    Returns:
        {status, applied, failed, errors}
    """
    if not cycle_id:
        return {"status": "error", "message": "cycle_id is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _agent_approve(cycle_id=cycle_id, approve=True),
        "agent.approve",
    )


@mcp.tool()
async def naukri_agent_reject(cycle_id: str) -> dict:
    """Reject all pending decisions from an agent cycle.

    Args:
        cycle_id: The agent cycle ID with pending decisions

    Returns:
        {status, message, rejected}
    """
    if not cycle_id:
        return {"status": "error", "message": "cycle_id is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _agent_approve(cycle_id=cycle_id, approve=False),
        "agent.reject",
    )


@mcp.tool()
async def naukri_agent_history(limit: int = 10) -> dict:
    """Get recent agent run history.

    Args:
        limit: Max records to return (default 10)

    Returns:
        {status, total, runs: [...]}
    """
    return await handle_tool_action(
        lambda: _agent_history(limit=limit),
        "agent.history",
    )


@mcp.tool()
async def naukri_agent_decisions(cycle_id: str) -> dict:
    """Get per-job decisions for a specific agent cycle.

    Args:
        cycle_id: The agent cycle ID

    Returns:
        {status, cycle_id, total, decisions: [...]}
    """
    if not cycle_id:
        return {"status": "error", "message": "cycle_id is required.", "error_code": "VALIDATION_ERROR"}
    return await handle_tool_action(
        lambda: _agent_decisions(cycle_id=cycle_id),
        "agent.decisions",
    )
