"""Unit tests for naukri_server.auth.consent_route — OAuth consent UI.

All tests are PURE: no real HTTP server. We capture the handler functions
registered on a fake mcp_instance, then invoke them with mock Starlette Requests.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from naukri_server.auth.consent_route import register


def _make_fake_mcp():
    """Create a fake FastMCP that captures decorated handlers."""
    handlers = {}

    class FakeMCP:
        def custom_route(self, path, methods):
            def decorator(fn):
                key = f"{methods[0]} {path}"
                handlers[key] = fn
                return fn
            return decorator

    return FakeMCP(), handlers


def _make_request(query=None, form_data=None):
    """Build a minimal mock Starlette Request."""
    req = MagicMock()
    req.query_params = MagicMock()
    req.query_params.get = MagicMock(side_effect=lambda k, default="": (query or {}).get(k, default))
    if form_data is not None:
        # form() is async — return a dict-like mock that supports .get()
        form_mock = MagicMock()
        form_mock.get = MagicMock(side_effect=lambda k: form_data.get(k))
        req.form = AsyncMock(return_value=form_mock)
    return req


# ---------------------------------------------------------------------------
# 1. Registration — both handlers registered
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_attaches_get_and_post(self):
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()
        register(mcp, provider)
        assert "GET /oauth/consent" in handlers
        assert "POST /oauth/consent" in handlers


# ---------------------------------------------------------------------------
# 2. GET /oauth/consent — renders form
# ---------------------------------------------------------------------------

class TestConsentGet:
    @pytest.mark.asyncio
    async def test_missing_consent_id_returns_400(self):
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()
        register(mcp, provider)
        get_handler = handlers["GET /oauth/consent"]

        req = _make_request(query={})
        response = await get_handler(req)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_consent_id_returns_404(self):
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()
        provider.get_pending_consent = AsyncMock(return_value=None)
        register(mcp, provider)
        get_handler = handlers["GET /oauth/consent"]

        req = _make_request(query={"consent_id": "nonexistent"})
        response = await get_handler(req)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_valid_consent_renders_form(self):
        """Valid consent_id renders HTML with client name and approve/deny buttons."""
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()

        client = MagicMock()
        client.client_id = "test-client-123"
        client.client_name = "Test MCP Client"
        params = MagicMock()
        params.scopes = ["naukri:full"]
        params.redirect_uri = "https://example.com/callback"

        provider.get_pending_consent = AsyncMock(return_value=(client, params))
        register(mcp, provider)
        get_handler = handlers["GET /oauth/consent"]

        req = _make_request(query={"consent_id": "valid-123"})
        response = await get_handler(req)
        assert response.status_code == 200
        body = response.body.decode("utf-8")
        # Form should contain client info
        assert "Test MCP Client" in body
        assert "test-client-123" in body
        assert "naukri:full" in body
        assert "https://example.com/callback" in body
        # Both buttons present
        assert 'value="approve"' in body
        assert 'value="deny"' in body
        # Hidden consent_id field
        assert 'value="valid-123"' in body

    @pytest.mark.asyncio
    async def test_html_escapes_user_input(self):
        """Client info is HTML-escaped to prevent XSS."""
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()

        client = MagicMock()
        client.client_id = "abc"
        client.client_name = "<script>alert('xss')</script>"
        params = MagicMock()
        params.scopes = []
        params.redirect_uri = "https://example.com"

        provider.get_pending_consent = AsyncMock(return_value=(client, params))
        register(mcp, provider)
        get_handler = handlers["GET /oauth/consent"]

        req = _make_request(query={"consent_id": "x"})
        response = await get_handler(req)
        body = response.body.decode("utf-8")
        # Must not contain raw <script>
        assert "<script>alert" not in body
        # Should contain escaped version
        assert "&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# 3. POST /oauth/consent — handles approve/deny
# ---------------------------------------------------------------------------

class TestConsentPost:
    @pytest.mark.asyncio
    async def test_missing_consent_id_returns_400(self):
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()
        register(mcp, provider)
        post_handler = handlers["POST /oauth/consent"]

        req = _make_request(form_data={"action": "approve"})
        response = await post_handler(req)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_action_returns_400(self):
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()
        register(mcp, provider)
        post_handler = handlers["POST /oauth/consent"]

        req = _make_request(form_data={"consent_id": "abc", "action": "delete"})
        response = await post_handler(req)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_consent_id_returns_404(self):
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()
        provider.complete_consent = AsyncMock(return_value=None)
        register(mcp, provider)
        post_handler = handlers["POST /oauth/consent"]

        req = _make_request(form_data={"consent_id": "missing", "action": "approve"})
        response = await post_handler(req)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_redirects_to_callback(self):
        """approve action calls complete_consent(approved=True) and redirects."""
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()
        provider.complete_consent = AsyncMock(return_value="https://example.com/cb?code=xyz")
        register(mcp, provider)
        post_handler = handlers["POST /oauth/consent"]

        req = _make_request(form_data={"consent_id": "abc", "action": "approve"})
        response = await post_handler(req)
        assert response.status_code == 302
        assert response.headers["location"] == "https://example.com/cb?code=xyz"
        provider.complete_consent.assert_awaited_once_with("abc", approved=True)

    @pytest.mark.asyncio
    async def test_deny_redirects_with_approved_false(self):
        """deny action calls complete_consent(approved=False) and redirects."""
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()
        provider.complete_consent = AsyncMock(return_value="https://example.com/cb?error=denied")
        register(mcp, provider)
        post_handler = handlers["POST /oauth/consent"]

        req = _make_request(form_data={"consent_id": "abc", "action": "deny"})
        response = await post_handler(req)
        assert response.status_code == 302
        assert response.headers["location"] == "https://example.com/cb?error=denied"
        provider.complete_consent.assert_awaited_once_with("abc", approved=False)

    @pytest.mark.asyncio
    async def test_form_str_helper_handles_non_string_values(self):
        """If a form field is an UploadFile (non-str), it's treated as empty."""
        mcp, handlers = _make_fake_mcp()
        provider = MagicMock()
        register(mcp, provider)
        post_handler = handlers["POST /oauth/consent"]

        # Simulate a form where consent_id is a non-str (e.g., UploadFile)
        upload_file = MagicMock()  # not a str
        req = _make_request(form_data={"consent_id": upload_file, "action": "approve"})
        response = await post_handler(req)
        # consent_id coerced to "" → bad request
        assert response.status_code == 400
