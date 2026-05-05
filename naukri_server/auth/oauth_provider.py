"""Single-user OAuth 2.0 Authorization Server provider.

Implements the OAuthAuthorizationServerProvider protocol from FastMCP. Designed
for one human owner of this MCP server who wants Claude.ai web (which only
supports OAuth client_id+secret) to connect.

Design choices for single-user simplicity:

- One pre-registered OAuth client is configured via env vars
  (MCP_OAUTH_CLIENT_ID, MCP_OAUTH_CLIENT_SECRET). Claude.ai also performs
  Dynamic Client Registration; we accept that too and store registrations
  in memory (single-process server, so in-memory storage is fine — restart
  forces re-registration, which Claude.ai handles transparently).

- Authorization codes, refresh tokens and access tokens are kept in memory.
  No DB writes — simpler, faster, and a process restart is the equivalent of
  rotating all sessions (acceptable for a personal server).

- Auto-approve (MCP_OAUTH_AUTO_APPROVE=1) skips the consent screen entirely,
  since you ARE the only user. With it off, /oauth/consent renders a tiny
  HTML page with an "Approve" button.

- Issued access tokens are random opaque strings (token_urlsafe). They are
  validated by load_access_token() which is also wired as the TokenVerifier
  for both standard MCP /mcp requests AND the shared-secret bearer flow
  (Claude Code) — so a single FastMCP `auth_server_provider` handles both
  client populations.
"""
import asyncio
import logging
import os
import secrets
import time
from typing import Optional

from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from naukri_server.auth.bearer_verifier import SharedSecretVerifier

logger = logging.getLogger(__name__)

# Token / code lifetimes
_AUTH_CODE_TTL_SECONDS = 600          # 10 minutes
_ACCESS_TOKEN_TTL_SECONDS = 3600      # 1 hour
_REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

# Identity for any caller authenticated via this provider — single-user server
_OWNER_CLIENT_ID = "owner"
_DEFAULT_SCOPES = ["naukri:full"]


class SingleUserOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth 2.0 provider for one configured client + dynamic registration.

    All state is in-memory. Suitable for single-user, single-process deployments.
    """

    def __init__(
        self,
        configured_client_id: str,
        configured_client_secret: str,
        public_url: str,
        auto_approve: bool = True,
        bearer_verifier: Optional[SharedSecretVerifier] = None,
    ):
        self._configured_client_id = configured_client_id
        self._configured_client_secret = configured_client_secret
        self._public_url = public_url.rstrip("/")
        self._auto_approve = auto_approve
        self._bearer_verifier = bearer_verifier

        # In-memory stores (single-process server is the consistency boundary)
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

        # Pending authorization requests (keyed by transient consent_id) used
        # only when auto_approve=False. Maps consent_id → (client, params).
        self._pending_consents: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams]] = {}

        # Lock to keep code/token issuance and exchange thread-safe under
        # FastMCP's async handlers (token endpoint can be hit concurrently).
        self._lock = asyncio.Lock()

        # Pre-register the configured client so /token & /authorize accept it
        # without requiring the client to call /register first.
        self._clients[configured_client_id] = OAuthClientInformationFull(
            client_id=configured_client_id,
            client_secret=configured_client_secret,
            redirect_uris=[
                # Claude.ai posts the redirect in the request, so we accept any
                # https URL by leaving this list permissive but non-empty.
                # Real validation happens at validate_redirect_uri time using
                # the URI provided in the /authorize request.
                AnyUrl("https://claude.ai/api/mcp/auth_callback"),
                AnyUrl("https://claude.com/api/mcp/auth_callback"),
            ],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=" ".join(_DEFAULT_SCOPES),
            token_endpoint_auth_method="client_secret_post",
        )

    # --- Client registration -------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Accept dynamic client registration (Claude.ai uses this).

        We don't persist registrations across restarts, but Claude.ai will
        re-register transparently on first use after a restart.
        """
        if not client_info.client_id:
            client_info.client_id = f"dynamic-{secrets.token_hex(8)}"
        # Dynamic clients use PKCE without a secret; if registration didn't
        # provide one, leave it None (PKCE handles authentication).
        self._clients[client_info.client_id] = client_info
        logger.info("Registered OAuth client: %s", client_info.client_id)

    # --- Authorization endpoint ---------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Return URL to redirect to. Either auto-approves and embeds the
        auth code in the redirect_uri, or sends to consent screen."""
        if self._auto_approve:
            return await self._issue_code_redirect(client, params)
        # Stash the request for the consent screen to pick up
        consent_id = secrets.token_urlsafe(24)
        async with self._lock:
            self._pending_consents[consent_id] = (client, params)
        return f"{self._public_url}/oauth/consent?consent_id={consent_id}"

    async def _issue_code_redirect(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Generate an auth code, store it, and build the redirect URL."""
        code_value = secrets.token_urlsafe(32)
        scopes = params.scopes or list(_DEFAULT_SCOPES)
        # client.client_id is `str | None` in the schema — but FastMCP only calls
        # authorize() after get_client() returned a hit, so it must be set here.
        client_id = client.client_id or ""
        auth_code = AuthorizationCode(
            code=code_value,
            scopes=scopes,
            expires_at=time.time() + _AUTH_CODE_TTL_SECONDS,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        async with self._lock:
            self._auth_codes[code_value] = auth_code
        return construct_redirect_uri(
            str(params.redirect_uri),
            code=code_value,
            state=params.state,
        )

    async def complete_consent(self, consent_id: str, approved: bool) -> Optional[str]:
        """Called by /oauth/consent POST after user clicks Approve/Deny.

        Returns the redirect URL (with auth code on approval, error on deny),
        or None if the consent_id is unknown/expired.
        """
        async with self._lock:
            entry = self._pending_consents.pop(consent_id, None)
        if not entry:
            return None
        client, params = entry
        if not approved:
            return construct_redirect_uri(
                str(params.redirect_uri),
                error="access_denied",
                error_description="User declined authorization",
                state=params.state,
            )
        return await self._issue_code_redirect(client, params)

    async def get_pending_consent(
        self, consent_id: str
    ) -> Optional[tuple[OAuthClientInformationFull, AuthorizationParams]]:
        """Used by the consent UI to render client info to the user."""
        async with self._lock:
            return self._pending_consents.get(consent_id)

    # --- Authorization code exchange ----------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        async with self._lock:
            code = self._auth_codes.get(authorization_code)
        if not code:
            return None
        if code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            async with self._lock:
                self._auth_codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        async with self._lock:
            # Single-use: remove the code immediately
            stored = self._auth_codes.pop(authorization_code.code, None)
        if not stored:
            raise TokenError(
                error="invalid_grant",
                error_description="Authorization code already used or expired",
            )
        access = self._mint_access_token(stored.scopes, stored.resource)
        # Refresh token is keyed by the OAuth client_id (not the resource owner
        # identity) so subsequent /token calls from the same client can use it.
        refresh = self._mint_refresh_token(stored.scopes, client.client_id or "")
        async with self._lock:
            self._access_tokens[access.token] = access
            self._refresh_tokens[refresh.token] = refresh
        return OAuthToken(
            access_token=access.token,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(stored.scopes),
            refresh_token=refresh.token,
        )

    # --- Refresh token exchange ---------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        async with self._lock:
            rt = self._refresh_tokens.get(refresh_token)
        if not rt:
            return None
        if rt.client_id != client.client_id:
            return None
        if rt.expires_at and rt.expires_at < int(time.time()):
            async with self._lock:
                self._refresh_tokens.pop(refresh_token, None)
            return None
        return rt

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Spec recommends rotation: invalidate old refresh, issue new pair
        async with self._lock:
            self._refresh_tokens.pop(refresh_token.token, None)
        new_scopes = scopes or refresh_token.scopes
        access = self._mint_access_token(new_scopes, None)
        new_refresh = self._mint_refresh_token(new_scopes, client.client_id or "")
        async with self._lock:
            self._access_tokens[access.token] = access
            self._refresh_tokens[new_refresh.token] = new_refresh
        return OAuthToken(
            access_token=access.token,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(new_scopes),
            refresh_token=new_refresh.token,
        )

    # --- Token verification (also called via TokenVerifier protocol) --------

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        """Verify a bearer token. Accepts BOTH:
        - OAuth-issued access tokens (from this provider's stores), AND
        - The static MCP_SHARED_SECRET (if a SharedSecretVerifier was attached)

        This dual acceptance is what lets a single auth_server_provider serve
        Claude.ai web (OAuth) and Claude Code CLI (raw bearer) on the same URL.
        """
        if not token:
            return None
        # 1. OAuth-issued access token
        async with self._lock:
            stored = self._access_tokens.get(token)
        if stored:
            if stored.expires_at and stored.expires_at < int(time.time()):
                async with self._lock:
                    self._access_tokens.pop(token, None)
                return None
            return stored
        # 2. Fallback: shared-secret bearer (Claude Code CLI / Desktop)
        if self._bearer_verifier and self._bearer_verifier.matches(token):
            return AccessToken(
                token=token,
                client_id=_OWNER_CLIENT_ID,
                scopes=list(_DEFAULT_SCOPES),
                expires_at=None,
            )
        return None

    # --- Revocation ----------------------------------------------------------

    async def revoke_token(self, token) -> None:
        """Revoke an access or refresh token. No-op if not found."""
        token_str = token.token if hasattr(token, "token") else str(token)
        async with self._lock:
            self._access_tokens.pop(token_str, None)
            self._refresh_tokens.pop(token_str, None)

    # --- Internals -----------------------------------------------------------

    def _mint_access_token(
        self, scopes: list[str], resource: Optional[str]
    ) -> AccessToken:
        return AccessToken(
            token=secrets.token_urlsafe(32),
            client_id=_OWNER_CLIENT_ID,
            scopes=list(scopes),
            expires_at=int(time.time()) + _ACCESS_TOKEN_TTL_SECONDS,
            resource=resource,
        )

    def _mint_refresh_token(
        self, scopes: list[str], oauth_client_id: str
    ) -> RefreshToken:
        return RefreshToken(
            token=secrets.token_urlsafe(32),
            # Refresh token client_id = OAuth client identifier (not user identity)
            # so load_refresh_token can verify the same client is presenting it.
            client_id=oauth_client_id,
            scopes=list(scopes),
            expires_at=int(time.time()) + _REFRESH_TOKEN_TTL_SECONDS,
        )


def build_oauth_provider_from_env(
    bearer_verifier: Optional[SharedSecretVerifier] = None,
) -> SingleUserOAuthProvider:
    """Construct a SingleUserOAuthProvider from env vars.

    Required:  MCP_OAUTH_CLIENT_ID, MCP_OAUTH_CLIENT_SECRET
    Optional:  MCP_PUBLIC_URL (default http://localhost:8321),
               MCP_OAUTH_AUTO_APPROVE (default 1 — bypass consent UI)

    Raises ValueError if required vars are missing.
    """
    client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("MCP_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError(
            "MCP_OAUTH_ENABLED=1 requires MCP_OAUTH_CLIENT_ID and "
            "MCP_OAUTH_CLIENT_SECRET to be set."
        )
    if len(client_secret) < 32:
        raise ValueError(
            f"MCP_OAUTH_CLIENT_SECRET must be at least 32 chars "
            f"(got {len(client_secret)})"
        )
    public_url = os.environ.get("MCP_PUBLIC_URL", "http://localhost:8321").strip()
    auto_approve = os.environ.get("MCP_OAUTH_AUTO_APPROVE", "1").strip() == "1"
    return SingleUserOAuthProvider(
        configured_client_id=client_id,
        configured_client_secret=client_secret,
        public_url=public_url,
        auto_approve=auto_approve,
        bearer_verifier=bearer_verifier,
    )
