"""Home Assistant MCP Server."""

import os
import sys


def _create_server():
    """Create server instance (deferred to avoid import during smoke test)."""
    from ha_mcp.server import HomeAssistantSmartMCPServer  # type: ignore[import-not-found]
    return HomeAssistantSmartMCPServer()


# Lazy server creation - only create when needed
_server = None


def _get_mcp():
    """Get the MCP instance, creating server if needed."""
    global _server
    if _server is None:
        _server = _create_server()
    return _server.mcp


# For module-level access (e.g., fastmcp.json referencing ha_mcp.__main__:mcp)
# This is accessed when the module is imported, so we need deferred creation
class _DeferredMCP:
    """Wrapper that defers MCP creation until actually accessed."""
    def __getattr__(self, name):
        return getattr(_get_mcp(), name)

    def run(self, *args, **kwargs):
        return _get_mcp().run(*args, **kwargs)


mcp = _DeferredMCP()


# CLI entry point (for pyproject.toml) - use FastMCP's built-in runner
def main() -> None:
    """Run server via CLI using FastMCP's stdio transport."""
    # Check for smoke test flag
    if "--smoke-test" in sys.argv:
        from ha_mcp.smoke_test import main as smoke_test_main
        sys.exit(smoke_test_main())

    _get_mcp().run()


# HTTP entry point for web clients
def _get_http_runtime() -> tuple[int, str]:
    """Return runtime configuration shared by HTTP transports."""

    port = int(os.getenv("MCP_PORT", "8086"))
    path = os.getenv("MCP_SECRET_PATH", "/mcp")
    return port, path


def main_web() -> None:
    """Run server over HTTP for web-capable MCP clients.

    Environment:
    - HOMEASSISTANT_URL (required)
    - HOMEASSISTANT_TOKEN (required)
    - MCP_PORT (optional, default: 8086)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    """

    port, path = _get_http_runtime()

    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        path=path,
    )


def main_sse() -> None:
    """Run server using Server-Sent Events transport for MCP clients.

    Environment:
    - HOMEASSISTANT_URL (required)
    - HOMEASSISTANT_TOKEN (required)
    - MCP_PORT (optional, default: 8086)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    """

    port, path = _get_http_runtime()

    mcp.run(
        transport="sse",
        host="0.0.0.0",
        port=port,
        path=path,
    )


if __name__ == "__main__":
    main()
