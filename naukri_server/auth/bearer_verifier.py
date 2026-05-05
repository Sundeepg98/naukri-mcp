"""Single-secret Bearer TokenVerifier for remote MCP access.

Validates Authorization: Bearer <token> against a constant-time compare with
MCP_SHARED_SECRET. Returns AccessToken with client_id="owner" on match.

Single-user only — one secret, one identity.
"""
import hmac
import os
from typing import Optional

from mcp.server.auth.provider import AccessToken, TokenVerifier


# Minimum entropy threshold for the shared secret. 32 chars of token_urlsafe
# gives ~192 bits which is plenty; we reject anything obviously weak.
_MIN_SECRET_LEN = 32

# Identity returned for any caller presenting the shared secret. Single-user
# server, so all bearer callers map to the same client_id.
_OWNER_CLIENT_ID = "owner"
_OWNER_SCOPES = ["naukri:full"]


class SharedSecretVerifier(TokenVerifier):
    """Constant-time compare of bearer token against MCP_SHARED_SECRET env var.

    Returns AccessToken(client_id="owner") on match, None otherwise.
    """

    def __init__(self, secret: str):
        if not secret or len(secret) < _MIN_SECRET_LEN:
            raise ValueError(
                f"MCP_SHARED_SECRET must be at least {_MIN_SECRET_LEN} chars "
                f"(got {len(secret) if secret else 0})"
            )
        # Encode once; compare_digest works on bytes
        self._secret_bytes = secret.encode("utf-8")

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Verify token via constant-time compare. Returns AccessToken or None."""
        if not token:
            return None
        try:
            token_bytes = token.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            return None
        if not hmac.compare_digest(self._secret_bytes, token_bytes):
            return None
        return AccessToken(
            token=token,
            client_id=_OWNER_CLIENT_ID,
            scopes=list(_OWNER_SCOPES),
            expires_at=None,
        )

    def matches(self, token: str) -> bool:
        """Sync helper used by OAuth provider to accept the same shared secret."""
        if not token:
            return False
        try:
            token_bytes = token.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            return False
        return hmac.compare_digest(self._secret_bytes, token_bytes)


def build_verifier_from_env() -> Optional[SharedSecretVerifier]:
    """Construct a SharedSecretVerifier from MCP_SHARED_SECRET env var.

    Returns None if the env var is unset or empty (auth-disabled mode).
    Raises ValueError if the secret is set but too short — fail loud rather
    than silently start an insecure server.
    """
    secret = os.environ.get("MCP_SHARED_SECRET", "").strip()
    if not secret:
        return None
    return SharedSecretVerifier(secret)
