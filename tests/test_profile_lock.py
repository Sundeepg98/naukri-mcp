"""Unit tests for naukri_server.profile_lock — cross-process profile guard.

Every test is PURE: no real browser, no network, no touching the real Chrome
profile. The lock file is redirected to a pytest ``tmp_path`` via
``patch.object(profile_lock, "_LOCK_FILE", ...)`` — the same idiom the existing
browser tests use for ``_AUTH_STATE_FILE``.

These tests protect the moat asset: they verify the server refuses to launch a
SECOND instance against the same persistent profile (which would corrupt it),
while still reclaiming locks left behind by crashed instances.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_server import profile_lock
from naukri_server.profile_lock import ProfileLockedError


@pytest.fixture
def lock_file(tmp_path):
    """Redirect the module-level lock file to a tmp path for the test."""
    f = tmp_path / "chrome-profile.lock"
    with patch.object(profile_lock, "_LOCK_FILE", f):
        yield f


# =====================================================================
# 1. acquire() — happy path + lock file contents
# =====================================================================

class TestAcquireBasic:
    def test_acquire_creates_lock_with_our_pid(self, lock_file):
        profile_lock.acquire()
        assert lock_file.exists()
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())

    def test_acquire_when_no_existing_lock(self, lock_file):
        assert not lock_file.exists()
        profile_lock.acquire()  # must not raise
        assert profile_lock.held_by() == os.getpid()

    def test_acquire_creates_parent_dir(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "chrome-profile.lock"
        with patch.object(profile_lock, "_LOCK_FILE", nested):
            profile_lock.acquire()
            assert nested.exists()


# =====================================================================
# 2. acquire() — LIVE holder raises (the corruption-prevention guarantee)
# =====================================================================

class TestAcquireLiveHolderRaises:
    def test_raises_when_held_by_other_live_pid(self, lock_file):
        # A different PID that we force to look alive.
        other_pid = os.getpid() + 1
        lock_file.write_text(f"{other_pid}\n", encoding="utf-8")
        with patch.object(profile_lock, "_pid_is_alive", return_value=True):
            with pytest.raises(ProfileLockedError) as exc:
                profile_lock.acquire()
        assert exc.value.holder_pid == other_pid
        # Actionable message names the holder PID.
        assert str(other_pid) in str(exc.value)
        assert "already in use" in str(exc.value)

    def test_does_not_overwrite_live_holders_lock(self, lock_file):
        other_pid = os.getpid() + 1
        lock_file.write_text(f"{other_pid}\n", encoding="utf-8")
        with patch.object(profile_lock, "_pid_is_alive", return_value=True):
            with pytest.raises(ProfileLockedError):
                profile_lock.acquire()
        # Holder's lock left intact — we must not clobber a live instance.
        assert lock_file.read_text(encoding="utf-8").strip() == str(other_pid)


# =====================================================================
# 3. acquire() — STALE lock (dead PID) is reclaimed, never deadlocks
# =====================================================================

class TestAcquireStaleReclaim:
    def test_reclaims_stale_dead_pid(self, lock_file):
        dead_pid = os.getpid() + 12345
        lock_file.write_text(f"{dead_pid}\n", encoding="utf-8")
        with patch.object(profile_lock, "_pid_is_alive", return_value=False):
            profile_lock.acquire()  # must NOT raise — reclaim instead
        # Lock now owned by us.
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())

    def test_reclaims_garbage_lock_file(self, lock_file):
        lock_file.write_text("not-a-pid\n", encoding="utf-8")
        # Unparseable PID -> treated as stale; reclaimed without raising.
        profile_lock.acquire()
        assert profile_lock.held_by() == os.getpid()

    def test_reclaims_empty_lock_file(self, lock_file):
        lock_file.write_text("", encoding="utf-8")
        profile_lock.acquire()
        assert profile_lock.held_by() == os.getpid()


# =====================================================================
# 4. acquire() — re-entrant for our own PID (watchdog stop->start cycle)
# =====================================================================

class TestAcquireReentrant:
    def test_reacquire_same_pid_succeeds(self, lock_file):
        profile_lock.acquire()
        # Same process acquiring again must not raise (watchdog restart).
        profile_lock.acquire()
        assert profile_lock.held_by() == os.getpid()

    def test_reentrant_does_not_consult_liveness(self, lock_file):
        lock_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        # Our own PID short-circuits before any liveness probe.
        with patch.object(profile_lock, "_pid_is_alive", side_effect=AssertionError(
                "should not probe liveness for our own PID")):
            profile_lock.acquire()


# =====================================================================
# 5. release() — only removes OUR lock; idempotent + safe
# =====================================================================

class TestRelease:
    def test_release_removes_our_lock(self, lock_file):
        profile_lock.acquire()
        assert lock_file.exists()
        profile_lock.release()
        assert not lock_file.exists()

    def test_release_is_idempotent(self, lock_file):
        profile_lock.acquire()
        profile_lock.release()
        profile_lock.release()  # second call must not raise
        assert not lock_file.exists()

    def test_release_when_no_lock_file(self, lock_file):
        assert not lock_file.exists()
        profile_lock.release()  # nothing to do, must not raise

    def test_release_does_not_remove_other_pids_lock(self, lock_file):
        other_pid = os.getpid() + 1
        lock_file.write_text(f"{other_pid}\n", encoding="utf-8")
        profile_lock.release()  # not ours — leave it
        assert lock_file.exists()
        assert lock_file.read_text(encoding="utf-8").strip() == str(other_pid)

    def test_release_swallows_unlink_errors(self, lock_file):
        profile_lock.acquire()
        with patch.object(type(lock_file), "unlink", side_effect=OSError("boom")):
            profile_lock.release()  # must not raise during shutdown

    def test_acquire_release_roundtrip_leaves_clean_state(self, lock_file):
        profile_lock.acquire()
        profile_lock.release()
        assert profile_lock.held_by() is None


# =====================================================================
# 6. _pid_is_alive — real-process liveness (no mocks)
# =====================================================================

class TestPidIsAlive:
    def test_current_process_is_alive(self):
        assert profile_lock._pid_is_alive(os.getpid()) is True

    def test_zero_and_negative_are_not_alive(self):
        assert profile_lock._pid_is_alive(0) is False
        assert profile_lock._pid_is_alive(-1) is False

    def test_almost_certainly_dead_pid(self):
        # A very high PID that is overwhelmingly unlikely to exist. We don't
        # assert hard on platform quirks — just that it returns a bool and does
        # not raise. (The reclaim path is exercised deterministically above via
        # patched _pid_is_alive.)
        result = profile_lock._pid_is_alive(2_000_000_000)
        assert isinstance(result, bool)


# =====================================================================
# 7. _read_lock_pid / held_by — parsing edge cases
# =====================================================================

class TestReadLockPid:
    def test_reads_pid_first_line_only(self, lock_file):
        lock_file.write_text("4321\nextra metadata line\n", encoding="utf-8")
        assert profile_lock._read_lock_pid() == 4321

    def test_missing_file_returns_none(self, lock_file):
        assert profile_lock._read_lock_pid() is None

    def test_held_by_returns_none_when_unlocked(self, lock_file):
        assert profile_lock.held_by() is None


# =====================================================================
# 8. Integration with NaukriBrowser.start()/stop() — lock acquire/release
#    Mock Playwright entirely; assert the lock is taken before launch and
#    released on clean stop and on startup failure.
# =====================================================================

def _make_fake_persistent_context():
    """A MagicMock browser context with one live page, usable by start()."""
    ctx = AsyncMock()
    page = MagicMock()
    page.url = "about:blank"
    ctx.pages = [page]
    ctx.new_page = AsyncMock(return_value=page)
    ctx.cookies = AsyncMock(return_value=[])  # no token -> skip session validate
    return ctx


class TestBrowserStartStopLockIntegration:
    @pytest.mark.asyncio
    async def test_start_acquires_then_stop_releases(self, lock_file):
        from naukri_server.browser import NaukriBrowser

        fake_ctx = _make_fake_persistent_context()

        fake_pw = AsyncMock()
        fake_pw.chromium.launch_persistent_context = AsyncMock(return_value=fake_ctx)

        @asynccontextmanager
        async def fake_async_playwright_cm():
            yield None

        b = NaukriBrowser()
        with patch("naukri_server.browser.async_playwright") as mock_ap:
            mock_ap.return_value.start = AsyncMock(return_value=fake_pw)
            await b.start()

        # Lock taken during start, owned by us.
        assert b._holds_profile_lock is True
        assert lock_file.exists()
        assert profile_lock.held_by() == os.getpid()

        # close_api_session is imported lazily inside stop(); stub it out.
        with patch("naukri_server.api.close_api_session", new=AsyncMock()):
            await b.stop()

        assert b._holds_profile_lock is False
        assert not lock_file.exists()

    @pytest.mark.asyncio
    async def test_start_refuses_when_live_instance_holds_lock(self, lock_file):
        from naukri_server.browser import NaukriBrowser

        other_pid = os.getpid() + 1
        lock_file.write_text(f"{other_pid}\n", encoding="utf-8")

        b = NaukriBrowser()
        launch = AsyncMock()
        with patch.object(profile_lock, "_pid_is_alive", return_value=True), \
             patch("naukri_server.browser.async_playwright") as mock_ap:
            mock_ap.return_value.start = AsyncMock()
            mock_ap.return_value.start.return_value.chromium.launch_persistent_context = launch
            with pytest.raises(ProfileLockedError):
                await b.start()

        # Crucially: we never launched the persistent context against the profile.
        launch.assert_not_called()
        assert b._holds_profile_lock is False
        # The live holder's lock is untouched.
        assert lock_file.read_text(encoding="utf-8").strip() == str(other_pid)

    @pytest.mark.asyncio
    async def test_start_failure_releases_lock(self, lock_file):
        """If launch fails after acquiring, the lock is released (no self-lockout)."""
        from naukri_server.browser import NaukriBrowser

        b = NaukriBrowser()
        with patch("naukri_server.browser.async_playwright") as mock_ap:
            mock_ap.return_value.start = AsyncMock()
            mock_ap.return_value.start.return_value.chromium.launch_persistent_context = \
                AsyncMock(side_effect=RuntimeError("chromium launch boom"))
            # start() catches the error internally (REST-only fallback).
            await b.start()

        assert b.available is False
        # Lock must have been released so a watchdog retry / next start works.
        assert b._holds_profile_lock is False
        assert not lock_file.exists()

    @pytest.mark.asyncio
    async def test_watchdog_style_restart_reuses_lock(self, lock_file):
        """stop() then start() in the same PID re-acquires cleanly (no false lock)."""
        from naukri_server.browser import NaukriBrowser

        fake_ctx = _make_fake_persistent_context()
        fake_pw = AsyncMock()
        fake_pw.chromium.launch_persistent_context = AsyncMock(return_value=fake_ctx)

        b = NaukriBrowser()
        with patch("naukri_server.browser.async_playwright") as mock_ap:
            mock_ap.return_value.start = AsyncMock(return_value=fake_pw)
            await b.start()
            with patch("naukri_server.api.close_api_session", new=AsyncMock()):
                await b.stop()
            # Restart — must not raise ProfileLockedError on our own profile.
            await b.start()

        assert b._holds_profile_lock is True
        assert profile_lock.held_by() == os.getpid()
