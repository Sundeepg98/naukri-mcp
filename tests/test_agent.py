"""Tests for autonomous agent — config, helpers, OODA cycle, tool, and scheduler.

Every test is PURE: no network, no browser, no file I/O.
We mock all async helpers (DB, events, auto_hunt, apply) and verify
config management, blocklist/quiet-hours logic, the four saga steps
(observe, decide, act, learn), the MCP tool dispatch, and scheduler wiring.
"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock, MagicMock, mock_open

import pytest

from naukri_server.agent import (
    DEFAULT_CONFIG,
    config_hash,
)
from naukri_server.domain.agent import (
    AgentBlocklist,
    AgentCandidate,
    QuietHours,
)


# =====================================================================
# Helpers
# =====================================================================

def _make_config(**overrides) -> dict:
    """Return a copy of DEFAULT_CONFIG with overrides applied."""
    config = DEFAULT_CONFIG.copy()
    config.update(overrides)
    return config


def _make_job(job_id="J100", title="Backend Dev", company="GoodCorp", fit_score=85):
    """Return a minimal ranked-job dict."""
    return {"job_id": job_id, "title": title, "company": company, "fit_score": fit_score}


def _make_candidate(job_id="J100", title="Backend Dev", company="GoodCorp", fit_score=85, search_name="Test"):
    """Return an AgentCandidate domain object for tests."""
    return AgentCandidate(job_id=job_id, title=title, company=company, fit_score=fit_score, search_name=search_name)


# =====================================================================
# 1. Config management
# =====================================================================


class TestConfigManagement:
    """Tests for load_agent_config, save_agent_config, config_hash."""

    def test_load_config_defaults(self):
        """When config file does not exist, load_agent_config returns DEFAULT_CONFIG."""
        with patch("naukri_server.agent.CONFIG_PATH") as mock_path:
            mock_path.exists.return_value = False
            from naukri_server.agent import load_agent_config
            config = load_agent_config()

        assert config["enabled"] is False
        assert config["mode"] == "approval"
        assert config["max_daily_applications"] == 15
        assert config["min_fit_score"] == 70

    def test_load_config_from_file(self):
        """When config file exists, load_agent_config reads and merges with defaults."""
        file_data = json.dumps({"enabled": True, "mode": "auto", "min_fit_score": 80})
        with patch("naukri_server.agent.CONFIG_PATH") as mock_path:
            mock_path.exists.return_value = True
            with patch("builtins.open", mock_open(read_data=file_data)):
                from naukri_server.agent import load_agent_config
                config = load_agent_config()

        assert config["enabled"] is True
        assert config["mode"] == "auto"
        assert config["min_fit_score"] == 80
        # Defaults for keys not in file
        assert config["max_daily_applications"] == 15
        assert "blocklist" in config

    def test_save_config(self):
        """save_agent_config writes JSON to CONFIG_PATH."""
        m = mock_open()
        with patch("naukri_server.agent.CONFIG_PATH") as mock_path:
            mock_path.parent.mkdir = MagicMock()
            with patch("builtins.open", m):
                from naukri_server.agent import save_agent_config
                save_agent_config({"enabled": True, "mode": "auto"})

        m.assert_called_once()
        # Verify json.dump was called via the written data
        handle = m()
        handle.write.assert_called()

    def test_config_hash_deterministic(self):
        """Same config dict always produces the same hash."""
        cfg = {"enabled": True, "mode": "auto", "min_fit_score": 75}
        assert config_hash(cfg) == config_hash(cfg)

    def test_config_hash_different(self):
        """Different configs produce different hashes."""
        cfg_a = {"enabled": True, "mode": "auto"}
        cfg_b = {"enabled": True, "mode": "approval"}
        assert config_hash(cfg_a) != config_hash(cfg_b)


# =====================================================================
# 2. Helpers — blocklist, quiet hours
# =====================================================================


class TestHelpers:
    """Tests for AgentBlocklist and QuietHours domain objects."""

    def test_blocklist_company(self):
        """Company in blocklist returns reason string containing company name."""
        bl = AgentBlocklist(companies=("TCS", "Infosys"), title_keywords=(), enabled=True)
        reason = bl.check("TCS Digital", "Dev")
        assert reason is not None
        assert "company_blocklist" in reason
        assert "TCS" in reason

    def test_blocklist_title(self):
        """Title keyword in blocklist returns reason string."""
        bl = AgentBlocklist(companies=(), title_keywords=("intern",), enabled=True)
        reason = bl.check("Google", "Software Intern")
        assert reason is not None
        assert "title_blocklist" in reason
        assert "intern" in reason

    def test_blocklist_not_blocked(self):
        """Clean job returns None."""
        bl = AgentBlocklist(companies=("TCS",), title_keywords=("intern",), enabled=True)
        assert bl.check("Google", "Senior Backend Developer") is None

    def test_blocklist_disabled(self):
        """When blocklist.enabled=False, always returns None."""
        bl = AgentBlocklist(companies=("TCS",), title_keywords=("intern",), enabled=False)
        assert bl.check("TCS", "Intern") is None

    def test_quiet_hours_during_quiet(self):
        """During quiet hours (hour=22, window 20-8) returns True."""
        qh = QuietHours(enabled=True, start_hour=20, end_hour=8)
        fake_now = datetime(2026, 4, 5, 22, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        with patch("naukri_server.domain.agent.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = qh.is_active
        assert result is True

    def test_quiet_hours_during_active(self):
        """During active hours (hour=10, window 20-8) returns False."""
        qh = QuietHours(enabled=True, start_hour=20, end_hour=8)
        fake_now = datetime(2026, 4, 5, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        with patch("naukri_server.domain.agent.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = qh.is_active
        assert result is False

    def test_quiet_hours_disabled(self):
        """When quiet_hours.enabled=False, returns False regardless of hour."""
        qh = QuietHours(enabled=False, start_hour=20, end_hour=8)
        assert qh.is_active is False


# =====================================================================
# 3. Agent core cycle — observe, decide, act, learn, run_agent_cycle
# =====================================================================


@pytest.fixture
def policy_already_seen(monkeypatch, tmp_path):
    """Tell the agent it has already run under the CURRENT scoring policy.

    Without this, `_act` downgrades an "auto" cycle to "approval" — correctly:
    a cycle that has never observed a scoring fingerprint shows the list once
    before acting on it. Two tests below exercise auto-mode APPLY behaviour and
    would otherwise be testing the downgrade instead.

    They passed before this fixture existed only because a previous run had left
    `agent_policy_state.json` in the repo root carrying the current hash — which
    is exactly the ambient-state dependence `tests/test_no_tree_writes.py` now
    prevents.
    """
    import json as _json

    from naukri_server import agent as _agent

    state = tmp_path / "agent_policy_state.json"
    monkeypatch.setattr(_agent, "POLICY_STATE_PATH", state)
    state.write_text(
        _json.dumps({"last_scoring_hash": _agent.current_scoring_hash()}),
        encoding="utf-8",
    )
    return state


class TestAgentCoreCycle:
    """Tests for the four saga steps and the main run_agent_cycle."""

    # -- run_agent_cycle guards --

    async def test_run_agent_cycle_disabled(self):
        """When enabled=False, run_agent_cycle returns skipped."""
        with patch("naukri_server.agent.load_agent_config",
                   return_value=_make_config(enabled=False)):
            from naukri_server.agent import run_agent_cycle
            result = await run_agent_cycle()

        assert result["status"] == "skipped"
        assert "disabled" in result["reason"].lower()

    async def test_run_agent_cycle_quiet_hours(self):
        """During quiet hours, run_agent_cycle returns skipped."""
        mock_qh = MagicMock()
        mock_qh.is_active = True
        with patch("naukri_server.agent.load_agent_config",
                   return_value=_make_config(enabled=True)), \
             patch("naukri_server.agent.QuietHours") as MockQH:
            MockQH.from_config.return_value = mock_qh
            from naukri_server.agent import run_agent_cycle
            result = await run_agent_cycle()

        assert result["status"] == "skipped"
        assert "quiet" in result["reason"].lower()

    async def test_run_agent_cycle_daily_limit_reached(self):
        """When daily limit reached, run_agent_cycle returns skipped after observe."""
        mock_qh = MagicMock()
        mock_qh.is_active = False
        with patch("naukri_server.agent.load_agent_config",
                   return_value=_make_config(enabled=True, max_daily_applications=5)), \
             patch("naukri_server.agent.QuietHours") as MockQH, \
             patch("naukri_server.database.insert_agent_run",
                   new_callable=AsyncMock, return_value=1), \
             patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value=set()), \
             patch("naukri_server.database.count_daily_applied",
                   new_callable=AsyncMock, return_value=5), \
             patch("naukri_server.database.update_agent_run",
                   new_callable=AsyncMock), \
             patch("naukri_server.events.event_bus") as mock_bus:
            MockQH.from_config.return_value = mock_qh
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import run_agent_cycle
            result = await run_agent_cycle()

        assert result["status"] == "skipped"
        assert "limit" in result["reason"].lower()
        assert result["daily_applied"] == 5

    # -- _observe --

    async def test_observe_returns_context(self):
        """_observe returns applied_ids, daily_applied, daily_remaining."""
        with patch("naukri_server.database.get_applied_job_ids",
                   new_callable=AsyncMock, return_value={"J1", "J2"}), \
             patch("naukri_server.database.count_daily_applied",
                   new_callable=AsyncMock, return_value=3), \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _observe
            config = _make_config(max_daily_applications=10)
            result = await _observe("cycle_test", config)

        assert result["applied_ids"] == {"J1", "J2"}
        assert result["daily_applied"] == 3
        assert result["daily_remaining"] == 7
        assert result["cycle_id"] == "cycle_test"

    # -- _decide --

    async def test_decide_filters_already_applied(self):
        """Job already in applied_ids is skipped with reason='already_applied'."""
        obs = {
            "cycle_id": "c1",
            "config": _make_config(
                searches=[{"name": "Test", "keywords": "node", "enabled": True}],
                blocklist={"enabled": False, "companies": [], "title_keywords": []},
            ),
            "applied_ids": {"J100"},
            "daily_applied": 0,
            "daily_remaining": 10,
        }
        hunt_result = {
            "status": "success",
            "jobs_found": 1,
            "jobs_matched": 1,
            "ranked_jobs": [_make_job(job_id="J100")],
        }

        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=hunt_result), \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock, return_value=1) as mock_decision, \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _decide
            result = await _decide(obs)

        assert len(result["candidates"]) == 0
        # Verify decision was inserted as skip with already_applied
        mock_decision.assert_awaited_once()
        call_kwargs = mock_decision.call_args
        assert call_kwargs[0][2] == "skip"  # decision
        assert call_kwargs[1].get("skip_reason") == "already_applied"

    async def test_decide_filters_blocklist(self):
        """Blocked company is skipped with reason containing 'company_blocklist'."""
        obs = {
            "cycle_id": "c2",
            "config": _make_config(
                searches=[{"name": "Test", "keywords": "node", "enabled": True}],
                blocklist={"enabled": True, "companies": ["TCS"], "title_keywords": []},
            ),
            "applied_ids": set(),
            "daily_applied": 0,
            "daily_remaining": 10,
        }
        hunt_result = {
            "status": "success",
            "jobs_found": 1,
            "jobs_matched": 1,
            "ranked_jobs": [_make_job(job_id="J200", company="TCS")],
        }

        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=hunt_result), \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock, return_value=1) as mock_decision, \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _decide
            result = await _decide(obs)

        assert len(result["candidates"]) == 0
        # Verify the skip decision
        call_args = mock_decision.call_args_list[0]
        assert call_args[0][2] == "skip"
        assert "company_blocklist" in call_args[1].get("skip_reason", "")

    async def test_decide_respects_daily_cap(self):
        """When more candidates than remaining budget, excess skipped with 'daily_cap'."""
        obs = {
            "cycle_id": "c3",
            "config": _make_config(
                searches=[{"name": "Test", "keywords": "node", "enabled": True}],
                blocklist={"enabled": False, "companies": [], "title_keywords": []},
            ),
            "applied_ids": set(),
            "daily_applied": 0,
            "daily_remaining": 1,  # Only 1 slot
        }
        hunt_result = {
            "status": "success",
            "jobs_found": 2,
            "jobs_matched": 2,
            "ranked_jobs": [
                _make_job(job_id="J300", company="A"),
                _make_job(job_id="J301", company="B"),
            ],
        }

        with patch("naukri_server.tools.auto_hunt.naukri_auto_hunt",
                   new_callable=AsyncMock, return_value=hunt_result), \
             patch("naukri_server.database.insert_agent_decision",
                   new_callable=AsyncMock, return_value=1) as mock_decision, \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _decide
            result = await _decide(obs)

        # 1 accepted, 1 capped
        assert len(result["candidates"]) == 1
        # Find the daily_cap skip call
        cap_calls = [
            c for c in mock_decision.call_args_list
            if c[1].get("skip_reason") == "daily_cap"
        ]
        assert len(cap_calls) == 1

    # -- _act --

    async def test_act_dry_run(self):
        """In dry_run mode, no applications are made."""
        decide_result = {
            "cycle_id": "c4",
            "config": _make_config(mode="dry_run"),
            "candidates": [_make_candidate(job_id="J400")],
            "applied_ids": set(),
            "daily_applied": 0,
            "daily_remaining": 10,
        }

        with patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _act
            result = await _act(decide_result)

        assert result["applied"] == 0
        assert result["mode"] == "dry_run"
        assert "dry run" in result["note"].lower()

    async def test_act_approval_mode(self):
        """In approval mode, notification is stored, no applications made."""
        decide_result = {
            "cycle_id": "c5",
            "config": _make_config(mode="approval"),
            "candidates": [_make_candidate(job_id="J500")],
            "applied_ids": set(),
            "daily_applied": 0,
            "daily_remaining": 10,
        }

        with patch("naukri_server.database.store_notification",
                   new_callable=AsyncMock) as mock_notif, \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _act
            result = await _act(decide_result)

        assert result["applied"] == 0
        assert result["mode"] == "approval"
        assert result["pending_approval"] == 1
        mock_notif.assert_awaited_once()

    async def test_act_auto_mode(self, policy_already_seen):
        """In auto mode, _apply_single is called for each candidate."""
        decide_result = {
            "cycle_id": "c6",
            "config": _make_config(mode="auto"),
            "candidates": [
                _make_candidate(job_id="J600", company="A"),
                _make_candidate(job_id="J601", company="B"),
            ],
            "applied_ids": set(),
            "daily_applied": 0,
            "daily_remaining": 10,
        }

        with patch("naukri_server.tools.apply._apply_single",
                   new_callable=AsyncMock,
                   return_value={"status": "applied"}) as mock_apply, \
             patch("naukri_server.database.update_agent_decision",
                   new_callable=AsyncMock), \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _act
            result = await _act(decide_result)

        assert result["applied"] == 2
        assert result["mode"] == "auto"
        assert mock_apply.await_count == 2

    async def test_act_auto_handles_apply_error(self, policy_already_seen):
        """In auto mode, _apply_single raising an exception is caught gracefully."""
        decide_result = {
            "cycle_id": "c7",
            "config": _make_config(mode="auto"),
            "candidates": [_make_candidate(job_id="J700")],
            "applied_ids": set(),
            "daily_applied": 0,
            "daily_remaining": 10,
        }

        with patch("naukri_server.tools.apply._apply_single",
                   new_callable=AsyncMock,
                   side_effect=RuntimeError("Browser crashed")), \
             patch("naukri_server.database.update_agent_decision",
                   new_callable=AsyncMock) as mock_update, \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _act
            result = await _act(decide_result)

        assert result["applied"] == 0
        assert result["failed"] == 1
        assert len(result["errors"]) == 1
        assert "Browser crashed" in result["errors"][0]
        # Decision should be updated to "error"
        mock_update.assert_awaited_once_with("c7", "J700", "error")

    async def test_act_no_candidates(self):
        """When there are no candidates, _act returns early with a note."""
        decide_result = {
            "cycle_id": "c8",
            "config": _make_config(mode="auto"),
            "candidates": [],
            "applied_ids": set(),
            "daily_applied": 0,
            "daily_remaining": 10,
        }

        with patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _act
            result = await _act(decide_result)

        assert result["applied"] == 0
        assert "no candidates" in result["note"].lower()

    # -- _learn --

    async def test_learn_updates_db(self):
        """_learn calls update_agent_run with correct fields and emits event."""
        act_result = {
            "cycle_id": "c9",
            "config": _make_config(),
            "candidates": [_make_candidate(job_id="J900")],
            "applied": 1,
            "searches_run": 1,
            "total_found": 5,
            "total_matched": 3,
            "mode": "auto",
            "_duration_ms": 1234.5,
        }

        with patch("naukri_server.database.update_agent_run",
                   new_callable=AsyncMock) as mock_update, \
             patch("naukri_server.events.event_bus") as mock_bus:
            mock_bus.emit = AsyncMock()
            from naukri_server.agent import _learn
            result = await _learn(act_result)

        assert result["cycle_id"] == "c9"
        assert result["applied"] == 1
        assert result["skipped"] == 0  # 1 candidate - 1 applied = 0
        assert result["mode"] == "auto"

        mock_update.assert_awaited_once()
        call_kw = mock_update.call_args[1]
        assert call_kw["status"] == "completed"
        assert call_kw["applied_count"] == 1
        assert call_kw["searches_run"] == 1
        assert call_kw["jobs_found"] == 5
        assert call_kw["duration_ms"] == 1234.5


# =====================================================================
# 4. Agent tool tests
# =====================================================================


class TestAgentTool:
    """Tests for the naukri_agent MCP tool — action dispatch and helpers."""

    async def test_agent_tool_status(self):
        """status action returns enabled, mode, recent_runs."""
        from naukri_server.tools.agent_tool import _agent_status

        with patch("naukri_server.agent.load_agent_config",
                   return_value=_make_config(enabled=True, mode="auto")), \
             patch("naukri_server.database.list_agent_runs",
                   new_callable=AsyncMock, return_value=[]):
            result = await _agent_status()

        assert result["status"] == "success"
        assert result["enabled"] is True
        assert result["mode"] == "auto"
        assert result["recent_runs"] == []

    async def test_agent_tool_config(self):
        """config action returns the full configuration."""
        from naukri_server.tools.agent_tool import _agent_config

        with patch("naukri_server.agent.load_agent_config",
                   return_value=_make_config(min_fit_score=80)):
            result = await _agent_config()

        assert result["status"] == "success"
        assert result["config"]["min_fit_score"] == 80

    async def test_agent_tool_update_config(self):
        """update_config merges JSON patch into existing config and saves."""
        from naukri_server.tools.agent_tool import _agent_update_config

        base_config = _make_config(mode="approval")

        with patch("naukri_server.agent.load_agent_config", return_value=base_config), \
             patch("naukri_server.agent.save_agent_config") as mock_save:
            result = await _agent_update_config('{"mode": "auto", "min_fit_score": 80}')

        assert result["status"] == "success"
        assert result["config"]["mode"] == "auto"
        assert result["config"]["min_fit_score"] == 80
        mock_save.assert_called_once()

    async def test_agent_tool_update_config_invalid_json(self):
        """update_config with invalid JSON returns VALIDATION_ERROR."""
        from naukri_server.tools.agent_tool import _agent_update_config

        with patch("naukri_server.agent.load_agent_config",
                   return_value=_make_config()):
            result = await _agent_update_config("{bad json")

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    async def test_agent_tool_run_now(self):
        """run_now calls run_agent_cycle and returns its result."""
        from naukri_server.tools.agent_tool import _agent_run_now

        with patch("naukri_server.agent.run_agent_cycle",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "cycle_id": "c_test", "applied": 2}):
            result = await _agent_run_now()

        assert result["status"] == "success"
        assert result["applied"] == 2

    async def test_agent_tool_approve(self):
        """approve action applies pending decisions and returns count."""
        from naukri_server.tools.agent_tool import _agent_approve

        pending = [
            {"job_id": "J1", "decision": "apply", "apply_status": "pending",
             "title": "Dev", "company": "A"},
        ]

        with patch("naukri_server.database.get_agent_run",
                   new_callable=AsyncMock, return_value={"cycle_id": "c1"}), \
             patch("naukri_server.database.list_agent_decisions",
                   new_callable=AsyncMock, return_value=pending), \
             patch("naukri_server.database.update_agent_decision",
                   new_callable=AsyncMock), \
             patch("naukri_server.tools.apply._apply_single",
                   new_callable=AsyncMock,
                   return_value={"status": "applied"}) as mock_apply:
            result = await _agent_approve("c1", approve=True)

        assert result["status"] == "success"
        assert result["applied"] == 1
        mock_apply.assert_awaited_once()

    async def test_agent_tool_reject(self):
        """reject action marks pending decisions as 'rejected'."""
        from naukri_server.tools.agent_tool import _agent_approve

        pending = [
            {"job_id": "J1", "decision": "apply", "apply_status": "pending"},
            {"job_id": "J2", "decision": "apply", "apply_status": "pending"},
        ]

        with patch("naukri_server.database.get_agent_run",
                   new_callable=AsyncMock, return_value={"cycle_id": "c2"}), \
             patch("naukri_server.database.list_agent_decisions",
                   new_callable=AsyncMock, return_value=pending), \
             patch("naukri_server.database.update_agent_decision",
                   new_callable=AsyncMock) as mock_update:
            result = await _agent_approve("c2", approve=False)

        assert result["status"] == "success"
        assert result["rejected"] == 2
        assert mock_update.await_count == 2
        # Verify all were marked "rejected"
        for call in mock_update.call_args_list:
            assert call[0][2] == "rejected"

    async def test_agent_tool_approve_unknown_cycle(self):
        """approve with unknown cycle_id returns NOT_FOUND."""
        from naukri_server.tools.agent_tool import _agent_approve

        with patch("naukri_server.database.get_agent_run",
                   new_callable=AsyncMock, return_value=None):
            result = await _agent_approve("nonexistent")

        assert result["status"] == "error"
        assert result["error_code"] == "NOT_FOUND"

    async def test_agent_tool_approve_no_pending(self):
        """approve with no pending decisions returns VALIDATION_ERROR."""
        from naukri_server.tools.agent_tool import _agent_approve

        with patch("naukri_server.database.get_agent_run",
                   new_callable=AsyncMock, return_value={"cycle_id": "c3"}), \
             patch("naukri_server.database.list_agent_decisions",
                   new_callable=AsyncMock,
                   return_value=[{"job_id": "J1", "decision": "skip", "apply_status": None}]):
            result = await _agent_approve("c3")

        assert result["status"] == "error"
        assert result["error_code"] == "VALIDATION_ERROR"

    async def test_agent_tool_history(self):
        """history action returns list of runs."""
        from naukri_server.tools.agent_tool import _agent_history

        mock_runs = [
            {"cycle_id": "c1", "started_at": "2026-04-05T10:00:00", "status": "completed"},
        ]

        with patch("naukri_server.database.list_agent_runs",
                   new_callable=AsyncMock, return_value=mock_runs):
            result = await _agent_history(limit=5)

        assert result["status"] == "success"
        assert result["total"] == 1
        assert len(result["runs"]) == 1

    async def test_agent_tool_decisions(self):
        """decisions action returns decisions for a cycle."""
        from naukri_server.tools.agent_tool import _agent_decisions

        mock_decs = [
            {"job_id": "J1", "decision": "apply", "apply_status": "applied"},
            {"job_id": "J2", "decision": "skip", "skip_reason": "already_applied"},
        ]

        with patch("naukri_server.database.list_agent_decisions",
                   new_callable=AsyncMock, return_value=mock_decs):
            result = await _agent_decisions("c_test")

        assert result["status"] == "success"
        assert result["cycle_id"] == "c_test"
        assert result["total"] == 2

# =====================================================================
# 5. Scheduler integration
# =====================================================================


class TestSchedulerIntegration:
    """Tests for agent wiring into the scheduler."""

    async def test_task_agent_cycle_calls_run(self):
        """_task_agent_cycle calls run_agent_cycle and returns its result."""
        from naukri_server.scheduler_tasks import _task_agent_cycle

        with patch("naukri_server.agent.run_agent_cycle",
                   new_callable=AsyncMock,
                   return_value={"status": "success", "applied": 3}) as mock_run:
            result = await _task_agent_cycle()

        mock_run.assert_awaited_once()
        assert result["status"] == "success"
        assert result["applied"] == 3

    def test_task_definitions_includes_agent(self):
        """TASK_DEFINITIONS contains an 'agent_cycle' task."""
        from naukri_server.scheduler_tasks import TASK_DEFINITIONS

        names = [t.name for t in TASK_DEFINITIONS]
        assert "agent_cycle" in names

    def test_agent_cycle_task_has_valid_config(self):
        """agent_cycle task has reasonable interval and timeout."""
        from naukri_server.scheduler_tasks import TASK_DEFINITIONS

        agent_task = next(t for t in TASK_DEFINITIONS if t.name == "agent_cycle")
        assert agent_task.interval_seconds == 7200  # 2 hours
        assert agent_task.timeout_seconds == 600
        assert agent_task.description != ""
