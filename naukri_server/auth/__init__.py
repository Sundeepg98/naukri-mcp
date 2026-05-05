"""Naukri MCP server authentication.

Two opt-in modes for remote access:

- Bearer (single shared secret): set MCP_SHARED_SECRET. Works with Claude Code CLI
  and Claude Desktop, which both let you set Authorization: Bearer headers.

- OAuth 2.0 (single-user authorization server): set MCP_OAUTH_ENABLED=1 plus
  MCP_OAUTH_CLIENT_ID / MCP_OAUTH_CLIENT_SECRET / MCP_PUBLIC_URL. Required for
  Claude.ai web, which only supports OAuth client_id+secret (no raw bearer).

Both modes can be active at the same time. When OAuth is enabled the same
provider also accepts the shared-secret token, so a single server handles
both client populations.

When neither env var is set, the server starts WITHOUT auth (current behavior),
so stdio mode and existing local HTTP usage are unaffected.
"""
