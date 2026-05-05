"""LSP behavioral contract tests.

Verify structural guarantees that every dispatcher and infrastructure helper
must uphold, independent of network/browser state.
"""

import pytest


# ───────────────────────────────────────────────────────────────────
# 1. handle_tool_action contract
# ───────────────────────────────────────────────────────────────────


class TestHandleToolActionContract:
    """handle_tool_action wraps callables with standardized error handling."""

    @pytest.mark.asyncio
    async def test_success_passthrough(self):
        from naukri_server.error_handler import handle_tool_action

        async def ok():
            return {"status": "success"}

        r = await handle_tool_action(ok, "test")
        assert r["status"] == "success"

    @pytest.mark.asyncio
    async def test_api_error_contract(self):
        from naukri_server.error_handler import handle_tool_action
        from naukri_server.api import NaukriAPIError

        async def fail():
            raise NaukriAPIError(503, "down")

        r = await handle_tool_action(fail, "test")
        assert r["status"] == "error"
        assert r["error_code"] == "API_ERROR"
        assert r["http_status"] == 503

    @pytest.mark.asyncio
    async def test_generic_error_contract(self):
        from naukri_server.error_handler import handle_tool_action

        async def fail():
            raise RuntimeError("boom")

        r = await handle_tool_action(fail, "test")
        assert r["status"] == "error"
        assert r["error_code"] == "INTERNAL_ERROR"


# ───────────────────────────────────────────────────────────────────
# 2. ApiClient interface contract
# ───────────────────────────────────────────────────────────────────


class TestInterfaceContract:
    """Concrete classes implement the abstract interfaces."""

    def test_naukri_api_client_is_api_client(self):
        from naukri_server.interfaces import NaukriApiClient, ApiClient
        assert issubclass(NaukriApiClient, ApiClient)

    def test_playwright_browser_is_browser_provider(self):
        from naukri_server.interfaces import PlaywrightBrowserProvider, BrowserProvider
        assert issubclass(PlaywrightBrowserProvider, BrowserProvider)
