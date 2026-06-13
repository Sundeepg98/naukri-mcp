"""Integration tests: kill-switch wiring into apply + auto-hunt paths.

Offline: api_client / DB writes are mocked. Verifies:
  - a tripped switch HALTS _apply_single and naukri_auto_hunt (fail-closed)
  - an apply that hits a block-tagged NaukriAPIError TRIPS the switch
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server import kill_switch
from naukri_server.api import NaukriBotCheckError


@pytest.fixture
def fresh_switch(tmp_path):
    state_path = tmp_path / "kill_switch_state.json"
    kill_switch._set_state_path_for_tests(state_path)
    kill_switch._reset_for_tests()
    yield
    kill_switch._reset_for_tests()


class TestApplyHaltsWhenTripped:
    @pytest.mark.asyncio
    async def test_apply_single_returns_halted_when_tripped(self, fresh_switch):
        from naukri_server.tools.apply import _apply_single

        kill_switch.trip("pre-tripped for test", block_kind="soft_block")

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock) as rec:
            result = await _apply_single("12345", title="Dev", company="Acme")

        assert result["status"] == "halted"
        assert result["error_code"] == "KILL_SWITCH_TRIPPED"
        assert result["block_kind"] == "soft_block"
        # It recorded the halt and never called the apply API.
        rec.assert_awaited()

    @pytest.mark.asyncio
    async def test_apply_single_does_not_call_api_when_tripped(self, fresh_switch):
        from naukri_server.tools.apply import _apply_single

        kill_switch.trip("tripped")

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock), \
             patch("naukri_server.interfaces.api_client.post", new_callable=AsyncMock) as post:
            result = await _apply_single("999")

        assert result["status"] == "halted"
        post.assert_not_called()


class TestApplyTripsOnBlock:
    @pytest.mark.asyncio
    async def test_block_tagged_error_trips_switch(self, fresh_switch):
        from naukri_server.tools.apply import _apply_single

        assert kill_switch.is_tripped() is False

        block_err = NaukriBotCheckError(406, "Akamai bot-check on apply", block_kind="captcha")

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock), \
             patch("naukri_server.tools.apply.get_apply_rate_limiter") as grl, \
             patch("naukri_server.tools.apply._load_cache", return_value={}), \
             patch("naukri_server.interfaces.api_client.post", new_callable=AsyncMock,
                   side_effect=block_err), \
             patch("naukri_server.kill_switch.trip_and_halt", new_callable=AsyncMock) as halt:
            grl.return_value = AsyncMock()
            # Route the trip through the real trip() so is_tripped flips, but
            # stub trip_and_halt's heavy side effects.
            async def _do_halt(reason, *, block_kind=""):
                kill_switch.trip(reason, block_kind=block_kind)
                return {"first_trip": True}
            halt.side_effect = _do_halt

            result = await _apply_single("555", title="X", company="Y")

        assert result["status"] == "halted"
        assert result["block_kind"] == "captcha"
        halt.assert_awaited_once()
        assert kill_switch.is_tripped() is True

    @pytest.mark.asyncio
    async def test_non_block_api_error_does_not_trip(self, fresh_switch):
        from naukri_server.tools.apply import _apply_single
        from naukri_server.api import NaukriAPIError

        plain_err = NaukriAPIError(500, "Internal Server Error")  # no block_kind

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock), \
             patch("naukri_server.tools.apply.get_apply_rate_limiter") as grl, \
             patch("naukri_server.tools.apply._load_cache", return_value={}), \
             patch("naukri_server.interfaces.api_client.post", new_callable=AsyncMock,
                   side_effect=plain_err):
            grl.return_value = AsyncMock()
            result = await _apply_single("777")

        assert result["status"] == "error"
        assert result["error_code"] == "API_ERROR"
        assert kill_switch.is_tripped() is False


class TestAutoHuntHaltsWhenTripped:
    @pytest.mark.asyncio
    async def test_auto_hunt_returns_halted_when_tripped(self, fresh_switch):
        from naukri_server.tools.auto_hunt import naukri_auto_hunt

        kill_switch.trip("blocked", block_kind="login_wall")

        # Should short-circuit BEFORE any search/profile call.
        with patch("naukri_server.tools.search.naukri_search_jobs", new_callable=AsyncMock) as search:
            result = await naukri_auto_hunt(keywords="python", limit=5)

        assert result["status"] == "halted"
        assert result["error_code"] == "KILL_SWITCH_TRIPPED"
        assert result["block_kind"] == "login_wall"
        search.assert_not_called()
