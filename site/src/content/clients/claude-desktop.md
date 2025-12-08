---
name: Claude Desktop
company: Anthropic
logo: /logos/anthropic.svg
transports: ['stdio']
configFormat: json
configLocation: |
  macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
  Windows: %APPDATA%\Claude\claude_desktop_config.json
accuracy: 5
order: 1
httpNote: Use mcp-proxy for HTTP connections (see Network/Remote setup)
---

## Configuration

Claude Desktop uses a JSON configuration file. After editing, restart Claude Desktop (Claude menu → Quit Claude, then reopen).

### stdio Configuration (Local)

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "{{HOMEASSISTANT_URL}}",
        "HOMEASSISTANT_TOKEN": "{{HOMEASSISTANT_TOKEN}}"
      }
    }
  }
}
```

### HTTP Configuration (Network/Remote)

Claude Desktop only supports stdio natively. Use `mcp-proxy` to connect to HTTP servers:

```bash
# Install mcp-proxy first
uv tool install mcp-proxy
```

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "mcp-proxy",
      "args": ["--transport", "streamablehttp", "{{MCP_SERVER_URL}}"]
    }
  }
}
```

## Notes

- Config file must be valid JSON
- Restart Claude Desktop after any config changes
- Claude Desktop does NOT inherit shell PATH - use full command paths if uvx isn't found
- For HTTP connections, mcp-proxy bridges stdio to streamable HTTP transport
