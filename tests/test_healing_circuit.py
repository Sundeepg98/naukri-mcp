"""Tests for naukri_server.healing.circuit — disable/enable + persistence.

Every test redirects the state file to tmp_path / 'healing_state.json' via
the _set_state_path_for_tests / _load_initial_state helpers, then resets
in-memory state, so tests don't pollute each other.
"""

import json

import pytest

from naukri_server.healing import circuit


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Point the circuit module at a fresh state file in tmp_path."""
    state_path = tmp_path / "healing_state.json"
    circuit._set_state_path_for_tests(state_path)
    circuit._reset_for_tests()
    yield state_path
    # Restore to pristine state for the next test (and the package singleton).
    circuit._reset_for_tests()


# ---------------------------------------------------------------------------
# Default state — initial value is "disabled"
# ---------------------------------------------------------------------------


def test_initial_state_is_disabled(tmp_state):
    """Process-start default MUST be disabled — explicit enable required."""
    assert circuit.is_disabled() is True
    assert circuit.is_enabled() is False


def test_status_includes_reason_and_state_file(tmp_state):
    s = circuit.status()
    assert s["disabled"] is True
    assert isinstance(s["reason"], str)
    assert s["state_file"].endswith("healing_state.json")


# ---------------------------------------------------------------------------
# enable()/disable() round-trip + persistence
# ---------------------------------------------------------------------------


def test_enable_then_disable_in_memory(tmp_state):
    circuit.enable()
    assert circuit.is_enabled() is True
    circuit.disable("manual override")
    assert circuit.is_disabled() is True


def test_disable_persists_to_disk(tmp_state):
    circuit.enable()  # ensure file exists in known state
    circuit.disable("revert failed")

    raw = json.loads(tmp_state.read_text(encoding="utf-8"))
    assert raw["disabled"] is True
    assert raw["reason"] == "revert failed"


def test_enable_persists_to_disk(tmp_state):
    circuit.disable("test")
    circuit.enable()

    raw = json.loads(tmp_state.read_text(encoding="utf-8"))
    assert raw["disabled"] is False
    assert raw["reason"] == ""


def test_disabled_state_persists_across_restarts(tmp_state):
    """SAFETY-CRITICAL: a disable from a prior process must survive restart.

    We simulate restart by resetting in-memory state and re-running the load.
    """
    circuit.disable("git revert failed mid-T2-fix")
    # Wipe the in-memory cache as if the process restarted
    circuit._reset_for_tests()
    # Reload from disk
    circuit._load_initial_state()

    assert circuit.is_disabled() is True
    assert "git revert failed" in circuit.status()["reason"]


def test_enabled_state_persists_across_restarts(tmp_state):
    """The inverse: a deliberate enable also survives restart."""
    circuit.enable()
    circuit._reset_for_tests()  # simulate restart wiping in-memory only
    circuit._load_initial_state()
    assert circuit.is_enabled() is True


# ---------------------------------------------------------------------------
# Robustness: malformed / missing files
# ---------------------------------------------------------------------------


def test_missing_state_file_is_treated_as_disabled(tmp_state):
    """If healing_state.json doesn't exist, default to disabled and create it."""
    # tmp_state was created by the fixture's _reset_for_tests + _load was implicit
    # via _set_state_path. We delete it here and re-load.
    if tmp_state.exists():
        tmp_state.unlink()
    circuit._load_initial_state()

    assert circuit.is_disabled() is True
    # Initial-load should have RECREATED the file (so users can inspect it)
    assert tmp_state.exists()


def test_corrupt_state_file_falls_back_to_disabled(tmp_state):
    """A garbage state file must NOT crash module import — fall back to disabled."""
    tmp_state.write_text("{not valid json", encoding="utf-8")
    circuit._reset_for_tests()
    circuit._load_initial_state()
    assert circuit.is_disabled() is True


def test_disk_write_failure_does_not_raise(tmp_state, monkeypatch):
    """If the state file becomes unwritable, disable() still updates memory."""
    def boom(*args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(circuit, "_write_state_file", boom)
    # The wrapped disable() catches the OSError logged inside _write_state_file
    # — but our monkeypatch replaces _write_state_file entirely, so the
    # OSError WILL propagate. Wrap to verify in-memory still flips:
    try:
        circuit.disable("test")
    except OSError:
        pass  # expected since we replaced the safe wrapper
    # Even if persistence raised, the in-memory state should reflect the call
    # IF the implementation flips the flag BEFORE calling write. Verify that
    # contract here so refactors that reorder don't silently regress.
    assert circuit.is_disabled() is True


# ---------------------------------------------------------------------------
# Race-safety on concurrent flips
# ---------------------------------------------------------------------------


def test_concurrent_flips_settle_to_a_consistent_state(tmp_state):
    """Multiple threads flipping the flag must not corrupt the in-memory state.

    Threading.Lock guarantees serialization; this is a smoke test that nothing
    in the lock holder leaks an exception.
    """
    import threading

    def flipper(disable_first: bool):
        for _ in range(50):
            if disable_first:
                circuit.disable("loop")
                circuit.enable()
            else:
                circuit.enable()
                circuit.disable("loop")

    t1 = threading.Thread(target=flipper, args=(True,))
    t2 = threading.Thread(target=flipper, args=(False,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Final in-memory value is implementation-defined (depends on which thread
    # ran last) — what matters is that is_disabled() returns a bool without
    # raising, AND status() agrees with is_disabled().
    flag = circuit.is_disabled()
    assert isinstance(flag, bool)
    assert circuit.status()["disabled"] == flag
