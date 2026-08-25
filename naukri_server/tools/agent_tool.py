"""MCP tools for controlling the autonomous job-hunting agent."""

import asyncio
import json
import logging

from mcp.server.fastmcp import Context

from naukri_server import mcp
from naukri_server.config import APPLY_MIN_FIT_SCORE
from naukri_server.error_handler import handle_tool_action

logger = logging.getLogger(__name__)


def _deep_merge(base: dict, patch: dict) -> dict:
    """Dicts MERGE, everything else REPLACES.

    jobcore's loader uses the identical rule for the config file; this is the
    same decision applied to agent_config.json, which is a separate file on
    purpose -- it holds the keys the shared config does NOT decide, and it is
    the lower-precedence layer for the six it does.
    """
    out = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _flatten_patch(patch: dict, prefix: str = "") -> dict:
    """Every leaf of *patch* as a dotted path. Lists are leaves."""
    out: dict = {}
    for key, value in (patch or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            out.update(_flatten_patch(value, path))
        else:
            out[path] = value
    return out


def _shadowed_by_the_policy_file(patch: dict) -> list[str]:
    """Keys this patch wrote that `config/jobhunt.json` then overrides.

    The shared file wins over agent_config.json for the six keys in
    `agent.FILE_DECIDABLE_KEYS`, so a patch touching one of them can be saved
    correctly and still not take effect. That is the exact silent-no-op shape
    this repo has been bitten by twice, so it is reported as a warning naming
    the key and the winning value rather than left for someone to discover by
    watching the agent do the wrong thing.
    """
    try:
        from naukri_server.agent import agent_config_overlay
    except Exception:  # pragma: no cover - defensive
        return []
    try:
        overlay = agent_config_overlay()
    except Exception:  # pragma: no cover - overlay already swallows
        return []
    if not overlay:
        return []

    flat = _flatten_patch(patch)
    out = []
    for dotted, winning in sorted(overlay.items()):
        if dotted not in flat:
            continue
        attempted = flat[dotted]
        if attempted == winning:
            continue
        out.append(
            f"{dotted}={attempted!r} was saved to agent_config.json but the "
            f"shared policy file decides this key and sets {winning!r}; the "
            f"agent will use {winning!r}. Edit config/jobhunt.json (via "
            f"naukri_set_config) to change it."
        )
    return out


def _min_agent_fit_floor() -> int:
    from naukri_server.agent import _min_agent_fit_floor as floor

    return floor()


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
        "min_fit_score": config.get("min_fit_score", APPLY_MIN_FIT_SCORE),
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
    """Get full agent configuration, plus which keys the shared file decided."""
    from naukri_server.agent import agent_config_overlay, load_agent_config
    config = await asyncio.to_thread(load_agent_config)
    overlay = await asyncio.to_thread(agent_config_overlay)
    return {"status": "success", "config": config,
            "config_sources": {k: "config/jobhunt.json" for k in sorted(overlay)}}


async def _agent_update_config(updates: str) -> dict:
    """Update agent configuration with JSON patch."""
    from naukri_server.agent import load_agent_config, save_agent_config, validate_agent_config
    config = await asyncio.to_thread(load_agent_config)
    try:
        patch = json.loads(updates)
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Invalid JSON: {e}", "error_code": "VALIDATION_ERROR"}

    if not isinstance(patch, dict):
        return {"status": "error", "message": "Patch must be a JSON object",
                "error_code": "VALIDATION_ERROR"}

    # Warn about unknown keys. `per_search_limit` joined this set on
    # 2026-08-25: it shipped in the schema and in agent_config.json but
    # `_decide` passed a hardcoded limit, so it was a decoy. It is read now.
    known_keys = {"enabled", "mode", "max_daily_applications", "min_fit_score",
                  "quiet_hours", "searches", "blocklist", "per_search_limit"}
    unknown = set(patch.keys()) - known_keys

    # DEEP merge, not dict.update. `config.update(patch)` REPLACED whole nested
    # blocks: patching {"quiet_hours": {"start_hour": 22}} silently reset
    # end_hour to 8 and re-derived `enabled` from a dict that no longer had it,
    # so a partial patch was a partial RESET of the quiet window that bounds
    # autonomous applying. Dicts merge; lists (searches, blocklist entries)
    # replace, because a list patch is a restatement, not an append.
    config = _deep_merge(config, patch)
    errors = validate_agent_config(config)
    if errors:
        return {"status": "error", "message": "Validation failed", "errors": errors, "error_code": "VALIDATION_ERROR"}

    await asyncio.to_thread(save_agent_config, config)
    result = {"status": "success", "message": "Config updated", "config": config}
    if unknown:
        result["warnings"] = [f"Unknown config key: {k}" for k in unknown]

    for shadowed in _shadowed_by_the_policy_file(patch):
        result.setdefault("warnings", []).append(shadowed)

    floor = _min_agent_fit_floor()
    if int(config.get("min_fit_score", floor)) < floor:
        result.setdefault("warnings", []).append(
            f"min_fit_score {config['min_fit_score']} is below the Python floor "
            f"{floor}; the agent will still refuse to enqueue anything under "
            f"{floor}. Lowering it here changes what is DISPLAYED, not what is "
            f"applied to."
        )
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

    This is the EFFECTIVE config after all three layers are merged:
    Python defaults < agent_config.json < config/jobhunt.json, where the
    shared file decides enabled, mode, min_fit_score, searches,
    per_search_limit and blocklist.enabled and nothing else. `config_sources`
    names the keys the shared file supplied, so a value that disagrees with
    agent_config.json is traceable without diffing two files.

    The numbers here are what the agent was CONFIGURED with, not what it will
    do: MIN_AGENT_FIT_FLOOR (60) raises any lower min_fit_score at run time,
    the daily quota caps candidates, and the kill switch can halt a batch.

    Returns:
        {status, config: {...full config...}, config_sources: {...}}
    """
    return await handle_tool_action(_agent_config, "agent.config")


@mcp.tool()
async def naukri_agent_update_config(updates: str) -> dict:
    """Merge JSON updates into agent config (agent_config.json).

    NOT THE ONLY SOURCE, since 2026-08-25. Six keys -- enabled, mode,
    min_fit_score, searches, per_search_limit, blocklist.enabled -- are also
    loadable from the shared config/jobhunt.json, and THE SHARED FILE WINS.
    A patch touching one of those is still saved here, but the response
    carries a warning naming the key and the value the agent will actually
    use; change it in config/jobhunt.json (naukri_set_config) instead.
    Everything else -- max_daily_applications, cycle_interval_hours,
    quiet_hours -- is decided here and only here.

    Four Python guards bound the agent whatever any config says, and none of
    them can be moved from any file:

      - MIN_AGENT_FIT_FLOOR (60) floors the apply selector on every cycle and
        on every per-search override, so a min_fit_score below it changes what
        is DISPLAYED, not what is applied to.
      - The kill switch is re-checked inside the auto-apply loop, so it halts
        a batch part-way rather than only at the start.
      - The daily quota caps candidates before any apply runs, and
        max_daily_applications is validated to 1-100.
      - requires_approval_cycle forces ONE approval cycle per change to the
        scoring/candidate fingerprint, and on the first-ever cycle. Note it
        keys on {scoring, candidate}, NOT on the agent block: setting mode to
        "auto" does not itself buy an approval cycle.

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
