---
name: Cline
company: Cline
logo: /logos/cline.svg
transports: ['stdio', 'streamable-http']
configFormat: json
configLocation: VS Code Settings → Cline → MCP Servers
accuracy: 4
order: 6
---

## Configuration

Cline is a VS Code extension with its own MCP configuration.

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

### Streamable HTTP Configuration (Network/Remote)

Cline uses `type: "streamableHttp"` for HTTP connections:

```json
{
  "mcpServers": {
    "home-assistant": {
      "url": "{{MCP_SERVER_URL}}",
      "type": "streamableHttp"
    }
  }
}
```

## Notes

- Uses `type: "streamableHttp"` for HTTP connections (not SSE)
- Configure via Cline extension settings
- Supports stdio and streamable HTTP natively
