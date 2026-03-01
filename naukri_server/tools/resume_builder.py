"""Resume builder tools — templates and AI resume builder status."""

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.config import RESUME_BUILDER_CONFIG_API, RESUME_BUILDER_STATUS_API


# ---------------------------------------------------------------------------
# Internal helpers (not MCP tools — used by the unified tool)
# ---------------------------------------------------------------------------

async def _get_templates() -> dict:
    """Fetch available resume builder templates."""
    data = await api_get(RESUME_BUILDER_CONFIG_API, params={"source": "rmLandingPage"})

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
    data = await api_get(RESUME_BUILDER_STATUS_API, params={"service": "resumeBuilder"})

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
# Unified MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def naukri_resume_builder(
    action: str = "templates",
) -> dict:
    """Unified resume builder — browse templates and check builder status.

    Actions:
      - "templates": List available resume builder templates (free and pro with preview images)
      - "status": Get resume builder service status (AI rewrite attempts left, subscription tier, features)

    Args:
        action: "templates" | "status"

    Returns:
        - templates: {status, count, free_count, pro_count, templates: [{id, name, type, preview_url}]}
        - status: {status, attempts_left, is_paid, show_genai_features, ...}
        - {status: "error", message} on failure
    """
    # -- templates ──────────────────────────────────────────────────────
    if action == "templates":
        try:
            return await _get_templates()
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Get resume templates failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # -- status ─────────────────────────────────────────────────────────
    elif action == "status":
        try:
            return await _get_status()
        except NaukriAPIError as e:
            return {"status": "error", "message": str(e), "http_status": e.status, "error_code": "API_ERROR"}
        except Exception as e:
            return {"status": "error", "message": f"Get resume builder status failed: {type(e).__name__}: {e}", "error_code": "API_ERROR"}

    # -- unknown action ─────────────────────────────────────────────────
    else:
        return {"status": "error", "message": f"Unknown action '{action}'. Use: templates, status", "error_code": "VALIDATION_ERROR"}
