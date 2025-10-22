"""Home Assistant MCP Server."""

import os
from ha_mcp.server import HomeAssistantSmartMCPServer  # type: ignore[import-not-found]

# Create server instance once
_server = HomeAssistantSmartMCPServer()

# FastMCP entry point (for fastmcp.json)
mcp = _server.mcp


# CLI entry point (for pyproject.toml) - use FastMCP's built-in runner
def main() -> None:
    """Run server via CLI using FastMCP's stdio transport."""
    mcp.run()


# HTTP entry point for web clients
def main_web() -> None:
    """Run server over HTTP for web-capable MCP clients.

    Environment:
    - HOMEASSISTANT_URL (required)
    - HOMEASSISTANT_TOKEN (required)
    - MCP_PORT (optional, default: 8086)
    - MCP_SECRET_PATH (optional, default: "/mcp")
    """

    port = int(os.getenv("MCP_PORT", "8086"))
    path = os.getenv("MCP_SECRET_PATH", "/mcp")

    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        path=path,
    )


if __name__ == "__main__":
    main()
