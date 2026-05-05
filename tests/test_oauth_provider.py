"""Tests for naukri_server.auth.oauth_provider — SingleUserOAuthProvider."""

import asyncio
import time

import pytest
from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AuthorizationParams,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull

from naukri_server.auth.bearer_verifier import SharedSecretVerifier
from naukri_server.auth.oauth_provider import (
    SingleUserOAuthProvider,
    build_oauth_provider_from_env,
    _ACCESS_TOKEN_TTL_SECONDS,
    _AUTH_CODE_TTL_SECONDS,
)


_GOOD_SECRET = "x" * 32
_GOOD_CLIENT_ID = "claude-ai-test"
_GOOD_CLIENT_SECRET = "y" * 48


def _make_provider(
    auto_approve: bool = True, with_bearer: bool = False
) -> SingleUserOAuthProvider:
    bv = SharedSecretVerifier(_GOOD_SECRET) if with_bearer else None
    return SingleUserOAuthProvider(
        configured_client_id=_GOOD_CLIENT_ID,
        configured_client_secret=_GOOD_CLIENT_SECRET,
        public_url="https://mcp.example.com",
        auto_approve=auto_approve,
        bearer_verifier=bv,
    )


def _make_authz_params(scopes=None) -> AuthorizationParams:
    return AuthorizationParams(
        state="test-state",
        scopes=scopes or ["naukri:full"],
        code_challenge="challenge-fake-pkce",
        redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


class TestClientRegistration:
    def test_pre_registered_client_loadable(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        assert client is not None
        assert client.client_id == _GOOD_CLIENT_ID
        assert client.client_secret == _GOOD_CLIENT_SECRET

    def test_unknown_client_returns_none(self):
        p = _make_provider()
        assert asyncio.run(p.get_client("unknown")) is None

    def test_dynamic_registration_accepted(self):
        p = _make_provider()
        new_client = OAuthClientInformationFull(
            client_id="dyn-client",
            redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        )
        asyncio.run(p.register_client(new_client))
        loaded = asyncio.run(p.get_client("dyn-client"))
        assert loaded is not None
        assert loaded.client_id == "dyn-client"

    def test_dynamic_registration_assigns_id_when_missing(self):
        p = _make_provider()
        new_client = OAuthClientInformationFull(
            client_id=None,
            redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        )
        asyncio.run(p.register_client(new_client))
        # After registration, client_id is assigned
        assert new_client.client_id is not None
        assert new_client.client_id.startswith("dynamic-")


class TestAuthorizeFlow:
    def test_auto_approve_returns_redirect_with_code(self):
        p = _make_provider(auto_approve=True)
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        assert "claude.ai" in url
        assert "code=" in url
        assert "state=test-state" in url

    def test_consent_mode_returns_consent_url(self):
        p = _make_provider(auto_approve=False)
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        assert "/oauth/consent" in url
        assert "consent_id=" in url

    def test_consent_approve_yields_redirect_with_code(self):
        p = _make_provider(auto_approve=False)
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        consent_url = asyncio.run(p.authorize(client, params))
        consent_id = consent_url.split("consent_id=")[1]
        redirect = asyncio.run(p.complete_consent(consent_id, approved=True))
        assert redirect is not None
        assert "code=" in redirect
        assert "state=test-state" in redirect

    def test_consent_deny_yields_error_redirect(self):
        p = _make_provider(auto_approve=False)
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        consent_url = asyncio.run(p.authorize(client, params))
        consent_id = consent_url.split("consent_id=")[1]
        redirect = asyncio.run(p.complete_consent(consent_id, approved=False))
        assert redirect is not None
        assert "error=access_denied" in redirect
        assert "state=test-state" in redirect

    def test_consent_unknown_id_returns_none(self):
        p = _make_provider(auto_approve=False)
        result = asyncio.run(p.complete_consent("unknown-id", approved=True))
        assert result is None


class TestAuthorizationCodeExchange:
    def test_load_authorization_code_returns_stored(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        code_value = url.split("code=")[1].split("&")[0]
        loaded = asyncio.run(p.load_authorization_code(client, code_value))
        assert loaded is not None
        assert loaded.code == code_value
        assert loaded.client_id == client.client_id

    def test_load_returns_none_for_wrong_client(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        code_value = url.split("code=")[1].split("&")[0]
        # Register a different client and try to load with it
        other = OAuthClientInformationFull(
            client_id="other",
            redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        )
        asyncio.run(p.register_client(other))
        loaded = asyncio.run(p.load_authorization_code(other, code_value))
        assert loaded is None

    def test_exchange_returns_oauth_token(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        code_value = url.split("code=")[1].split("&")[0]
        code = asyncio.run(p.load_authorization_code(client, code_value))
        token = asyncio.run(p.exchange_authorization_code(client, code))
        assert token.access_token is not None
        assert token.refresh_token is not None
        assert token.token_type == "Bearer"
        assert token.expires_in == _ACCESS_TOKEN_TTL_SECONDS
        assert "naukri:full" in (token.scope or "")

    def test_code_is_single_use(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        code_value = url.split("code=")[1].split("&")[0]
        code = asyncio.run(p.load_authorization_code(client, code_value))
        # First exchange succeeds
        asyncio.run(p.exchange_authorization_code(client, code))
        # Second exchange fails
        with pytest.raises(TokenError):
            asyncio.run(p.exchange_authorization_code(client, code))

    def test_expired_code_returns_none_on_load(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        code_value = url.split("code=")[1].split("&")[0]
        # Mutate stored expiry into the past
        stored = p._auth_codes[code_value]
        p._auth_codes[code_value] = stored.model_copy(update={"expires_at": time.time() - 1})
        assert asyncio.run(p.load_authorization_code(client, code_value)) is None


class TestRefreshTokenExchange:
    def _get_token_pair(self, p, client):
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        code_value = url.split("code=")[1].split("&")[0]
        code = asyncio.run(p.load_authorization_code(client, code_value))
        return asyncio.run(p.exchange_authorization_code(client, code))

    def test_refresh_returns_new_pair_and_invalidates_old(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        first = self._get_token_pair(p, client)
        rt = asyncio.run(p.load_refresh_token(client, first.refresh_token))
        assert rt is not None
        second = asyncio.run(p.exchange_refresh_token(client, rt, scopes=[]))
        assert second.access_token != first.access_token
        assert second.refresh_token != first.refresh_token
        # Old refresh token is invalidated
        assert asyncio.run(p.load_refresh_token(client, first.refresh_token)) is None

    def test_load_refresh_returns_none_for_wrong_client(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        first = self._get_token_pair(p, client)
        other = OAuthClientInformationFull(
            client_id="other",
            redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        )
        asyncio.run(p.register_client(other))
        assert asyncio.run(p.load_refresh_token(other, first.refresh_token)) is None


class TestAccessTokenVerification:
    def test_load_access_token_returns_valid_oauth_token(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        code_value = url.split("code=")[1].split("&")[0]
        code = asyncio.run(p.load_authorization_code(client, code_value))
        oauth_tok = asyncio.run(p.exchange_authorization_code(client, code))
        verified = asyncio.run(p.load_access_token(oauth_tok.access_token))
        assert verified is not None
        assert verified.client_id == "owner"
        assert "naukri:full" in verified.scopes

    def test_load_access_token_returns_none_for_unknown(self):
        p = _make_provider()
        assert asyncio.run(p.load_access_token("nonexistent")) is None

    def test_load_access_token_returns_none_for_empty(self):
        p = _make_provider()
        assert asyncio.run(p.load_access_token("")) is None
        assert asyncio.run(p.load_access_token(None)) is None  # type: ignore[arg-type]

    def test_load_access_token_returns_none_when_expired(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        code_value = url.split("code=")[1].split("&")[0]
        code = asyncio.run(p.load_authorization_code(client, code_value))
        oauth_tok = asyncio.run(p.exchange_authorization_code(client, code))
        # Force expiry
        stored = p._access_tokens[oauth_tok.access_token]
        p._access_tokens[oauth_tok.access_token] = stored.model_copy(
            update={"expires_at": int(time.time()) - 1}
        )
        assert asyncio.run(p.load_access_token(oauth_tok.access_token)) is None

    def test_bearer_fallback_accepts_shared_secret(self):
        """OAuth provider with bearer_verifier should accept the shared secret
        as a valid access token — this is what makes a single server work for
        BOTH Claude.ai web (OAuth) and Claude Code CLI (raw bearer)."""
        p = _make_provider(with_bearer=True)
        verified = asyncio.run(p.load_access_token(_GOOD_SECRET))
        assert verified is not None
        assert verified.client_id == "owner"
        assert "naukri:full" in verified.scopes

    def test_bearer_fallback_rejects_wrong_secret(self):
        p = _make_provider(with_bearer=True)
        assert asyncio.run(p.load_access_token("wrong-secret")) is None

    def test_no_bearer_verifier_means_only_oauth_tokens_accepted(self):
        p = _make_provider(with_bearer=False)
        assert asyncio.run(p.load_access_token(_GOOD_SECRET)) is None


class TestRevocation:
    def test_revoke_removes_access_token(self):
        p = _make_provider()
        client = asyncio.run(p.get_client(_GOOD_CLIENT_ID))
        params = _make_authz_params()
        url = asyncio.run(p.authorize(client, params))
        code_value = url.split("code=")[1].split("&")[0]
        code = asyncio.run(p.load_authorization_code(client, code_value))
        oauth_tok = asyncio.run(p.exchange_authorization_code(client, code))
        verified = asyncio.run(p.load_access_token(oauth_tok.access_token))
        assert verified is not None
        # Revoke
        asyncio.run(p.revoke_token(verified))
        assert asyncio.run(p.load_access_token(oauth_tok.access_token)) is None

    def test_revoke_unknown_token_is_noop(self):
        p = _make_provider()
        # Build a valid AccessToken-shaped object that isn't stored
        from mcp.server.auth.provider import AccessToken
        fake = AccessToken(token="never-stored", client_id="owner", scopes=[])
        asyncio.run(p.revoke_token(fake))  # must not raise


class TestBuildFromEnv:
    def test_raises_when_client_id_missing(self, monkeypatch):
        monkeypatch.delenv("MCP_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET", _GOOD_CLIENT_SECRET)
        with pytest.raises(ValueError, match="MCP_OAUTH_CLIENT_ID"):
            build_oauth_provider_from_env()

    def test_raises_when_client_secret_missing(self, monkeypatch):
        monkeypatch.setenv("MCP_OAUTH_CLIENT_ID", "x")
        monkeypatch.delenv("MCP_OAUTH_CLIENT_SECRET", raising=False)
        with pytest.raises(ValueError):
            build_oauth_provider_from_env()

    def test_raises_when_client_secret_too_short(self, monkeypatch):
        monkeypatch.setenv("MCP_OAUTH_CLIENT_ID", "x")
        monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET", "short")
        with pytest.raises(ValueError, match="at least"):
            build_oauth_provider_from_env()

    def test_builds_with_required_vars(self, monkeypatch):
        monkeypatch.setenv("MCP_OAUTH_CLIENT_ID", _GOOD_CLIENT_ID)
        monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET", _GOOD_CLIENT_SECRET)
        monkeypatch.delenv("MCP_OAUTH_AUTO_APPROVE", raising=False)
        monkeypatch.setenv("MCP_PUBLIC_URL", "https://test.example.com")
        p = build_oauth_provider_from_env()
        assert isinstance(p, SingleUserOAuthProvider)
        assert p._configured_client_id == _GOOD_CLIENT_ID
        assert p._public_url == "https://test.example.com"
        # default auto_approve = True
        assert p._auto_approve is True

    def test_auto_approve_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("MCP_OAUTH_CLIENT_ID", _GOOD_CLIENT_ID)
        monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET", _GOOD_CLIENT_SECRET)
        monkeypatch.setenv("MCP_OAUTH_AUTO_APPROVE", "0")
        p = build_oauth_provider_from_env()
        assert p._auto_approve is False

    def test_bearer_verifier_passed_through(self, monkeypatch):
        monkeypatch.setenv("MCP_OAUTH_CLIENT_ID", _GOOD_CLIENT_ID)
        monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET", _GOOD_CLIENT_SECRET)
        bv = SharedSecretVerifier(_GOOD_SECRET)
        p = build_oauth_provider_from_env(bearer_verifier=bv)
        # Sanity: shared secret is accepted as a token
        verified = asyncio.run(p.load_access_token(_GOOD_SECRET))
        assert verified is not None
        assert verified.client_id == "owner"
