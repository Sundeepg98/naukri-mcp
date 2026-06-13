"""MCP tool for the global apply/auto-hunt kill-switch.

Mirrors the resume_healing UX: the switch trips automatically on a block (see
naukri_server.kill_switch), and the operator inspects + resumes it explicitly
here. Resume is deliberately a manual, operator-gated action — auto-resuming
into a live block is what risks an account lockout.
"""

import logging

from naukri_server import mcp, kill_switch
from naukri_server.error_handler import handle_tool_action

logger = logging.getLogger(__name__)


async def _kill_switch_status() -> dict:
    return {"status": "success", **kill_switch.status()}


async def _kill_switch_resume() -> dict:
    before = kill_switch.is_tripped()
    kill_switch.reset()
    return {
        "status": "success",
        "was_tripped": before,
        "message": (
            "Kill-switch reset — apply/auto-hunt re-enabled."
            if before else
            "Kill-switch was not tripped; nothing to resume."
        ),
        "note": "The autonomous agent stays in dry_run if it was flipped — set its "
                "mode back to approval/auto explicitly with naukri_agent_update_config.",
    }


async def _kill_switch_trip(reason: str) -> dict:
    """Manual operator trip (e.g. they noticed a block before the system did)."""
    result = await kill_switch.trip_and_halt(reason or "manual operator trip",
                                             block_kind="manual")
    return {"status": "success", **result}


@mcp.tool()
async def naukri_kill_switch(action: str = "status", reason: str = "") -> dict:
    """Inspect or control the global apply/auto-hunt kill-switch.

    The kill-switch HALTS all apply + auto-hunt traffic when Naukri's bot
    manager soft-blocks us, to protect the account from a lockout. It trips
    automatically on a block-state detection (or N soft signals in a window),
    flips the autonomous agent to dry_run, and persists the tripped state across
    restarts. Resume is operator-gated.

    Args:
        action: One of:
            - "status" (default): show tripped state, reason, soft-signal count.
            - "resume": clear the tripped state and re-enable apply/auto-hunt.
            - "trip": manually trip the switch now (reason recommended).
        reason: Optional human-readable reason (used with action="trip").

    Returns:
        {status, tripped, reason, block_kind, tripped_at, trip_count, ...}
    """
    act = (action or "status").strip().lower()
    if act == "status":
        return await handle_tool_action(_kill_switch_status, "kill_switch.status")
    if act in ("resume", "reset", "enable"):
        return await handle_tool_action(_kill_switch_resume, "kill_switch.resume")
    if act in ("trip", "halt", "disable"):
        return await handle_tool_action(
            lambda: _kill_switch_trip(reason), "kill_switch.trip",
        )
    return {
        "status": "error",
        "message": f"Unknown action '{action}'. Use 'status', 'resume', or 'trip'.",
        "error_code": "VALIDATION_ERROR",
    }
