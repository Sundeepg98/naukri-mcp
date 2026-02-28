"""Resume builder tools — templates, status, and AI resume features."""

from naukri_server import mcp
from naukri_server.api import api_get, NaukriAPIError
from naukri_server.config import RESUME_BUILDER_CONFIG_API, RESUME_BUILDER_STATUS_API


@mcp.tool()
async def naukri_get_resume_templates() -> dict:
    """List available resume builder templates — free and pro templates with preview images.

    Returns 12 templates (3 free, 9 pro/paid). Each includes name, ID, type (free/pro),
    and preview image URL.

    Returns:
        - {status: "success", count, free_count, pro_count, templates: [{id, name, type, preview_url}]}
        - {status: "error", message}
    """
    try:
        data = await api_get(RESUME_BUILDER_CONFIG_API)

        # Response may be wrapped in data envelope
        inner = data.get("data", data) if isinstance(data, dict) else data

        # Templates are in configurations list
        configs = inner.get("configurations", inner) if isinstance(inner, dict) else inner

        templates = []
        if isinstance(configs, list):
            for t in configs:
                if isinstance(t, dict):
                    ttype = "free" if t.get("type") == 1 else "pro"
                    templates.append({
                        "id": t.get("id", t.get("templateId", "")),
                        "name": t.get("name", t.get("templateName", "")),
                        "type": ttype,
                        "preview_url": t.get("previewImage", t.get("image", "")),
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
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get resume templates: {type(e).__name__}: {e}"}


@mcp.tool()
async def naukri_get_resume_builder_status() -> dict:
    """Get your resume builder service status — AI rewrite attempts left, subscription tier, and features.

    Returns:
        - {status: "success", attempts_left, is_paid, show_genai, ...}
        - {status: "error", message}
    """
    try:
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
    except NaukriAPIError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"Failed to get resume builder status: {type(e).__name__}: {e}"}
