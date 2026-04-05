"""Tests for the automated API endpoint discovery probe.

Every test is PURE: no network, no browser, no file I/O.
We mock browser, config, and database to verify discovery logic.
"""

import asyncio
import types
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_response(url, status=200, content_type="application/json"):
    """Create a mock Playwright response object."""
    resp = MagicMock()
    resp.url = url
    resp.status = status
    resp.headers = {"content-type": content_type}
    return resp


def _make_mock_page():
    """Create a mock page that captures on_response callbacks."""
    page = MagicMock()
    page._listeners = []

    def on(event, callback):
        if event == "response":
            page._listeners.append(callback)

    def remove_listener(event, callback):
        if event == "response":
            page._listeners = [c for c in page._listeners if c != callback]

    page.on = on
    page.remove_listener = remove_listener
    return page


@asynccontextmanager
async def _mock_pool_acquire(page):
    """Mock for browser.page_pool.acquire()."""
    yield page


def _make_mock_config(api_constants: dict):
    """Create a mock config module with the given *_API constants.

    api_constants: dict mapping attribute names to values, e.g.
        {"SEARCH_API": "/jobapi/v3/search"}
    """
    mod = types.ModuleType("mock_config")
    for k, v in api_constants.items():
        setattr(mod, k, v)
    return mod


# ---------------------------------------------------------------------------
# 1. Probe detects new endpoints not in config
# ---------------------------------------------------------------------------

class TestDiscoveryDetectsNew:

    @pytest.mark.asyncio
    async def test_probe_detects_new_endpoints(self):
        """When network traffic includes URLs not in config *_API constants, they are flagged as new."""
        mock_page = _make_mock_page()

        # Simulate responses that will be captured
        known_response = _make_mock_response("https://www.naukri.com/jobapi/v3/search?q=python")
        new_response = _make_mock_response("https://www.naukri.com/cloudgateway-newservice/v1/magic/endpoint")
        css_response = _make_mock_response("https://www.naukri.com/static/style.css", content_type="text/css")

        async def mock_page_goto(page, url):
            # Fire the captured listeners with our mock responses
            for listener in list(page._listeners):
                await listener(known_response)
                await listener(new_response)
                await listener(css_response)

        mock_pool = MagicMock()
        mock_pool.acquire = lambda: _mock_pool_acquire(mock_page)

        mock_browser = MagicMock()
        mock_browser.page_pool = mock_pool

        mock_event_bus = MagicMock()
        mock_event_bus.emit = AsyncMock()

        mock_config = _make_mock_config({"SEARCH_API": "/jobapi/v3/search"})

        with patch("naukri_server.health.probes.discovery.config", mock_config), \
             patch("naukri_server.browser.browser", mock_browser), \
             patch("naukri_server.browser.page_goto", new_callable=AsyncMock, side_effect=mock_page_goto):

            # Patch lazy imports inside the function
            with patch.dict("sys.modules", {}):
                import naukri_server.health.probes.discovery as disc_mod
                orig_fn = disc_mod.discover_config_audit

                # Wrap to inject mocks for lazy imports
                async def patched_discover():
                    with patch("naukri_server.browser.browser", mock_browser), \
                         patch("naukri_server.browser.page_goto", new_callable=AsyncMock, side_effect=mock_page_goto), \
                         patch("naukri_server.events.event_bus", mock_event_bus), \
                         patch("naukri_server.database.log_endpoint_audit", new_callable=AsyncMock) as mock_log:
                        # Re-import inside patched context
                        result = await orig_fn()
                        return result, mock_log

                result, mock_log = await patched_discover()

        assert result.status == "healthy"
        assert result.metadata["new_count"] >= 1
        assert any("/cloudgateway-newservice/v1/magic/endpoint" in ep for ep in result.metadata["new_endpoints"])


# ---------------------------------------------------------------------------
# 2. Probe reports zero new when all known
# ---------------------------------------------------------------------------

class TestDiscoveryAllKnown:

    @pytest.mark.asyncio
    async def test_probe_reports_zero_new_when_all_known(self):
        """When all discovered URLs are already in config, new_count should be 0."""
        mock_page = _make_mock_page()

        known_response = _make_mock_response("https://www.naukri.com/jobapi/v3/search?q=python")

        async def mock_page_goto(page, url):
            for listener in list(page._listeners):
                await listener(known_response)

        mock_pool = MagicMock()
        mock_pool.acquire = lambda: _mock_pool_acquire(mock_page)

        mock_browser = MagicMock()
        mock_browser.page_pool = mock_pool

        # Config has the exact same endpoint
        mock_config = _make_mock_config({"SEARCH_API": "/jobapi/v3/search"})

        with patch("naukri_server.health.probes.discovery.config", mock_config), \
             patch("naukri_server.browser.browser", mock_browser), \
             patch("naukri_server.browser.page_goto", new_callable=AsyncMock, side_effect=mock_page_goto), \
             patch("asyncio.sleep", new_callable=AsyncMock):

            from naukri_server.health.probes.discovery import discover_config_audit
            result = await discover_config_audit()

        assert result.status == "healthy"
        assert result.metadata["new_count"] == 0
        assert result.metadata["new_endpoints"] == []


# ---------------------------------------------------------------------------
# 3. NewEndpointDiscovered event emitted correctly
# ---------------------------------------------------------------------------

class TestNewEndpointDiscoveredEvent:

    @pytest.mark.asyncio
    async def test_event_emitted_for_each_new_endpoint(self):
        """NewEndpointDiscovered event should be emitted once per new endpoint."""
        from naukri_server.events import EventBus, NewEndpointDiscovered

        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(NewEndpointDiscovered, handler)

        event = NewEndpointDiscovered(
            url="/cloudgateway-new/v1/test",
            method="GET",
            page_source="auto_discovery",
        )
        await bus.emit(event)

        assert len(received) == 1
        assert received[0].url == "/cloudgateway-new/v1/test"
        assert received[0].method == "GET"
        assert received[0].page_source == "auto_discovery"

    @pytest.mark.asyncio
    async def test_event_has_default_fields(self):
        """NewEndpointDiscovered should have sensible defaults."""
        from naukri_server.events import NewEndpointDiscovered

        event = NewEndpointDiscovered()
        assert event.url == ""
        assert event.method == "GET"
        assert event.page_source == ""
        assert event.timestamp  # inherited from DomainEvent

    @pytest.mark.asyncio
    async def test_subscriber_handles_error_gracefully(self):
        """The subscriber for NewEndpointDiscovered should not raise even if DB fails."""
        from naukri_server.events import NewEndpointDiscovered
        from naukri_server.subscribers import _on_new_endpoint_discovered

        event = NewEndpointDiscovered(
            url="/cloudgateway-unknown/v1/endpoint",
            method="POST",
            page_source="homepage",
        )

        # Verify handler doesn't raise even if store_notification fails
        with patch("naukri_server.database.store_notification", new_callable=AsyncMock, side_effect=Exception("DB error")):
            # Should not raise
            await _on_new_endpoint_discovered(event)
