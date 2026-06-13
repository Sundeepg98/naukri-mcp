"""Tests for verify-after-apply read-back (config.VERIFY_APPLY_READBACK).

Offline: api_client + DB mocked. Verifies that with read-back enabled, an apply
that the history confirms stays "applied", an apply NOT found downgrades to
"applied_unverified", and a read failure stays "applied" (unverified, never a
false failure). With read-back disabled the behavior is unchanged.
"""

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server import kill_switch


@pytest.fixture
def fresh_switch(tmp_path):
    state_path = tmp_path / "kill_switch_state.json"
    kill_switch._set_state_path_for_tests(state_path)
    kill_switch._reset_for_tests()
    yield
    kill_switch._reset_for_tests()


def _apply_post_ok():
    """api_client.post return for a clean phase-1 apply success."""
    return {"jobs": [{"status": 200}], "quotaDetails": {"dailyApplied": 1}}


class TestVerifyReadbackHelper:
    @pytest.mark.asyncio
    async def test_found_returns_true(self):
        from naukri_server.tools.apply import _verify_apply_readback
        with patch("naukri_server.interfaces.api_client.get", new_callable=AsyncMock,
                   return_value={"applyDetails": [{"jobId": "123"}], "matchingRowsCount": 1}):
            assert await _verify_apply_readback("123") is True

    @pytest.mark.asyncio
    async def test_absent_returns_false(self):
        from naukri_server.tools.apply import _verify_apply_readback
        with patch("naukri_server.interfaces.api_client.get", new_callable=AsyncMock,
                   return_value={"applyDetails": [{"jobId": "999"}], "matchingRowsCount": 1}):
            assert await _verify_apply_readback("123") is False

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_none(self):
        from naukri_server.tools.apply import _verify_apply_readback
        with patch("naukri_server.interfaces.api_client.get", new_callable=AsyncMock,
                   side_effect=RuntimeError("read failed")):
            assert await _verify_apply_readback("123") is None


class TestApplyWithReadbackEnabled:
    @pytest.mark.asyncio
    async def test_confirmed_apply_stays_applied(self, fresh_switch):
        from naukri_server.tools import apply as apply_mod

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock), \
             patch("naukri_server.database.count_daily_applied", new_callable=AsyncMock, return_value=0), \
             patch("naukri_server.tools.apply.get_apply_rate_limiter") as grl, \
             patch("naukri_server.tools.apply._load_cache", return_value={}), \
             patch("naukri_server.config.VERIFY_APPLY_READBACK", True), \
             patch("naukri_server.tools.apply.api_client.post", new_callable=AsyncMock,
                   return_value=_apply_post_ok()), \
             patch("naukri_server.tools.apply._verify_apply_readback", new_callable=AsyncMock,
                   return_value=True):
            grl.return_value = AsyncMock()
            result = await apply_mod._apply_single("123", title="T", company="C")

        assert result["status"] == "applied"
        assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_unconfirmed_apply_downgrades(self, fresh_switch):
        from naukri_server.tools import apply as apply_mod

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock), \
             patch("naukri_server.database.count_daily_applied", new_callable=AsyncMock, return_value=0), \
             patch("naukri_server.tools.apply.get_apply_rate_limiter") as grl, \
             patch("naukri_server.tools.apply._load_cache", return_value={}), \
             patch("naukri_server.config.VERIFY_APPLY_READBACK", True), \
             patch("naukri_server.tools.apply.api_client.post", new_callable=AsyncMock,
                   return_value=_apply_post_ok()), \
             patch("naukri_server.tools.apply._verify_apply_readback", new_callable=AsyncMock,
                   return_value=False):
            grl.return_value = AsyncMock()
            result = await apply_mod._apply_single("123", title="T", company="C")

        assert result["status"] == "applied_unverified"
        assert result["verified"] is False

    @pytest.mark.asyncio
    async def test_unverifiable_apply_stays_applied(self, fresh_switch):
        """Read-back fetch failed (None) → still 'applied', not a false failure."""
        from naukri_server.tools import apply as apply_mod

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock), \
             patch("naukri_server.database.count_daily_applied", new_callable=AsyncMock, return_value=0), \
             patch("naukri_server.tools.apply.get_apply_rate_limiter") as grl, \
             patch("naukri_server.tools.apply._load_cache", return_value={}), \
             patch("naukri_server.config.VERIFY_APPLY_READBACK", True), \
             patch("naukri_server.tools.apply.api_client.post", new_callable=AsyncMock,
                   return_value=_apply_post_ok()), \
             patch("naukri_server.tools.apply._verify_apply_readback", new_callable=AsyncMock,
                   return_value=None):
            grl.return_value = AsyncMock()
            result = await apply_mod._apply_single("123", title="T", company="C")

        assert result["status"] == "applied"
        assert "verified" not in result  # no confidence flag when unverifiable


class TestApplyWithReadbackDisabled:
    @pytest.mark.asyncio
    async def test_no_readback_call_when_disabled(self, fresh_switch):
        from naukri_server.tools import apply as apply_mod

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock), \
             patch("naukri_server.database.count_daily_applied", new_callable=AsyncMock, return_value=0), \
             patch("naukri_server.tools.apply.get_apply_rate_limiter") as grl, \
             patch("naukri_server.tools.apply._load_cache", return_value={}), \
             patch("naukri_server.config.VERIFY_APPLY_READBACK", False), \
             patch("naukri_server.tools.apply.api_client.post", new_callable=AsyncMock,
                   return_value=_apply_post_ok()), \
             patch("naukri_server.tools.apply._verify_apply_readback", new_callable=AsyncMock) as verify:
            grl.return_value = AsyncMock()
            result = await apply_mod._apply_single("123", title="T", company="C")

        assert result["status"] == "applied"
        assert "verified" not in result
        verify.assert_not_called()
