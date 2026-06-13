"""Tests for naukri_server.kill_switch — trip/reset/persist/fail-closed/window.

Mirrors tests/test_healing_circuit.py: each test redirects the state file to
tmp_path and resets in-memory state so tests don't pollute each other.
Fully offline.
"""

import json

import pytest

from naukri_server import kill_switch


@pytest.fixture
def tmp_state(tmp_path):
    """Point the kill_switch module at a fresh state file in tmp_path."""
    state_path = tmp_path / "kill_switch_state.json"
    kill_switch._set_state_path_for_tests(state_path)
    kill_switch._reset_for_tests()
    yield state_path
    kill_switch._reset_for_tests()


# ---------------------------------------------------------------------------
# Default state — NOT tripped (fresh install operates normally)
# ---------------------------------------------------------------------------


def test_initial_state_not_tripped(tmp_state):
    assert kill_switch.is_tripped() is False


def test_guard_passes_when_not_tripped(tmp_state):
    # Should not raise.
    kill_switch.guard()


def test_status_shape(tmp_state):
    s = kill_switch.status()
    assert s["tripped"] is False
    assert s["state_file"].endswith("kill_switch_state.json")
    assert "soft_signal_threshold" in s
    assert "window_seconds" in s


# ---------------------------------------------------------------------------
# trip() / reset() + guard
# ---------------------------------------------------------------------------


def test_trip_sets_tripped_and_guard_raises(tmp_state):
    first = kill_switch.trip("captcha on apply", block_kind="captcha")
    assert first is True
    assert kill_switch.is_tripped() is True
    with pytest.raises(kill_switch.KillSwitchTrippedError) as exc:
        kill_switch.guard()
    assert exc.value.block_kind == "captcha"
    assert "captcha on apply" in str(exc.value)


def test_trip_is_idempotent_keeps_first_reason(tmp_state):
    assert kill_switch.trip("first reason", block_kind="soft_block") is True
    assert kill_switch.trip("second reason", block_kind="login_wall") is False
    s = kill_switch.status()
    assert s["reason"] == "first reason"
    assert s["block_kind"] == "soft_block"
    assert s["trip_count"] == 2


def test_reset_clears_tripped(tmp_state):
    kill_switch.trip("blocked")
    assert kill_switch.is_tripped() is True
    kill_switch.reset()
    assert kill_switch.is_tripped() is False
    kill_switch.guard()  # no raise


# ---------------------------------------------------------------------------
# Disk persistence + restart survival
# ---------------------------------------------------------------------------


def test_trip_persists_to_disk(tmp_state):
    kill_switch.trip("revert", block_kind="soft_block")
    raw = json.loads(tmp_state.read_text(encoding="utf-8"))
    assert raw["tripped"] is True
    assert raw["reason"] == "revert"
    assert raw["block_kind"] == "soft_block"


def test_tripped_state_survives_restart(tmp_state):
    """SAFETY-CRITICAL: a trip from a prior process must survive restart."""
    kill_switch.trip("akamai block mid-run", block_kind="captcha")
    # Simulate restart: wipe in-memory, reload from disk.
    kill_switch._reset_for_tests()
    assert kill_switch.is_tripped() is False  # in-memory wiped
    kill_switch._load_initial_state()
    assert kill_switch.is_tripped() is True
    assert "akamai block" in kill_switch.status()["reason"]


def test_reset_persists(tmp_state):
    kill_switch.trip("x")
    kill_switch.reset()
    raw = json.loads(tmp_state.read_text(encoding="utf-8"))
    assert raw["tripped"] is False


# ---------------------------------------------------------------------------
# FAIL-CLOSED behavior
# ---------------------------------------------------------------------------


def test_missing_state_file_is_not_tripped(tmp_state):
    """Fresh install (no file) → not tripped, operates normally."""
    if tmp_state.exists():
        tmp_state.unlink()
    kill_switch._reset_for_tests()
    kill_switch._load_initial_state()
    assert kill_switch.is_tripped() is False


def test_corrupt_state_file_fails_closed_tripped(tmp_state):
    """A garbage state file must FAIL CLOSED → tripped (not silently open)."""
    tmp_state.write_text("{not valid json", encoding="utf-8")
    kill_switch._reset_for_tests()
    kill_switch._load_initial_state()
    assert kill_switch.is_tripped() is True
    assert "unreadable" in kill_switch.status()["reason"].lower()


def test_malformed_but_valid_json_fails_closed(tmp_state):
    """Valid JSON with wrong types → fail closed tripped."""
    tmp_state.write_text(json.dumps({"tripped": "not-a-bool", "trip_count": "x"}),
                         encoding="utf-8")
    kill_switch._reset_for_tests()
    kill_switch._load_initial_state()
    # bool("not-a-bool") is True anyway, but int("x") raises → fail closed path.
    assert kill_switch.is_tripped() is True


