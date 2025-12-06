---
name: Zed
company: Zed Industries
logo: /logos/zed.svg
transports: ['stdio']
configFormat: json
configLocation: ~/.config/zed/settings.json
accuracy: 4
order: 10
httpNote: stdio only - limited HTTP support
---

## Configuration

Zed uses a `context_servers` key in its settings (different from most clients).

### Config File Locations

- **macOS/Linux:** `~/.config/zed/settings.json`
- **Windows:** `%APPDATA%\Zed\settings.json`
- **Project-specific:** `.zed/settings.json`

### stdio Configuration (Local)

Add to `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
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

### HTTP Configuration (Limited)

Zed has limited HTTP support. For remote servers, use `url` and `headers`:

```json
{
  "context_servers": {
    "home-assistant": {
      "url": "{{MCP_SERVER_URL}}",
      "headers": {
        "Authorization": "Bearer {{API_TOKEN}}"
      }
    }
  }
}
```

**Note:** HTTP transport support is limited and may not work with all servers. For full compatibility, use stdio-based MCP servers.

## Notes

- Uses `context_servers` key (not `mcpServers`)
- Uses flat structure: `"command": "..."` and `"args": [...]`
- stdio transport is fully supported
- HTTP transport has limited support (under development)
- Settings file supports JSON with `//` comments
- See [Zed MCP docs](https://zed.dev/docs/ai/mcp) for details
