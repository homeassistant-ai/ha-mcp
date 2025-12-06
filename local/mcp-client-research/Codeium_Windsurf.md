# Windsurf (Codeium)

## Overview
Windsurf is Codeium's AI-powered IDE. It supports MCP servers via JSON configuration.

## Configuration File Location
- `~/.codeium/windsurf/mcp_config.json`
- Or: `~/.windsurf/mcp.json` (varies by version)

---

## Source: awesome-remote-mcp-servers

### HTTP Remote Configuration
```json
{
  "mcpServers": {
    "server-name": {
      "serverUrl": "https://your-server.com/sse"
    }
  }
}
```

---

## Source: MicrosoftDocs/mcp

### Remote Server Configuration
```json
{
  "mcpServers": {
    "server-name": {
      "serverUrl": "https://your-server.com/api/mcp"
    }
  }
}
```

*Note: Windsurf is recommended for remote connections.*

---

## Source: playwright-mcp

### STDIO Configuration
Follow Windsurf MCP documentation with standard config:
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

---

## Source: Multiple repos (general pattern)

### Standard STDIO Configuration
```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx|uvx|python",
      "args": ["package-name", "args"],
      "env": {
        "API_KEY": "your-key"
      }
    }
  }
}
```

### HTTP Remote Configuration
```json
{
  "mcpServers": {
    "remote-server": {
      "serverUrl": "https://your-server.com/sse"
    }
  }
}
```

---

## Notes
- Uses `serverUrl` for HTTP transport (different from other clients using `url`)
- Supports both local and remote MCP servers
- Config file location may vary by Windsurf version
- Restart Windsurf after config changes
