# Zed (Zed Industries)

## Overview
Zed is a high-performance code editor with native MCP support.

## Configuration File Location
- **Linux**: `~/.config/zed/settings.json` (or `$XDG_CONFIG_HOME/zed/settings.json`)
- **macOS**: `~/.config/zed/settings.json`

---

## Source: Official Zed Documentation (zed.dev/docs/ai/mcp)

### STDIO Configuration
Add to `settings.json`:
```json
{
  "context_servers": {
    "server-name": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your-token"
      }
    }
  }
}
```

### Remote/HTTP Configuration
```json
{
  "context_servers": {
    "remote-server": {
      "url": "https://your-server.com/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

### Via UI
1. Open Agent Panel's Settings view
2. Click "Add Custom Server" button
3. Configure server details

---

## Key Differences
- Uses `context_servers` key (not `mcpServers`)
- Settings file supports JSON with `//` comments
- Has visual indicator for server status (green = active)

## Notes
- Native MCP support in Agent Panel
- `agent.always_allow_tool_actions` setting controls tool permissions
- Extensions can also provide MCP servers
