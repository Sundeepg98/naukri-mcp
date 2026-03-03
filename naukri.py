#!/usr/bin/env python3
"""Naukri MCP Server — entry point with dual transport support.

Modes:
  python naukri.py          → stdio only (default, used by Claude Code main session)
  python naukri.py --dual   → stdio + HTTP on port 8321 (team agents connect via HTTP)
  python naukri.py --http   → HTTP only on port 8321 (standalone, no stdio)

The --dual flag lets TeamCreate agents connect to the same server instance
via HTTP while the main Claude Code session uses stdio. One browser, one
PagePool, one token — shared across all sessions.
"""
import sys

from naukri_server import mcp

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8321


def run_dual():
    """Run both stdio and HTTP transports in the same process."""
    import anyio
    import uvicorn
    from naukri_server.config import logger

    async def _dual():
        app = mcp.streamable_http_app()
        config = uvicorn.Config(
            app, host=HTTP_HOST, port=HTTP_PORT, log_level="warning",
        )
        server = uvicorn.Server(config)
        logger.info("Dual mode: stdio + HTTP on %s:%d", HTTP_HOST, HTTP_PORT)
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve)
            await mcp.run_stdio_async()
            tg.cancel_scope.cancel()

    anyio.run(_dual)


def run_http_only():
    """Run HTTP transport only (no stdio)."""
    mcp.run(transport="streamable-http", host=HTTP_HOST, port=HTTP_PORT)


if __name__ == "__main__":
    if "--dual" in sys.argv:
        run_dual()
    elif "--http" in sys.argv:
        run_http_only()
    else:
        mcp.run()
