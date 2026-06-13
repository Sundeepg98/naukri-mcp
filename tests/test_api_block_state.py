"""Tests for the block-state integration in naukri_server.api._api_request.

Focus: the BUG FIX where a 200 CAPTCHA/HTML interstitial must NOT be counted as
``api_metrics.success`` nor reset the circuit breaker (success-recording now
happens AFTER the content-type/interstitial guard), plus the api_metrics.blocks
counter and block_kind tagging.

Fully offline: the aiohttp session + token manager are mocked; no network.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import naukri_server.api as api_mod
from naukri_server.api import _api_request, NaukriAPIError, NaukriBotCheckError, api_metrics, _api_circuit


def _make_mock_response(status, json_data=None, text="", headers=None, url="", history=()):
    resp = AsyncMock()
    resp.status = status
    default_headers = {"content-type": "application/json"}
    if headers:
        default_headers.update(headers)
    resp.headers = default_headers
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    resp.url = url
    resp.history = history
    return resp


def _make_session_with_responses(responses):
    call_idx = {"i": 0}

    def make_ctx(*args, **kwargs):
        idx = min(call_idx["i"], len(responses) - 1)
        call_idx["i"] += 1
        resp = responses[idx]
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session = AsyncMock()
    session.request = MagicMock(side_effect=make_ctx)
    return session


@pytest.fixture
def reset_metrics_and_circuit():
    """Snapshot + restore api_metrics counters and the module circuit breaker."""
    before = (api_metrics.total, api_metrics.success, api_metrics.errors,
              api_metrics.retries, api_metrics.auth_refreshes, api_metrics.blocks)
    blocks_by_state = dict(api_metrics.blocks_by_state)
    # Force the circuit closed and counters to a known baseline.
    _api_circuit.record_success()
    yield
    (api_metrics.total, api_metrics.success, api_metrics.errors,
     api_metrics.retries, api_metrics.auth_refreshes, api_metrics.blocks) = before
    api_metrics.blocks_by_state = blocks_by_state
    _api_circuit.record_success()


def _patched_call(session):
    """Context-manager bundle: patched session + token manager + no real sleep."""
    mock_token_mgr = AsyncMock()
    mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
    mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")
    mock_token_mgr.refresh_via_pool = AsyncMock()
    cm = patch.multiple(
        "naukri_server.api",
        get_session=AsyncMock(return_value=session),
        asyncio=MagicMock(sleep=AsyncMock()),
    )
    return cm, mock_token_mgr


class TestSuccessRecordingBugFix:
    """A 200 that is actually an interstitial must not read as healthy."""

    @pytest.mark.asyncio
    async def test_200_html_interstitial_does_not_record_success(self, reset_metrics_and_circuit):
        """200 with text/html body (CAPTCHA interstitial) → error, NOT success."""
        resp = _make_mock_response(
            200,
            text="<html>Please complete the reCAPTCHA to continue</html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
        session = _make_session_with_responses([resp])

        success_before = api_metrics.success
        errors_before = api_metrics.errors
        blocks_before = api_metrics.blocks

        mock_token_mgr = AsyncMock()
        mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
        mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")

        with patch("naukri_server.api.get_session", AsyncMock(return_value=session)), \
             patch("naukri_server.api.browser") as mock_browser:
            mock_browser.token_manager = mock_token_mgr
            with pytest.raises(NaukriAPIError) as exc_info:
                await _api_request("GET", "/test-interstitial")

        # The core bug fix: success was NOT incremented for the interstitial.
        assert api_metrics.success == success_before
        # It was counted as an error instead.
        assert api_metrics.errors == errors_before + 1
        # And the block-state classifier counted it.
        assert api_metrics.blocks == blocks_before + 1
        # The raised error is tagged as a block (captcha marker present).
        assert exc_info.value.block_kind == "captcha"

    @pytest.mark.asyncio
    async def test_200_html_interstitial_does_not_reset_circuit(self, reset_metrics_and_circuit):
        """A soft block must record a circuit FAILURE, not a success."""
        # Prime the circuit with some failures (but not enough to open).
        _api_circuit.record_failure()
        _api_circuit.record_failure()
        failures_primed = _api_circuit._failures
        assert failures_primed == 2

        resp = _make_mock_response(
            200,
            text="Unusual activity detected from your network",
            headers={"content-type": "text/html"},
        )
        session = _make_session_with_responses([resp])

        mock_token_mgr = AsyncMock()
        mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
        mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")

        with patch("naukri_server.api.get_session", AsyncMock(return_value=session)), \
             patch("naukri_server.api.browser") as mock_browser:
            mock_browser.token_manager = mock_token_mgr
            with pytest.raises(NaukriAPIError):
                await _api_request("GET", "/test-interstitial2")

        # record_success() would have reset _failures to 0. The bug fix means a
        # soft-block 200 records a FAILURE, so the count went UP, not to 0.
        assert _api_circuit._failures == failures_primed + 1

    @pytest.mark.asyncio
    async def test_clean_200_json_still_records_success(self, reset_metrics_and_circuit):
        """Regression guard: a real JSON 200 still counts as success."""
        resp = _make_mock_response(200, json_data={"ok": True}, text='{"ok": true}')
        session = _make_session_with_responses([resp])

        success_before = api_metrics.success

        mock_token_mgr = AsyncMock()
        mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
        mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")

        with patch("naukri_server.api.get_session", AsyncMock(return_value=session)), \
             patch("naukri_server.api.browser") as mock_browser:
            mock_browser.token_manager = mock_token_mgr
            result = await _api_request("GET", "/test-ok")

        assert result == {"ok": True}
        assert api_metrics.success == success_before + 1

    @pytest.mark.asyncio
    async def test_204_no_content_records_success(self, reset_metrics_and_circuit):
        """204 has no body to classify — still a genuine success."""
        resp = _make_mock_response(204, text="")
        session = _make_session_with_responses([resp])

        success_before = api_metrics.success

        mock_token_mgr = AsyncMock()
        mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
        mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")

        with patch("naukri_server.api.get_session", AsyncMock(return_value=session)), \
             patch("naukri_server.api.browser") as mock_browser:
            mock_browser.token_manager = mock_token_mgr
            result = await _api_request("DELETE", "/test-delete")

        assert result == {}
        assert api_metrics.success == success_before + 1


class TestBlockKindTaggingOnErrorStatus:
    """Error-status responses get classified + tagged with block_kind."""

    @pytest.mark.asyncio
    async def test_406_bot_check_persists_tagged_soft_block(self, reset_metrics_and_circuit):
        """406 twice (refresh + retry both 406) → NaukriBotCheckError, block_kind set."""
        resp_a = _make_mock_response(406, text="Access Denied", headers={"content-type": "text/html"})
        resp_b = _make_mock_response(406, text="Access Denied", headers={"content-type": "text/html"})
        session = _make_session_with_responses([resp_a, resp_b])

        blocks_before = api_metrics.blocks

        mock_token_mgr = AsyncMock()
        mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
        mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")
        mock_token_mgr.refresh_via_pool = AsyncMock()

        with patch("naukri_server.api.get_session", AsyncMock(return_value=session)), \
             patch("naukri_server.api.browser") as mock_browser, \
             patch("naukri_server.api.asyncio.sleep", new_callable=AsyncMock):
            mock_browser.token_manager = mock_token_mgr
            with pytest.raises(NaukriBotCheckError) as exc_info:
                await _api_request("GET", "/test-bot")

        assert exc_info.value.block_kind == "soft_block"
        # Two 406 responses, each classified → blocks counter advanced.
        assert api_metrics.blocks >= blocks_before + 1

    @pytest.mark.asyncio
    async def test_500_error_not_tagged_as_block(self, reset_metrics_and_circuit):
        """A genuine 5xx (after retries) is an API error, not a block."""
        # API_MAX_RETRIES=2 → 3 total attempts all 500.
        resps = [_make_mock_response(500, text="Internal Server Error") for _ in range(4)]
        session = _make_session_with_responses(resps)

        blocks_before = api_metrics.blocks

        mock_token_mgr = AsyncMock()
        mock_token_mgr.ensure_token = AsyncMock(return_value="fake-token")
        mock_token_mgr.get_cookies = MagicMock(return_value="cookie=val")

        with patch("naukri_server.api.get_session", AsyncMock(return_value=session)), \
             patch("naukri_server.api.browser") as mock_browser, \
             patch("naukri_server.api.asyncio.sleep", new_callable=AsyncMock):
            mock_browser.token_manager = mock_token_mgr
            with pytest.raises(NaukriAPIError) as exc_info:
                await _api_request("GET", "/test-500")

        # 500 plain text is not a block marker → block_kind stays None.
        assert exc_info.value.block_kind is None
        assert api_metrics.blocks == blocks_before
