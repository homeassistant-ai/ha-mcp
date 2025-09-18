"""Home Assistant MCP Server."""

from homeassistant_mcp.server import HomeAssistantSmartMCPServer  # type: ignore[import-not-found]

# Create server instance once
_server = HomeAssistantSmartMCPServer()

# FastMCP entry point (for fastmcp.json)
mcp = _server.mcp


# CLI entry point (for pyproject.toml) - use FastMCP's built-in runner
def main() -> None:
    """Run server via CLI using FastMCP's stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
