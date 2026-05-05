"""Tests for naukri_server.auth.bearer_verifier — SharedSecretVerifier."""

import asyncio

import pytest

from naukri_server.auth.bearer_verifier import (
    SharedSecretVerifier,
    build_verifier_from_env,
    _MIN_SECRET_LEN,
)


_GOOD_SECRET = "x" * _MIN_SECRET_LEN
_OTHER_SECRET = "y" * _MIN_SECRET_LEN


def _run(coro):
    """Helper: run an async coroutine in a fresh event loop."""
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio._get_running_loop() else asyncio.run(coro)


class TestSharedSecretVerifier:
    def test_accepts_correct_token(self):
        v = SharedSecretVerifier(_GOOD_SECRET)
        result = asyncio.run(v.verify_token(_GOOD_SECRET))
        assert result is not None
        assert result.client_id == "owner"
        assert result.scopes == ["naukri:full"]
        assert result.token == _GOOD_SECRET
        # Single-user server: tokens don't expire (until secret rotation)
        assert result.expires_at is None

    def test_rejects_wrong_token(self):
        v = SharedSecretVerifier(_GOOD_SECRET)
        result = asyncio.run(v.verify_token(_OTHER_SECRET))
        assert result is None

    def test_rejects_empty_token(self):
        v = SharedSecretVerifier(_GOOD_SECRET)
        assert asyncio.run(v.verify_token("")) is None

    def test_rejects_none_token(self):
        v = SharedSecretVerifier(_GOOD_SECRET)
        # Defensive: None should not crash, just reject
        assert asyncio.run(v.verify_token(None)) is None  # type: ignore[arg-type]

    def test_rejects_almost_correct_token(self):
        """Constant-time compare: substrings or prefixes must not match."""
        v = SharedSecretVerifier(_GOOD_SECRET)
        # Same prefix, different length
        assert asyncio.run(v.verify_token(_GOOD_SECRET[:-1])) is None
        # Same length, different content
        assert asyncio.run(v.verify_token("z" * _MIN_SECRET_LEN)) is None

    def test_constructor_rejects_short_secret(self):
        with pytest.raises(ValueError, match="at least"):
            SharedSecretVerifier("short")

    def test_constructor_rejects_empty_secret(self):
        with pytest.raises(ValueError):
            SharedSecretVerifier("")

    def test_constructor_rejects_none(self):
        with pytest.raises(ValueError):
            SharedSecretVerifier(None)  # type: ignore[arg-type]

    def test_matches_helper_accepts_correct(self):
        v = SharedSecretVerifier(_GOOD_SECRET)
        assert v.matches(_GOOD_SECRET) is True

    def test_matches_helper_rejects_wrong(self):
        v = SharedSecretVerifier(_GOOD_SECRET)
        assert v.matches(_OTHER_SECRET) is False
        assert v.matches("") is False
        assert v.matches(None) is False  # type: ignore[arg-type]


class TestBuildVerifierFromEnv:
    def test_returns_none_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("MCP_SHARED_SECRET", raising=False)
        assert build_verifier_from_env() is None

    def test_returns_none_when_env_empty(self, monkeypatch):
        monkeypatch.setenv("MCP_SHARED_SECRET", "")
        assert build_verifier_from_env() is None

    def test_returns_none_when_env_whitespace(self, monkeypatch):
        monkeypatch.setenv("MCP_SHARED_SECRET", "   ")
        assert build_verifier_from_env() is None

    def test_returns_verifier_when_env_set(self, monkeypatch):
        monkeypatch.setenv("MCP_SHARED_SECRET", _GOOD_SECRET)
        v = build_verifier_from_env()
        assert v is not None
        assert isinstance(v, SharedSecretVerifier)
        # Round-trip check
        assert v.matches(_GOOD_SECRET) is True

    def test_raises_when_env_too_short(self, monkeypatch):
        monkeypatch.setenv("MCP_SHARED_SECRET", "short")
        with pytest.raises(ValueError):
            build_verifier_from_env()
