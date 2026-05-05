"""Resume builder tools — templates and AI resume builder status."""

from typing import Optional

from naukri_server import mcp
from naukri_server.interfaces import api_client
from naukri_server.config import RESUME_BUILDER_CONFIG_API, RESUME_BUILDER_STATUS_API
from naukri_server.error_handler import handle_tool_action


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
# Atomic single-purpose MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_resume_templates() -> dict:
    """List available resume builder templates (free and pro variants with preview images).

    Returns:
        {status, count, free_count, pro_count, templates: [{id, name, type, preview_url}]}
    """
    return await handle_tool_action(_get_templates, "resume_builder.templates")


@mcp.tool()
async def naukri_resume_builder_status() -> dict:
    """Get resume builder service status — AI rewrite attempts left, subscription tier, features.

    Returns:
        {status, attempts_left, is_paid, show_genai_features, show_rewrite, experiment_variant}
    """
    return await handle_tool_action(_get_status, "resume_builder.status")


@mcp.tool()
async def naukri_tailor_resume(job_id: str, timeout_seconds: int = 120) -> dict:
    """Tailor resume suggestions for a specific job (headline, skills to add, etc.).

    Args:
        job_id: Naukri job ID or URL
        timeout_seconds: Max seconds before timeout (default 120)

    Returns:
        {status, job_title, company, suggestions: {headline, skills_to_add, ...}}
    """
    if not job_id:
        return {"status": "error", "message": "job_id is required.", "error_code": "VALIDATION_ERROR"}
    from naukri_server.tools.resume_tailor import _tailor_resume
    return await handle_tool_action(
        lambda: _tailor_resume(job_id=job_id, timeout_seconds=timeout_seconds),
        "resume_builder.tailor",
    )