# ---------------------------------------------------------------------------
# Soft-signal rolling window → self-trip
# ---------------------------------------------------------------------------


def test_soft_signals_below_threshold_do_not_trip(tmp_state):
    # Threshold default is 3; two signals shouldn't trip.
    assert kill_switch.record_soft_signal("soft_block", now=0.0) is False
    assert kill_switch.record_soft_signal("soft_block", now=1.0) is False
    assert kill_switch.is_tripped() is False


def test_soft_signals_reaching_threshold_trip(tmp_state):
    kill_switch.record_soft_signal("soft_block", now=0.0)
    kill_switch.record_soft_signal("soft_block", now=1.0)
    tripped = kill_switch.record_soft_signal("soft_block", now=2.0)
    assert tripped is True
    assert kill_switch.is_tripped() is True


def test_soft_signals_outside_window_do_not_accumulate(tmp_state):
    win = kill_switch.WINDOW_SECONDS
    # Three signals, but each spaced beyond the window → never 3 in-window.
    kill_switch.record_soft_signal("soft_block", now=0.0)
    kill_switch.record_soft_signal("soft_block", now=win + 1)
    kill_switch.record_soft_signal("soft_block", now=2 * win + 2)
    assert kill_switch.is_tripped() is False


def test_record_soft_signal_noop_when_already_tripped(tmp_state):
    kill_switch.trip("already")
    assert kill_switch.record_soft_signal("soft_block", now=0.0) is False


# ---------------------------------------------------------------------------
# trip_and_halt side effects (agent flip + notification) — mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trip_and_halt_flips_agent_and_notifies(tmp_state, monkeypatch):
    saved = {}

    def fake_load_config():
        return {"mode": "auto", "enabled": True}

    def fake_save_config(cfg):
        saved["cfg"] = cfg

    async def fake_store_notification(notif):
        saved["notif"] = notif
        return 4242

    monkeypatch.setattr("naukri_server.agent.load_agent_config", fake_load_config, raising=False)
    monkeypatch.setattr("naukri_server.agent.save_agent_config", fake_save_config, raising=False)
    monkeypatch.setattr("naukri_server.database.store_notification", fake_store_notification, raising=False)

    result = await kill_switch.trip_and_halt("captcha on apply", block_kind="captcha")

    assert result["first_trip"] is True
    assert result["agent_set_dry_run"] is True
    assert saved["cfg"]["mode"] == "dry_run"
    assert result["notification_id"] == 4242
    assert saved["notif"]["priority"] == "high"
    assert saved["notif"]["event_type"] == "KillSwitchTripped"
    assert kill_switch.is_tripped() is True


@pytest.mark.asyncio
async def test_trip_and_halt_second_call_skips_side_effects(tmp_state, monkeypatch):
    calls = {"save": 0, "notif": 0}

    monkeypatch.setattr("naukri_server.agent.load_agent_config",
                        lambda: {"mode": "auto"}, raising=False)
    monkeypatch.setattr("naukri_server.agent.save_agent_config",
                        lambda cfg: calls.__setitem__("save", calls["save"] + 1), raising=False)

    async def fake_store(notif):
        calls["notif"] += 1
        return 1
    monkeypatch.setattr("naukri_server.database.store_notification", fake_store, raising=False)

    await kill_switch.trip_and_halt("first", block_kind="soft_block")
    await kill_switch.trip_and_halt("second", block_kind="soft_block")

    # Side effects only on the FIRST trip.
    assert calls["save"] == 1
    assert calls["notif"] == 1


@pytest.mark.asyncio
async def test_trip_and_halt_survives_side_effect_failure(tmp_state, monkeypatch):
    """If flipping the agent / notifying fails, the trip itself still sticks."""
    def boom():
        raise RuntimeError("config unavailable")
    monkeypatch.setattr("naukri_server.agent.load_agent_config", boom, raising=False)

    async def boom_store(notif):
        raise RuntimeError("db down")
    monkeypatch.setattr("naukri_server.database.store_notification", boom_store, raising=False)

    result = await kill_switch.trip_and_halt("blocked", block_kind="captcha")
    assert result["tripped"] is True
    assert kill_switch.is_tripped() is True
    assert result["agent_set_dry_run"] is False
    assert result["notification_id"] is None


# ---------------------------------------------------------------------------
# Concurrency smoke (RLock)
# ---------------------------------------------------------------------------


def test_concurrent_trip_reset_settle(tmp_state):
    import threading

    def worker(do_trip):
        for _ in range(50):
            if do_trip:
                kill_switch.trip("loop")
            else:
                kill_switch.reset()

    t1 = threading.Thread(target=worker, args=(True,))
    t2 = threading.Thread(target=worker, args=(False,))
    t1.start(); t2.start(); t1.join(); t2.join()

    flag = kill_switch.is_tripped()
    assert isinstance(flag, bool)
    assert kill_switch.status()["tripped"] == flag
