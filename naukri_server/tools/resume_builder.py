"""Resume builder tools — templates and AI resume builder status."""

from typing import Optional

from naukri_server import mcp
from naukri_server.interfaces import api_client
from naukri_server.config import RESUME_BUILDER_CONFIG_API, RESUME_BUILDER_STATUS_API
from naukri_server.error_handler import handle_tool_action
from naukri_server.models import validate_action_params


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _get_templates() -> dict:
    """Fetch available resume builder templates."""
    data = await api_client.get(RESUME_BUILDER_CONFIG_API, params={"source": "rmLandingPage"})

    # Response: {statusCode, message, data: {templateConfiguration: {filters, templateDetails}}}
    inner = data.get("data", data) if isinstance(data, dict) else data
    tc = inner.get("templateConfiguration", inner) if isinstance(inner, dict) else inner
    configs = tc.get("templateDetails", tc.get("configurations", [])) if isinstance(tc, dict) else tc

    templates = []
    if isinstance(configs, list):
        for t in configs:
            if isinstance(t, dict):
                ttype = "free" if t.get("type") == 1 else "pro"
                icons = t.get("icons", {})
                preview = icons.get("variant1", "") if isinstance(icons, dict) else ""
                templates.append({
                    "id": t.get("id", ""),
                    "name": t.get("name", ""),
                    "type": ttype,
                    "preview_url": preview,
                })

    free_count = sum(1 for t in templates if t["type"] == "free")
    pro_count = sum(1 for t in templates if t["type"] == "pro")

    return {
        "status": "success",
        "count": len(templates),
        "free_count": free_count,
        "pro_count": pro_count,
        "templates": templates,
    }


async def _get_status() -> dict:
    """Fetch resume builder service status."""
    data = await api_client.get(RESUME_BUILDER_STATUS_API, params={"service": "resumeBuilder"})

    # Response may be wrapped in data envelope
    inner = data.get("data", data) if isinstance(data, dict) else data

    # The service details are under resumeBuilder key or at top level
    rb = inner.get("resumeBuilder", inner) if isinstance(inner, dict) else inner
    if not isinstance(rb, dict):
        rb = {}

    return {
        "status": "success",
        "attempts_left": rb.get("attemptsLeft", 0),
        "is_paid": rb.get("isPaid", False),
        "show_genai_features": rb.get("showGenAIFeatures", False),
        "show_rewrite": rb.get("showRewrite", False),
        "experiment_variant": rb.get("attachResumeExperimentVariant", ""),
    }


# ---------------------------------------------------------------------------
# ISP param validation
# ---------------------------------------------------------------------------

_VALID_PARAMS_PER_ACTION = {
    "templates": set(),
    "status": set(),
    "tailor": {"job_id", "timeout_seconds"},
}


# ---------------------------------------------------------------------------
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_resume_builder(
    action: str = "templates",
    job_id: Optional[str] = None,
    timeout_seconds: int = 120,
) -> dict:
    """Unified resume builder — browse templates, check builder status, or tailor resume for a job.

    Actions:
      - "templates": List available resume builder templates (free and pro with preview images)
      - "status": Get resume builder service status (AI rewrite attempts left, subscription tier, features)
      - "tailor": Tailor resume for a specific job (requires job_id)

    Args:
        action: "templates" | "status" | "tailor"
        job_id: Naukri job ID or URL (required for tailor)
        timeout_seconds: Max seconds before timeout (default 120, used by tailor)

    Returns:
        - templates: {status, count, free_count, pro_count, templates: [{id, name, type, preview_url}]}
        - status: {status, attempts_left, is_paid, show_genai_features, ...}
        - tailor: {status, job_title, company, suggestions: {headline, skills_to_add, ...}}
        - {status: "error", message} on failure
    """
    # ── ISP: warn about params irrelevant to chosen action ─────────────
    _provided = {
        "job_id": job_id,
        "timeout_seconds": timeout_seconds if timeout_seconds != 120 else None,
    }
    _unused = validate_action_params(action, _provided, _VALID_PARAMS_PER_ACTION)

    def _attach_unused(result: dict) -> dict:
        if _unused and isinstance(result, dict):
            result["unused_params"] = _unused
        return result

    # -- templates ──────────────────────────────────────────────────────
    if action == "templates":
        return _attach_unused(await handle_tool_action(_get_templates, "resume_builder.templates"))

    # -- status ─────────────────────────────────────────────────────────
    elif action == "status":
        return _attach_unused(await handle_tool_action(_get_status, "resume_builder.status"))

    # -- tailor ──────────────────────────────────────────────────────────
    elif action == "tailor":
        if not job_id:
            return {"status": "error", "message": "tailor requires job_id.", "error_code": "VALIDATION_ERROR"}
        from naukri_server.tools.resume_tailor import _tailor_resume
        return _attach_unused(await handle_tool_action(
            lambda: _tailor_resume(job_id=job_id, timeout_seconds=timeout_seconds),
            "resume_builder.tailor",
        ))

    # -- unknown action ─────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: templates, status, tailor", "error_code": "VALIDATION_ERROR"}
