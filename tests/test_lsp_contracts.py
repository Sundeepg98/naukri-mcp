"""LSP behavioral contract tests.

Verify structural guarantees that every dispatcher and infrastructure helper
must uphold, independent of network/browser state.
"""

import pytest


# ───────────────────────────────────────────────────────────────────
# 1. All 13 dispatchers return error_code on unknown action
# ───────────────────────────────────────────────────────────────────


class TestUnknownActionContract:
    """Every dispatcher returns error with error_code for unknown actions."""

    @pytest.mark.asyncio
    async def test_applications_unknown(self):
        from naukri_server.tools.tracking import naukri_applications
        r = await naukri_applications(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_insights_unknown(self):
        from naukri_server.tools.insights import naukri_insights
        r = await naukri_insights(insight_type="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_profile_unknown(self):
        from naukri_server.tools.profile import naukri_profile
        r = await naukri_profile(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_company_unknown(self):
        from naukri_server.tools.companies import naukri_company
        r = await naukri_company(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_settings_unknown(self):
        from naukri_server.tools.settings import naukri_settings
        r = await naukri_settings(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_jobs_unknown(self):
        from naukri_server.tools.jobs import naukri_jobs
        r = await naukri_jobs(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_saved_jobs_unknown(self):
        from naukri_server.tools.saved_jobs import naukri_saved_jobs
        r = await naukri_saved_jobs(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_inbox_unknown(self):
        from naukri_server.tools.inbox import naukri_inbox
        r = await naukri_inbox(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_alerts_unknown(self):
        from naukri_server.tools.alerts import naukri_job_alerts
        r = await naukri_job_alerts(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_early_access_unknown(self):
        from naukri_server.tools.early_access import naukri_early_access
        r = await naukri_early_access(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_mock_interview_unknown(self):
        from naukri_server.tools.mock_interview import naukri_mock_interview
        r = await naukri_mock_interview(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_resume_builder_unknown(self):
        from naukri_server.tools.resume_builder import naukri_resume_builder
        r = await naukri_resume_builder(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r

    @pytest.mark.asyncio
    async def test_reminders_unknown(self):
        from naukri_server.tools.reminders import naukri_reminders
        r = await naukri_reminders(action="BOGUS")
        assert r["status"] == "error"
        assert "error_code" in r


# ───────────────────────────────────────────────────────────────────
# 2. handle_tool_action contract
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
# 3. ApiClient interface contract
# ───────────────────────────────────────────────────────────────────


class TestInterfaceContract:
    """Concrete classes implement the abstract interfaces."""

    def test_naukri_api_client_is_api_client(self):
        from naukri_server.interfaces import NaukriApiClient, ApiClient
        assert issubclass(NaukriApiClient, ApiClient)

    def test_playwright_browser_is_browser_provider(self):
        from naukri_server.interfaces import PlaywrightBrowserProvider, BrowserProvider
        assert issubclass(PlaywrightBrowserProvider, BrowserProvider)
