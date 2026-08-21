"""MCP tools for the central config file — read the effective policy, patch it.

The server provides the capability; the calling client decides whether to use
it. So the surface is deliberately narrow:

* ``naukri_config()`` reads everything, including PROVENANCE — whether each
  value came from his file or from the shipped default — and including what the
  file is NOT allowed to decide, by name. "Why did my edit do nothing?" has to
  be answerable without reading source.

* ``naukri_set_config()`` writes ``candidate``, ``scoring`` and
  ``servers.naukri`` only. A patch touching another server's section is refused
  by name. Compare-and-swap on ``base_revision`` so two writers cannot lose an
  update, and ``confirm_widen`` for the one-way guards.

Neither tool can arm the autonomous agent. That block is not loadable from this
file at any tier — see :mod:`naukri_server.policy` and
``tests/test_safety_invariant.py``, which runs the escalation rather than
asserting it cannot happen.
"""

import json
from typing import Optional

from naukri_server import mcp
from naukri_server.error_handler import handle_tool_action


async def _config(section: Optional[str] = None) -> dict:
    from naukri_server import policy

    report = policy.report(section)
    report.setdefault("status", "success")
    return report


async def _set_config(patch: str, base_revision: Optional[int] = None,
                      confirm_widen: bool = False,
                      actor: str = "claude") -> dict:
    from naukri_server import policy

    try:
        parsed = json.loads(patch) if isinstance(patch, str) else patch
    except json.JSONDecodeError as exc:
        return {"status": "error", "message": f"Invalid JSON: {exc}",
                "error_code": "VALIDATION_ERROR"}
    if not isinstance(parsed, dict):
        return {"status": "error", "message": "Patch must be a JSON object",
                "error_code": "VALIDATION_ERROR"}

    result = policy.apply_patch(parsed, base_revision=base_revision,
                                actor=actor, confirm_widen=confirm_widen)
    if result.get("status") == "refused":
        result["error_code"] = "REFUSED"
    elif result.get("status") == "conflict":
        result["error_code"] = "CONFLICT"
        result.setdefault("message", (
            "the file changed since you read it; re-read with naukri_config() "
            "and re-apply your patch on top of the current values"
        ))
    elif result.get("status") == "no_config_file":
        result["error_code"] = "NOT_FOUND"
    return result


# ---------------------------------------------------------------------------
# Atomic single-purpose MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_config(section: Optional[str] = None) -> dict:
    """Read the effective scoring/candidate/naukri policy and where it came from.

    The numbers behind a fit score — the skills-vs-experience split, the
    bonuses, the verdict bands, the experience penalties — are values in a JSON
    file, not literals in the code. This reports what they currently are.

    Args:
        section: "candidate", "scoring" or "server" to return just that block.
            Omit for everything.

    Returns:
        {status, policy_rev, policy_hash, source, config_status,
         candidate, scoring, server, provenance, unknown_keys,
         tier_c_refusals, warnings, config_error, external_edit,
         revision_regression, searched, not_loadable_here,
         min_agent_fit_floor, agent_authority}

        - provenance says "file" or "default" per key, so "is that my setting
          or the shipped value?" is answerable without diffing.
        - source is null when no file was found; `searched` then lists every
          path that was tried.
        - tier_c_refusals names any key present in the file that the file is
          not allowed to decide. Those keep their Python values.
        - external_edit / revision_regression are set when the file was
          hand-edited or when a save overwrote a newer revision.
    """
    return await handle_tool_action(lambda: _config(section), "config.read")


@mcp.tool()
async def naukri_set_config(
    patch: str,
    base_revision: Optional[int] = None,
    confirm_widen: bool = False,
) -> dict:
    """Patch the central config file. Deep merge, compare-and-swap, tier-checked.

    Writes only `candidate`, `scoring` and `servers.naukri`. Dicts merge, lists
    replace, and a `null` leaf reverts that key to its shipped default.

    CANNOT arm the autonomous agent: `agent.enabled`, `agent.mode`,
    `agent.min_fit_score`, `agent.searches`, `agent.per_search_limit` and
    `agent.blocklist.enabled` are not loadable from this file at all. Use
    naukri_agent_update_config for those, and note that a Python floor still
    bounds what the agent will enqueue whatever any file says.

    Args:
        patch: JSON object of changes, e.g.
            '{"scoring": {"weights": {"skills": 0.75, "experience": 0.25}}}'
        base_revision: the `revision` you read from naukri_config(). If it no
            longer matches, the write is refused as a conflict rather than
            silently overwriting someone else's change. Omit to skip the check.
        confirm_widen: required to LOOSEN a one-way guard — raising a daily
            cap, adding skills (every skill added raises every score), removing
            a blocklist entry. Tightening never needs it.

    Returns:
        {status: "ok", revision, policy_rev, policy_hash, changed: {...}}
        {status: "refused", refusals: [...], error_code: "REFUSED"}
        {status: "conflict", revision, error_code: "CONFLICT"}
        {status: "no_config_file", searched: [...], hint, error_code: "NOT_FOUND"}
    """
    return await handle_tool_action(
        lambda: _set_config(patch, base_revision=base_revision,
                            confirm_widen=confirm_widen),
        "config.write",
    )
