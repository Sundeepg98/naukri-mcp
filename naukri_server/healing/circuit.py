"""Healing circuit-breaker — global on/off switch with disk persistence.

The healer is DISABLED by default at process start, then either:
  - User explicitly resumes via naukri_agent(action="resume_healing"), OR
  - A previous run set _HEALING_DISABLED=True due to a `git revert` failure
    (we want that state to survive a process restart so the user notices).

State is persisted to <DATA_DIR>/healing_state.json. The file is read once at
module import; subsequent disable/enable calls update both the in-memory flag
and the file (with a defensive try/except around the file write so an I/O
error never breaks the caller).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from naukri_server.config import DATA_DIR

logger = logging.getLogger(__name__)

_STATE_FILENAME = "healing_state.json"
_STATE_PATH: Path = DATA_DIR / _STATE_FILENAME

# Module-level lock so concurrent disable/enable from subscribers don't race.
_lock = threading.Lock()


# In-memory flag — initial state is "disabled" (safe default). Loaded from
# disk on first import so a previous-session disable persists.
_HEALING_DISABLED: bool = True
_DISABLED_REASON: str = "initial state — explicit enable required"


def _read_state_file(path: Path | None = None) -> dict[str, Any] | None:
    """Read the persisted state file. Returns None if missing or unreadable."""
    p = path or _STATE_PATH
    try:
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("healing_state.json unreadable, ignoring: %s", exc)
        return None


def _write_state_file(disabled: bool, reason: str, path: Path | None = None) -> None:
    """Persist current healer state. Failure is logged but never raised."""
    p = path or _STATE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".healer.tmp")
        tmp.write_text(
            json.dumps({"disabled": disabled, "reason": reason}, indent=2),
            encoding="utf-8",
        )
        # Atomic rename — works on Windows and POSIX.
        import os as _os
        _os.replace(str(tmp), str(p))
    except OSError as exc:
        logger.warning("Failed to persist healing_state.json: %s", exc)


def _load_initial_state() -> None:
    """Read disk state at module import; default to disabled-with-reason."""
    global _HEALING_DISABLED, _DISABLED_REASON
    data = _read_state_file()
    if data is None:
        # Persist the initial "disabled" state so the file exists for inspection.
        _write_state_file(True, _DISABLED_REASON)
        return
    try:
        _HEALING_DISABLED = bool(data.get("disabled", True))
        _DISABLED_REASON = str(data.get("reason", "loaded from disk"))
    except (TypeError, AttributeError):
        _HEALING_DISABLED = True
        _DISABLED_REASON = "state file malformed; defaulted to disabled"


# Run the initial load once at import time. The state path is module-level
# (DATA_DIR/healing_state.json), so tests that want isolation MUST monkeypatch
# _STATE_PATH and then call _load_initial_state() (or use the helpers below).
_load_initial_state()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_disabled() -> bool:
    """True if the healer is currently disabled."""
    with _lock:
        return _HEALING_DISABLED


def is_enabled() -> bool:
    """Convenience inverse of is_disabled()."""
    return not is_disabled()


def disable(reason: str) -> None:
    """Disable the healer. Persisted to disk so it survives restarts."""
    global _HEALING_DISABLED, _DISABLED_REASON
    with _lock:
        _HEALING_DISABLED = True
        _DISABLED_REASON = reason or "disabled by caller"
        _write_state_file(_HEALING_DISABLED, _DISABLED_REASON)
    logger.warning("Healer DISABLED: %s", reason)


def enable() -> None:
    """Enable the healer (typically via naukri_agent action='resume_healing')."""
    global _HEALING_DISABLED, _DISABLED_REASON
    with _lock:
        _HEALING_DISABLED = False
        _DISABLED_REASON = ""
        _write_state_file(_HEALING_DISABLED, _DISABLED_REASON)
    logger.info("Healer ENABLED")


def status() -> dict[str, Any]:
    """Snapshot of current circuit state — used by daily brief / agent status."""
    with _lock:
        return {
            "disabled": _HEALING_DISABLED,
            "reason": _DISABLED_REASON,
            "state_file": str(_STATE_PATH),
        }


# ---------------------------------------------------------------------------
# Test helpers — safe to call directly; no external state assumptions
# ---------------------------------------------------------------------------


def _set_state_path_for_tests(new_path: Path) -> None:
    """Monkeypatch entry point used by tests to point at a tmp_path file.

    After calling this, the next disable()/enable() will persist to new_path.
    Tests should also call _load_initial_state() to re-read from new_path.
    """
    global _STATE_PATH
    _STATE_PATH = new_path


def _reset_for_tests() -> None:
    """Reset in-memory state to the safe default. Tests-only."""
    global _HEALING_DISABLED, _DISABLED_REASON
    with _lock:
        _HEALING_DISABLED = True
        _DISABLED_REASON = "reset by test"
