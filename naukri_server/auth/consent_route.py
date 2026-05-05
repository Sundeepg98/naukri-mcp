"""Consent UI for OAuth authorization (used when MCP_OAUTH_AUTO_APPROVE != 1).

Exposes:
  GET  /oauth/consent?consent_id=...   → renders Approve/Deny page
  POST /oauth/consent                   → processes form, redirects to client

Single-user server, so the consent page is a single-button confirmation
(no real "user picker"). It exists so a paranoid owner can verify what
client and scopes are requesting access before approving.
"""
import logging
from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)


_CONSENT_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Authorize Naukri MCP Access</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, sans-serif; max-width: 480px;
        margin: 4em auto; padding: 1em; color: #222; }}
 h1 {{ font-size: 1.4em; margin-bottom: 0.2em; }}
 .meta {{ color: #555; font-size: 0.9em; margin: 1em 0; line-height: 1.6; }}
 .meta b {{ color: #222; }}
 form {{ display: flex; gap: 0.5em; margin-top: 1.5em; }}
 button {{ padding: 0.6em 1.2em; font-size: 1em; border-radius: 6px;
          border: 1px solid #888; cursor: pointer; }}
 .approve {{ background: #006da7; color: white; border-color: #006da7; }}
 .deny {{ background: #fff; color: #444; }}
</style>
</head><body>
<h1>Authorize Naukri MCP access?</h1>
<div class="meta">
 <div><b>Client:</b> {client_name}</div>
 <div><b>Client ID:</b> <code>{client_id}</code></div>
 <div><b>Scopes:</b> {scopes}</div>
 <div><b>Redirect:</b> <code>{redirect_uri}</code></div>
</div>
<form method="POST" action="/oauth/consent">
 <input type="hidden" name="consent_id" value="{consent_id}">
 <button class="approve" name="action" value="approve">Approve</button>
 <button class="deny" name="action" value="deny">Deny</button>
</form>
</body></html>
"""


def register(mcp_instance, oauth_provider) -> None:
    """Register /oauth/consent GET + POST routes on the given FastMCP instance.

    Called from naukri_server/__init__.py only when OAuth is enabled.
    """

    @mcp_instance.custom_route("/oauth/consent", methods=["GET"])
    async def consent_get(request: Request) -> Response:
        consent_id = request.query_params.get("consent_id", "").strip()
        if not consent_id:
            return HTMLResponse("Missing consent_id", status_code=400)
        entry = await oauth_provider.get_pending_consent(consent_id)
        if not entry:
            return HTMLResponse(
                "<h1>Consent request not found or expired.</h1>",
                status_code=404,
            )
        client, params = entry
        scopes = " ".join(params.scopes or [])
        html = _CONSENT_TEMPLATE.format(
            consent_id=escape(consent_id),
            client_name=escape(getattr(client, "client_name", None) or client.client_id or "Unknown"),
            client_id=escape(client.client_id or ""),
            scopes=escape(scopes or "naukri:full"),
            redirect_uri=escape(str(params.redirect_uri)),
        )
        return HTMLResponse(html)

    def _form_str(form, key: str) -> str:
        """Coerce a form field to str — Starlette form fields can be str OR UploadFile.
        We expect plain text fields, so any non-string value is treated as empty."""
        value = form.get(key)
        if isinstance(value, str):
            return value.strip()
        return ""

    @mcp_instance.custom_route("/oauth/consent", methods=["POST"])
    async def consent_post(request: Request) -> Response:
        form = await request.form()
        consent_id = _form_str(form, "consent_id")
        action = _form_str(form, "action")
        if not consent_id or action not in ("approve", "deny"):
            return HTMLResponse("Bad request", status_code=400)
        redirect_url = await oauth_provider.complete_consent(
            consent_id, approved=(action == "approve")
        )
        if not redirect_url:
            return HTMLResponse(
                "<h1>Consent request not found or expired.</h1>",
                status_code=404,
            )
        return RedirectResponse(redirect_url, status_code=302)

    logger.info("OAuth consent UI registered at /oauth/consent")
