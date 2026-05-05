#!/usr/bin/env python3
"""Naukri MCP Server — entry point with dual transport support.

Modes:
  python naukri.py          → stdio only (default, used by Claude Code main session)
  python naukri.py --dual   → stdio + HTTP on port 8321 (team agents connect via HTTP)
  python naukri.py --http   → HTTP only on port 8321 (standalone, no stdio)

Remote access (opt-in via env vars):
  MCP_REMOTE=1                  → bind 0.0.0.0 instead of 127.0.0.1
  MCP_PORT=8321                 → override port
  MCP_SHARED_SECRET=<secret>    → enable bearer auth (Claude Code CLI / Desktop)
  MCP_OAUTH_ENABLED=1           → enable OAuth provider (Claude.ai web)
  MCP_PUBLIC_URL=https://...    → public URL of this server (used as OAuth issuer)
  MCP_OAUTH_CLIENT_ID=...       → pre-registered OAuth client_id
  MCP_OAUTH_CLIENT_SECRET=...   → pre-registered OAuth client_secret
  MCP_OAUTH_AUTO_APPROVE=1      → skip consent screen (single-user only)

Hardening: when MCP_REMOTE=1, MCP_SHARED_SECRET or MCP_OAUTH_ENABLED MUST also
be set. The server refuses to start with public bind + no auth.

The --dual flag lets TeamCreate agents connect to the same server instance
via HTTP while the main Claude Code session uses stdio. One browser, one
PagePool, one token — shared across all sessions.
"""
import os
import sys

# --- Hardening assertion (BEFORE importing naukri_server, which reads env) ---
# Refuse to start if MCP_REMOTE=1 but no auth mode is enabled.
if os.environ.get("MCP_REMOTE") == "1":
    _has_bearer = bool(os.environ.get("MCP_SHARED_SECRET", "").strip())
    _has_oauth = os.environ.get("MCP_OAUTH_ENABLED", "").strip() == "1"
    if not (_has_bearer or _has_oauth):
        sys.exit(
            "ERROR: MCP_REMOTE=1 requires authentication.\n"
            "       Set MCP_SHARED_SECRET (>=32 chars) for bearer auth (Claude Code/Desktop)\n"
            "       and/or MCP_OAUTH_ENABLED=1 for OAuth (Claude.ai web)."
        )

from naukri_server import mcp  # noqa: E402

# Bind 0.0.0.0 only when explicitly opted in via MCP_REMOTE; default stays
# 127.0.0.1 so local --dual / --http usage doesn't accidentally expose the port.
HTTP_HOST = "0.0.0.0" if os.environ.get("MCP_REMOTE") == "1" else "127.0.0.1"
HTTP_PORT = int(os.environ.get("MCP_PORT", "8321"))


def run_dual():
    """Run both stdio and HTTP transports in the same process."""
    import anyio
    import uvicorn
    from naukri_server.config import logger

    async def _dual():
        app = mcp.streamable_http_app()

        # Kill any stale process holding the port (common on /mcp reconnect)
        try:
            test_sock = __import__("socket").socket()
            test_sock.setsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_REUSEADDR, 1)
            test_sock.bind((HTTP_HOST, HTTP_PORT))
            test_sock.close()
        except OSError:
            logger.warning("Port %d in use — attempting to continue anyway", HTTP_PORT)

        config = uvicorn.Config(
            app, host=HTTP_HOST, port=HTTP_PORT, log_level="warning",
        )
        server = uvicorn.Server(config)
        logger.info("Dual mode: stdio + HTTP on %s:%d", HTTP_HOST, HTTP_PORT)

        async def _start_http():
            try:
                await server.serve()
            except (SystemExit, OSError) as e:
                logger.warning("HTTP server failed to start: %s (stdio still works)", e)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_start_http)
            await mcp.run_stdio_async()
            tg.cancel_scope.cancel()

    anyio.run(_dual)


def run_http_only():
    """Run HTTP transport only (no stdio).

    Uses uvicorn directly so we honor HTTP_HOST / HTTP_PORT env overrides —
    FastMCP.run() ignores host/port kwargs and only reads them from Settings,
    which we don't control here.
    """
    import anyio
    import uvicorn
    from naukri_server.config import logger

    async def _serve():
        app = mcp.streamable_http_app()
        config = uvicorn.Config(
            app, host=HTTP_HOST, port=HTTP_PORT, log_level="warning",
        )
        server = uvicorn.Server(config)
        logger.info("HTTP mode: %s:%d", HTTP_HOST, HTTP_PORT)
        await server.serve()

    anyio.run(_serve)


if __name__ == "__main__":
    if "--dual" in sys.argv:
        run_dual()
    elif "--http" in sys.argv:
        run_http_only()
    else:
        mcp.run()
