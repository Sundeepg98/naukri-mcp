"""Cross-process singleton lock for the persistent Chrome profile.

The Naukri MCP launches Playwright against a FIXED on-disk Chrome profile
(``config.CHROME_PROFILE``). That profile is the anti-detection moat: its
cookies + persistent fingerprint are what keep the session logged in and
human-looking. Two server instances pointed at the SAME profile directory
will corrupt it — Chromium is not safe for concurrent use of one user-data
dir, and a corrupted profile means re-login + a fresh, cold, more-detectable
fingerprint. That is the single highest-cost failure for this server.

The in-process ref-count in ``__init__.py`` only serialises sessions WITHIN
one process. It does nothing across processes. This module adds the missing
cross-process guard: a pidfile written beside the profile dir.

Design (deliberately conservative + stdlib-only — no new dependency):

* ``acquire()`` writes ``<profile>.lock`` containing this process's PID + a
  monotonic-ish start marker. Before writing it inspects any existing lock:
    - live PID owned by ANOTHER process  -> raise ``ProfileLockedError``
      (actionable message naming the holder PID) — do NOT touch the profile.
    - our OWN PID (re-entrant: watchdog stop->start cycle restarts the
      browser in the same process) -> succeed, refresh the lock.
    - dead/unknown PID (STALE lock — previous instance crashed without
      releasing) -> reclaim it; never deadlock on a corpse.
    - unparseable/garbage lock file -> treated as stale and reclaimed.
* ``release()`` removes the lock, but only if WE still own it (never delete
  another live instance's lock). Idempotent + exception-safe so it can sit
  in a ``finally`` / shutdown path.

The lock file lives NEXT TO the profile dir (``<profile>.lock``), not inside
it, so it never pollutes the Chromium user-data-dir and is trivially
patchable to a tmp path in tests. We do NOT rely on Chromium's own
``SingletonLock`` (a dangling symlink that's unreadable/flaky on Windows and
an internal Chromium detail we shouldn't couple to).
"""

import os
from pathlib import Path

from naukri_server.config import CHROME_PROFILE, logger

# Lock file sits beside the profile dir, e.g. ``.../chrome-profile.lock``.
# Module-level so tests can ``patch.object(profile_lock, "_LOCK_FILE", tmp)``.
_LOCK_FILE = Path(str(CHROME_PROFILE) + ".lock")


class ProfileLockedError(RuntimeError):
    """Raised when another LIVE process already holds the profile lock.

    Carries ``holder_pid`` so callers/operators know exactly which process to
    stop. Refusing to launch here is the whole point — corrupting the moat
    profile is far costlier than failing loudly.
    """

    def __init__(self, holder_pid: int, lock_file: Path):
        self.holder_pid = holder_pid
        self.lock_file = lock_file
        super().__init__(
            f"naukri profile already in use by PID {holder_pid} "
            f"(lock: {lock_file}) — only one instance may use the persistent "
            f"Chrome profile. Stop the other instance (or, if PID {holder_pid} "
            f"is dead, delete the lock file) before starting another."
        )


def _pid_is_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` currently exists.

    Cross-platform, side-effect-free. On POSIX uses ``os.kill(pid, 0)``
    (signal 0 only checks existence/permission, never delivers a signal). On
    Windows ``os.kill`` cannot probe with signal 0, so we open the process
    handle via ``ctypes``/OpenProcess and check whether it's still running.
    A PID we can't positively confirm as alive is treated as dead so a stale
    lock can be reclaimed (fail-open toward reclaim, never deadlock).
    """
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259

            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                # Could not open — almost always means the PID doesn't exist.
                return False
            try:
                exit_code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                # If we can open it at all, assume it's alive (conservative:
                # avoid stealing a lock from a process we just can't introspect).
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # ctypes unavailable / unexpected — be conservative and assume the
            # holder is alive rather than risk reclaiming a live profile lock.
            return True

    # POSIX
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — definitely alive.
        return True
    except OSError:
        return False
    return True


def _read_lock_pid() -> "int | None":
    """Return the PID recorded in the lock file, or None if absent/garbage.

    The lock file's first line is the PID. Anything unparseable is treated as
    a stale/corrupt lock (returns None) so it can be safely reclaimed.
    """
    try:
        raw = _LOCK_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("profile_lock: could not read lock file %s: %s", _LOCK_FILE, e)
        return None
    if not raw:
        return None
    first_line = raw.splitlines()[0].strip()
    try:
        return int(first_line)
    except ValueError:
        logger.warning(
            "profile_lock: lock file %s has non-integer PID %r — treating as stale",
            _LOCK_FILE, first_line,
        )
        return None


def _write_lock() -> None:
    """Write our PID into the lock file (atomic replace), creating dirs as needed."""
    try:
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Parent may already exist or be uncreatable; the write below surfaces
        # any real problem.
        pass
    payload = f"{os.getpid()}\n"
    tmp = _LOCK_FILE.with_suffix(_LOCK_FILE.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(_LOCK_FILE)


def acquire() -> None:
    """Acquire the cross-process profile lock before launching the browser.

    Re-entrant for the current process (the watchdog restarts the browser in
    the same PID via stop()->start()). Reclaims stale locks left by crashed
    instances. Raises :class:`ProfileLockedError` if another LIVE process
    holds it — in that case the caller MUST NOT launch the persistent context.
    """
    existing_pid = _read_lock_pid()

    if existing_pid is not None:
        if existing_pid == os.getpid():
            # Same process re-acquiring (e.g. watchdog restart). Refresh + go.
            logger.debug("profile_lock: re-entrant acquire by PID %d", existing_pid)
            _write_lock()
            return
        if _pid_is_alive(existing_pid):
            logger.error(
                "profile_lock: refusing to launch — profile held by live PID %d",
                existing_pid,
            )
            raise ProfileLockedError(existing_pid, _LOCK_FILE)
        # Holder is dead — stale lock from a crashed instance. Reclaim it.
        logger.warning(
            "profile_lock: reclaiming stale lock from dead PID %d (%s)",
            existing_pid, _LOCK_FILE,
        )

    _write_lock()
    logger.info("profile_lock: acquired profile lock (PID %d, %s)", os.getpid(), _LOCK_FILE)


def release() -> None:
    """Release the profile lock if WE own it. Idempotent + exception-safe.

    Safe to call from a ``finally`` / shutdown path even if ``acquire`` was
    never called or already released. Never removes another live instance's
    lock (defends against a late shutdown racing a new owner).
    """
    try:
        existing_pid = _read_lock_pid()
        if existing_pid is None:
            return  # Nothing (or garbage) to release.
        if existing_pid != os.getpid():
            # Not ours — likely already reclaimed by another instance. Leave it.
            logger.debug(
                "profile_lock: not releasing lock owned by PID %d (we are %d)",
                existing_pid, os.getpid(),
            )
            return
        _LOCK_FILE.unlink()
        logger.info("profile_lock: released profile lock (PID %d)", os.getpid())
    except FileNotFoundError:
        pass
    except Exception as e:  # never let cleanup raise during shutdown
        logger.warning("profile_lock: error releasing lock %s: %s", _LOCK_FILE, e)


def held_by() -> "int | None":
    """Return the PID currently recorded in the lock file, or None. (Diagnostics.)"""
    return _read_lock_pid()
