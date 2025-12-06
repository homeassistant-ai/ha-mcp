# Cline (VS Code Extension)

## Overview
Cline is a VS Code extension for AI-assisted coding. It supports MCP servers via JSON configuration.

## Configuration File Location
- **macOS**: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
- **Windows**: `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`
- **Linux**: `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`

---

## Source: awesome-remote-mcp-servers

### HTTP Remote Configuration
Access MCP Servers section → Remote Servers → Edit Configuration:
```json
{
  "mcpServers": {
    "server-name": {
      "url": "https://your-server.com/sse",
      "type": "streamableHttp"
    }
  }
}
```

---

## Source: MicrosoftDocs/mcp

### Streamable HTTP Configuration
Use `"type": "streamableHttp"` for remote servers:
```json
{
  "mcpServers": {
    "server-name": {
      "type": "streamableHttp",
      "url": "https://your-server.com/api/mcp"
    }
  }
}
```

---

## Source: Multiple repos (general pattern)

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

### Via UI
1. Open Cline settings
2. Navigate to MCP Servers section
3. Click "Remote Servers" or "Edit Configuration"
4. Add server configuration

---

## Notes
- Cline uses `"type": "streamableHttp"` for remote servers
- Also known as "Claude Dev" extension
- Supports both local and remote MCP servers
- Has built-in UI for managing servers
