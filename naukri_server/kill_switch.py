"""Global apply / auto-hunt KILL-SWITCH — a fail-closed safety breaker.

This is the lockout-hardening backstop. When Naukri's Akamai Bot Manager starts
soft-blocking us (a block-state classification, or N soft signals inside a
window), continuing to fire apply / auto-hunt traffic deepens the block and
risks an account lockout — the one thing the residential-IP + persistent-profile
moat cannot recover from automatically. So we HARD-STOP all write/automation
traffic the moment a block is detected and require an explicit, operator-gated
resume.

Design (mirrors healing/circuit.py — same disk-persist + thread-lock + test
helpers shape so it boots warm and is inspectable):

  * In-memory ``_TRIPPED`` flag, persisted to ``<DATA_DIR>/kill_switch_state.json``
    via an atomic temp-file rename, read once at import. A trip from a prior
    process therefore SURVIVES a restart — the operator must see and clear it.
  * ``trip(reason, *, block_kind)`` — flip to tripped, persist, record metadata.
    Idempotent: a second trip keeps the FIRST reason/time (the root cause) and
    just bumps a counter.
  * ``reset()`` — operator-gated clear (exposed via the naukri_kill_switch MCP
    tool, mirroring the resume_healing UX).
  * ``record_soft_signal(kind)`` — feed a single soft signal; if the count within
    ``WINDOW_SECONDS`` reaches ``SOFT_SIGNAL_THRESHOLD`` the switch trips itself.
  * ``guard()`` — call at the top of every apply / auto-hunt entrypoint; raises
    ``KillSwitchTrippedError`` (fail-closed) when tripped so the caller halts.

FAIL-CLOSED: if the persisted state is unreadable/corrupt we treat the switch as
TRIPPED (safer to wrongly halt than to wrongly fire traffic into a block). A
fresh install with no state file is NOT tripped (so normal operation starts).

The actual side effects of a trip (flip the autonomous agent to dry_run, store a
high-priority notification) live in ``trip_and_halt()`` which is async and
imports the agent/db lazily to keep this module dependency-light and importable
from anywhere (including the hot api path).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from naukri_server.config import DATA_DIR

logger = logging.getLogger(__name__)

_STATE_FILENAME = "kill_switch_state.json"
_STATE_PATH: Path = DATA_DIR / _STATE_FILENAME

# Soft-signal trip policy: N non-fatal block signals within the window trips the
# switch even if no single response was decisively a hard block. Conservative —
# a couple of soft signals in a short window is already a strong lockout warning.
SOFT_SIGNAL_THRESHOLD = int(os.environ.get("NAUKRI_KILL_SOFT_THRESHOLD", "3"))
WINDOW_SECONDS = float(os.environ.get("NAUKRI_KILL_SOFT_WINDOW_SECONDS", "300"))

_lock = threading.RLock()


@dataclass
class _State:
    tripped: bool = False
    reason: str = ""
    block_kind: str = ""
    tripped_at: Optional[str] = None       # ISO8601 of FIRST trip
    trip_count: int = 0
    # Rolling timestamps of recent soft signals (monotonic seconds), in-memory
    # only (not persisted — a window is inherently process-local/short-lived).
    _soft_signals: list[float] = field(default_factory=list)


_state = _State()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KillSwitchTrippedError(RuntimeError):
    """Raised by guard() when apply / auto-hunt is halted by the kill-switch."""

    def __init__(self, reason: str, block_kind: str = ""):
        self.reason = reason
        self.block_kind = block_kind
        msg = f"Apply/auto-hunt halted by kill-switch: {reason}"
        if block_kind:
            msg += f" (block_kind={block_kind})"
        msg += ". Operator must resume via naukri_kill_switch(action='resume')."
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Persistence (mirrors healing/circuit.py)
# ---------------------------------------------------------------------------


def _read_state_file(path: Path | None = None) -> dict[str, Any] | None:
    p = path or _STATE_PATH
    try:
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # FAIL-CLOSED on a corrupt/unreadable file: signal "unknown" so the
        # loader trips. (None means "no file" → fresh, not tripped.)
        logger.warning("kill_switch_state.json unreadable — failing closed: %s", exc)
        return {"__unreadable__": True}


def _write_state_file(path: Path | None = None) -> None:
    p = path or _STATE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".killswitch.tmp")
        tmp.write_text(
            json.dumps({
                "tripped": _state.tripped,
                "reason": _state.reason,
                "block_kind": _state.block_kind,
                "tripped_at": _state.tripped_at,
                "trip_count": _state.trip_count,
            }, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(p))
    except OSError as exc:
        logger.warning("Failed to persist kill_switch_state.json: %s", exc)


def _load_initial_state() -> None:
    """Read disk state at import. Missing file → not tripped. Corrupt → tripped."""
    global _state
    data = _read_state_file()
    if data is None:
        # No state file: pristine install — not tripped. Don't create the file
        # until something actually trips (keeps a clean checkout free of state).
        _state = _State()
        return
    if data.get("__unreadable__"):
        # FAIL-CLOSED: unreadable state → assume tripped.
        _state = _State(
            tripped=True,
            reason="state file unreadable at startup — failing closed",
            block_kind="",
            tripped_at=_now_iso(),
            trip_count=1,
        )
        return
    try:
        _state = _State(
            tripped=bool(data.get("tripped", False)),
            reason=str(data.get("reason", "")),
            block_kind=str(data.get("block_kind", "")),
            tripped_at=data.get("tripped_at"),
            trip_count=int(data.get("trip_count", 0)),
        )
    except (TypeError, ValueError, AttributeError):
        _state = _State(tripped=True, reason="state file malformed — failing closed",
                        tripped_at=_now_iso(), trip_count=1)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


_load_initial_state()


# ---------------------------------------------------------------------------
# Core sync API
# ---------------------------------------------------------------------------


def is_tripped() -> bool:
    with _lock:
        return _state.tripped


def trip(reason: str, *, block_kind: str = "") -> bool:
    """Trip the kill-switch (idempotent). Returns True if THIS call tripped it.

    A re-trip keeps the original reason/time (the root cause) and just bumps the
    counter, so the operator sees what first broke.
    """
    with _lock:
        first = not _state.tripped
        _state.trip_count += 1
        if first:
            _state.tripped = True
            _state.reason = reason or "tripped by caller"
            _state.block_kind = block_kind
            _state.tripped_at = _now_iso()
            logger.error("KILL-SWITCH TRIPPED: %s (block_kind=%s)", reason, block_kind)
        else:
            logger.warning("Kill-switch already tripped (re-trip #%d): %s",
                           _state.trip_count, reason)
        _write_state_file()
        return first


def reset() -> None:
    """Operator-gated clear. Wipes tripped state + the soft-signal window."""
    with _lock:
        _state.tripped = False
        _state.reason = ""
        _state.block_kind = ""
        _state.tripped_at = None
        _state.trip_count = 0
        _state._soft_signals.clear()
        _write_state_file()
    logger.info("Kill-switch RESET by operator — apply/auto-hunt re-enabled")


def record_soft_signal(kind: str = "soft_block", *, now: Optional[float] = None) -> bool:
    """Record one soft block signal. Trips the switch if the window threshold is hit.

    Returns True if this signal caused a trip. Uses a monotonic rolling window so
    transient single blocks don't trip, but a cluster does.
    """
    t = now if now is not None else time.monotonic()
    with _lock:
        if _state.tripped:
            return False
        # Drop signals that have aged out of the window.
        _state._soft_signals = [s for s in _state._soft_signals if t - s < WINDOW_SECONDS]
        _state._soft_signals.append(t)
        count = len(_state._soft_signals)
        logger.info("Kill-switch soft signal #%d/%d (kind=%s) in %.0fs window",
                    count, SOFT_SIGNAL_THRESHOLD, kind, WINDOW_SECONDS)
        if count >= SOFT_SIGNAL_THRESHOLD:
            trip(f"{count} soft block signals within {int(WINDOW_SECONDS)}s window",
                 block_kind=kind)
            return True
        return False


def guard() -> None:
    """Raise KillSwitchTrippedError if tripped. Call at apply/auto-hunt entry.

    Fail-closed: if tripped (including the corrupt-state-at-startup case) this
    raises so the caller halts.
    """
    with _lock:
        if _state.tripped:
            raise KillSwitchTrippedError(_state.reason, _state.block_kind)


def status() -> dict[str, Any]:
    with _lock:
        return {
            "tripped": _state.tripped,
            "reason": _state.reason,
            "block_kind": _state.block_kind,
            "tripped_at": _state.tripped_at,
            "trip_count": _state.trip_count,
            "soft_signals_in_window": len(_state._soft_signals),
            "soft_signal_threshold": SOFT_SIGNAL_THRESHOLD,
            "window_seconds": WINDOW_SECONDS,
            "state_file": str(_STATE_PATH),
        }


# ---------------------------------------------------------------------------
# Async trip-with-side-effects: flip agent → dry_run + high-priority notification
# ---------------------------------------------------------------------------


async def trip_and_halt(reason: str, *, block_kind: str = "") -> dict[str, Any]:
    """Trip the switch AND perform the halt side effects.

    Side effects (best-effort, each guarded so one failure can't block the trip):
      1. Persist the tripped flag (via trip()).
      2. Flip the autonomous agent config to mode="dry_run" so a running/next
         cycle cannot apply.
      3. Store a high-priority notification so the operator is alerted.

    Returns a summary dict. Safe to call when already tripped (the notification /
    agent flip are skipped on a re-trip to avoid spamming).
    """
    first = trip(reason, block_kind=block_kind)
    result: dict[str, Any] = {
        "tripped": True,
        "first_trip": first,
        "reason": reason,
        "block_kind": block_kind,
        "agent_set_dry_run": False,
        "notification_id": None,
    }
    if not first:
        # Already tripped earlier — don't re-flip / re-notify.
        return result

    # 2. Flip the autonomous agent to dry_run (fail-closed: never auto-apply
    #    while blocked). Best-effort.
    try:
        from naukri_server.agent import load_agent_config, save_agent_config
        import asyncio as _asyncio
        config = await _asyncio.to_thread(load_agent_config)
        if config.get("mode") != "dry_run":
            config["mode"] = "dry_run"
            await _asyncio.to_thread(save_agent_config, config)
            result["agent_set_dry_run"] = True
            logger.warning("Kill-switch: autonomous agent flipped to dry_run")
    except Exception as exc:
        logger.error("Kill-switch: failed to flip agent to dry_run: %s", exc)

    # 3. High-priority operator notification.
    try:
        from naukri_server.database import store_notification
        nid = await store_notification({
            "event_type": "KillSwitchTripped",
            "title": "KILL-SWITCH TRIPPED — apply/auto-hunt halted",
            "body": (
                f"Apply and auto-hunt have been HALTED to protect the account.\n\n"
                f"Reason: {reason}\n"
                f"Block kind: {block_kind or 'n/a'}\n\n"
                f"The autonomous agent has been set to dry_run. No applications "
                f"will be submitted until you resume.\n\n"
                f"Investigate the block (likely Akamai bot-check / soft lockout), "
                f"then resume with: naukri_kill_switch(action='resume')."
            ),
            "priority": "high",
            "created_at": _now_iso(),
            "metadata": json.dumps({
                "reason": reason,
                "block_kind": block_kind,
            }),
        })
        result["notification_id"] = int(nid) if nid is not None else None
    except Exception as exc:
        logger.error("Kill-switch: failed to store trip notification: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Test helpers (mirror healing/circuit.py)
# ---------------------------------------------------------------------------


def _set_state_path_for_tests(new_path: Path) -> None:
    global _STATE_PATH
    _STATE_PATH = new_path


def _reset_for_tests() -> None:
    """Reset in-memory state to pristine (NOT tripped). Tests-only."""
    global _state
    with _lock:
        _state = _State()
