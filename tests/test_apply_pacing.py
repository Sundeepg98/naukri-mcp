"""Tests for apply-path pacing (stealth > throughput).

Covers:
  - resilience.sample_think_time / human_think_time (distribution + bounds)
  - the HARD daily-quota circuit in _apply_single
  - batch_apply stopping early on a halt / quota_reached

Fully offline.
"""

import math

import pytest
from unittest.mock import AsyncMock, patch

from naukri_server import resilience
from naukri_server import kill_switch


# ---------------------------------------------------------------------------
# Think-time distribution
# ---------------------------------------------------------------------------


class TestSampleThinkTime:
    def test_clamps_to_min(self):
        # Force a tiny sample → clamped up to min.
        v = resilience.sample_think_time(7.0, 0.6, 3.0, 20.0,
                                         rand_func=lambda mu, s: 0.001)
        assert v == 3.0

    def test_clamps_to_max(self):
        v = resilience.sample_think_time(7.0, 0.6, 3.0, 20.0,
                                         rand_func=lambda mu, s: 9999.0)
        assert v == 20.0

    def test_median_passthrough_in_range(self):
        # A sample equal to the median (exp(mu)) stays as-is when within bounds.
        v = resilience.sample_think_time(7.0, 0.6, 3.0, 20.0,
                                         rand_func=lambda mu, s: math.exp(mu))
        assert v == pytest.approx(7.0)

    def test_mu_is_log_of_median(self):
        captured = {}

        def rf(mu, s):
            captured["mu"] = mu
            captured["sigma"] = s
            return math.exp(mu)

        resilience.sample_think_time(7.0, 0.6, 3.0, 20.0, rand_func=rf)
        assert captured["mu"] == pytest.approx(math.log(7.0))
        assert captured["sigma"] == 0.6

    def test_rand_func_error_falls_back_to_median(self):
        def boom(mu, s):
            raise ValueError("bad params")
        v = resilience.sample_think_time(7.0, 0.6, 3.0, 20.0, rand_func=boom)
        assert v == pytest.approx(7.0)

    def test_distribution_is_variable(self):
        """Real RNG: samples vary (not a constant) and stay within bounds."""
        import random
        random.seed(1234)
        samples = [resilience.sample_think_time(7.0, 0.6, 3.0, 20.0) for _ in range(200)]
        assert all(3.0 <= s <= 20.0 for s in samples)
        assert len(set(round(s, 3) for s in samples)) > 50  # genuinely varied

    @pytest.mark.asyncio
    async def test_human_think_time_sleeps_sampled_value(self):
        slept = []

        async def fake_sleep(d):
            slept.append(d)

        out = await resilience.human_think_time(
            7.0, 0.6, 3.0, 20.0,
            sleep_func=fake_sleep, rand_func=lambda mu, s: 5.0,
        )
        assert out == 5.0
        assert slept == [5.0]


# ---------------------------------------------------------------------------
# HARD daily-quota circuit
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_switch(tmp_path):
    state_path = tmp_path / "kill_switch_state.json"
    kill_switch._set_state_path_for_tests(state_path)
    kill_switch._reset_for_tests()
    yield
    kill_switch._reset_for_tests()


