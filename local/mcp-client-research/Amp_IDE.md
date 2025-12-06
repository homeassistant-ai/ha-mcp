# Amp (IDE)

## Overview
Amp is an AI-powered IDE that supports MCP servers.

---

## Source: playwright-mcp

### Via VS Code Extension Settings
Update `settings.json`:
```json
"amp.mcpServers": {
  "server-name": {
    "command": "npx",
    "args": ["@package/mcp-server"]
  }
}
```

### Via CLI
```bash
amp mcp add server-name -- npx @package/mcp-server
```

---

## Source: Multiple repos

### Standard Configuration Format
```json
{
  "amp.mcpServers": {
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
- Uses `amp.mcpServers` key in settings.json
- Supports both CLI and settings-based configuration
- Standard MCP server configuration format
