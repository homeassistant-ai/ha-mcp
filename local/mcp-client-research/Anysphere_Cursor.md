# Cursor (Anysphere)

## Overview
Cursor is an AI-powered code editor. It supports MCP servers via JSON configuration or UI settings.

## Configuration File Location
- **Global**: `~/.cursor/mcp.json`
- **Project**: `.cursor/mcp.json` (in project root)

---

## Source: awesome-remote-mcp-servers

### HTTP/Remote Configuration
Add to `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "server-name": {
      "url": "https://your-server.com/sse"
    }
  }
}
```

---

## Source: MicrosoftDocs/mcp

### One-Click Installation
Click installation badges provided by MCP servers for automatic setup.

### Manual Setup via UI
1. Open Cursor Settings
2. Navigate to MCP section
3. Click "Add new MCP Server"
4. Enter server details

---

## Source: playwright-mcp

### STDIO Configuration
Add to `~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

### Via UI
1. Navigate to `Cursor Settings` → `MCP` → `Add new MCP Server`
2. Name: `playwright`
3. Type: `command`
4. Command: `npx @playwright/mcp@latest`

---

## Source: github-mcp-server

### Docker Configuration
```json
{
  "mcpServers": {
    "github": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "-e",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/github/github-mcp-server"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "your-token"
      }
    }
  }
}
```

---

## Source: Multiple repos (general pattern)

### Standard Configuration Format
```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx|uvx|python|node",
      "args": ["package-name", "additional-args"],
      "env": {
        "API_KEY": "your-api-key"
      }
    }
  }
}
```

### HTTP Remote Server
```json
{
  "mcpServers": {
    "remote-server": {
      "url": "https://your-server.com/sse"
    }
  }
}
```

### Deeplink Installation
Some servers support one-click installation via deeplinks:
`cursor://mcp/install?config=...`

---

## Notes
- Supports both local (stdio) and remote (HTTP) servers
- Project-level config in `.cursor/mcp.json` overrides global config
- Restart Cursor after config changes
- Cursor has built-in UI for managing MCP servers