class TestDailyQuotaCircuit:
    @pytest.mark.asyncio
    async def test_apply_blocked_at_quota(self, fresh_switch):
        from naukri_server.tools.apply import _apply_single
        from naukri_server.config import DAILY_APPLY_QUOTA

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock), \
             patch("naukri_server.database.count_daily_applied", new_callable=AsyncMock,
                   return_value=DAILY_APPLY_QUOTA), \
             patch("naukri_server.interfaces.api_client.post", new_callable=AsyncMock) as post:
            result = await _apply_single("123", title="T", company="C")

        assert result["status"] == "quota_reached"
        assert result["error_code"] == "DAILY_QUOTA_REACHED"
        assert result["daily_quota"] == DAILY_APPLY_QUOTA
        # Hard stop: the API was never called.
        post.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_allowed_below_quota(self, fresh_switch):
        from naukri_server.tools.apply import _apply_single

        with patch("naukri_server.tools.apply.record_application", new_callable=AsyncMock), \
             patch("naukri_server.database.count_daily_applied", new_callable=AsyncMock,
                   return_value=0), \
             patch("naukri_server.tools.apply.get_apply_rate_limiter") as grl, \
             patch("naukri_server.tools.apply._load_cache", return_value={}), \
             patch("naukri_server.interfaces.api_client.post", new_callable=AsyncMock,
                   return_value={"jobs": [{"status": 200}]}):
            grl.return_value = AsyncMock()
            result = await _apply_single("123", title="T", company="C")

        assert result["status"] == "applied"

    @pytest.mark.asyncio
    async def test_quota_check_failure_fails_open(self, fresh_switch):
        """A DB hiccup on the count must not wedge applies (fail-open here)."""
        from naukri_server.tools.apply import _daily_quota_exceeded

        with patch("naukri_server.database.count_daily_applied", new_callable=AsyncMock,
                   side_effect=RuntimeError("db down")):
            exceeded, applied = await _daily_quota_exceeded()
        assert exceeded is False
        assert applied == 0


# ---------------------------------------------------------------------------
# batch_apply stops early on halt / quota
# ---------------------------------------------------------------------------


class TestBatchStopsEarly:
    @pytest.mark.asyncio
    async def test_batch_stops_on_halt(self, fresh_switch):
        import naukri_server.tools.apply as apply_mod

        search_result = {
            "status": "success",
            "jobs": [
                {"job_id": f"J{i}", "title": "T", "company": "C", "is_applied": False}
                for i in range(5)
            ],
        }

        calls = {"n": 0}

        async def fake_apply_single(job_id, *a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                return {"status": "halted", "job_id": job_id,
                        "error_code": "KILL_SWITCH_TRIPPED", "block_kind": "captcha"}
            return {"status": "applied", "job_id": job_id}

        async def no_think(*a, **k):
            return 0.0

        with (
            patch("naukri_server.tools.search.naukri_search_jobs",
                  new_callable=AsyncMock, return_value=search_result),
            patch("naukri_server.database.get_applied_job_ids",
                  new_callable=AsyncMock, return_value=set()),
            patch("naukri_server.tools.apply._apply_single",
                  new_callable=AsyncMock, side_effect=fake_apply_single),
            patch("naukri_server.tools.apply.human_think_time", side_effect=no_think),
        ):
            result = await apply_mod.naukri_batch_apply(keywords="python", limit=5)

        # Stopped after the 2nd job (the halt) — jobs 3-5 not attempted.
        assert calls["n"] == 2
        assert result["status"] == "halted"
        assert result["halted_early"] is True
        assert result["applied"] == 1
        assert result["not_attempted"] == 3

    @pytest.mark.asyncio
    async def test_batch_stops_on_quota(self, fresh_switch):
        import naukri_server.tools.apply as apply_mod

        search_result = {
            "status": "success",
            "jobs": [
                {"job_id": f"J{i}", "title": "T", "company": "C", "is_applied": False}
                for i in range(3)
            ],
        }

        async def fake_apply_single(job_id, *a, **k):
            return {"status": "quota_reached", "job_id": job_id,
                    "error_code": "DAILY_QUOTA_REACHED"}

        async def no_think(*a, **k):
            return 0.0

        with (
            patch("naukri_server.tools.search.naukri_search_jobs",
                  new_callable=AsyncMock, return_value=search_result),
            patch("naukri_server.database.get_applied_job_ids",
                  new_callable=AsyncMock, return_value=set()),
            patch("naukri_server.tools.apply._apply_single",
                  new_callable=AsyncMock, side_effect=fake_apply_single),
            patch("naukri_server.tools.apply.human_think_time", side_effect=no_think),
        ):
            result = await apply_mod.naukri_batch_apply(keywords="python", limit=5)

        assert result["status"] == "quota_reached"
        assert result["applied"] == 0
