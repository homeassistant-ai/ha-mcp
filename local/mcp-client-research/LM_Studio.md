# LM Studio

## Overview
LM Studio is a local LLM application that supports MCP servers.

---

## Source: playwright-mcp

### Configuration
Click install button or edit `mcp.json` in Program settings using the standard configuration:

```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx",
      "args": ["@package/mcp-server"]
    }
  }
}
```

---

## Source: Multiple repos

### Standard Configuration Format
```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx|uvx|python",
      "args": ["package-name"],
      "env": {
        "API_KEY": "your-key"
      }
    }
  }
}
```

---

## Notes
- Local LLM application with MCP support
- Configuration via Program settings
- Standard mcpServers JSON format
- Supports local MCP servers
