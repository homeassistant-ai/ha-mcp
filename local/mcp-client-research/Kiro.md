# Kiro

## Overview
Kiro is an AI development tool that supports MCP servers.

---

## Source: MicrosoftDocs/mcp

### Legacy/Proxy Configuration
Same configuration as Claude Desktop legacy format:
```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your-server.com/api/mcp"]
    }
  }
}
```

---

## Source: Multiple repos

### Standard STDIO Configuration
```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx",
      "args": ["@package/mcp-server"],
      "env": {
        "API_KEY": "your-key"
      }
    }
  }
}
```

---

## Notes
- Uses mcp-remote proxy for HTTP servers
- Configuration format similar to Claude Desktop
- Standard mcpServers JSON format
- Supports both stdio and proxied HTTP servers
