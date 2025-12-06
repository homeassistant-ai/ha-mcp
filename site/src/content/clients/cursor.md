---
name: Cursor
company: Anysphere
logo: /logos/cursor.svg
transports: ['stdio', 'sse', 'streamable-http']
configFormat: json
configLocation: Settings → Cursor Settings → MCP
accuracy: 5
order: 3
---

## Configuration

Cursor uses JSON configuration via Settings → Cursor Settings → MCP. Supports stdio, SSE, and streamable HTTP transports.

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

### SSE Configuration (Network/Remote)

```json
{
  "mcpServers": {
    "home-assistant": {
      "url": "{{MCP_SERVER_URL}}",
      "transport": "sse"
    }
  }
}
```

### Streamable HTTP Configuration (Network/Remote)

```json
{
  "mcpServers": {
    "home-assistant": {
      "url": "{{MCP_SERVER_URL}}",
      "transport": "http"
    }
  }
}
```

## Notes

- Native support for stdio, SSE, and HTTP transports
- Configuration UI available in Settings
- Restart Cursor after config changes
