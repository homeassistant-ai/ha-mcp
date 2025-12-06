---
name: Raycast
company: Raycast
logo: /logos/raycast.svg
transports: ['stdio', 'sse', 'streamable-http']
configFormat: json
configLocation: Manage MCP Servers → Show Config File in Finder
accuracy: 4
order: 12
httpNote: HTTP transport is experimental (requires v1.100.0+)
---

## Configuration

Raycast supports MCP servers via its Extensions system (macOS only).

**Requirements:**
- Raycast v1.98.0+ (MCP support)
- Raycast v1.100.0+ (HTTP transport - experimental)
- Raycast Pro subscription or BYOK (Bring Your Own Key)

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

### HTTP Configuration (Experimental)

```json
{
  "mcpServers": {
    "home-assistant": {
      "url": "{{MCP_SERVER_URL}}",
      "httpHeaders": {
        "Authorization": "Bearer {{API_TOKEN}}"
      }
    }
  }
}
```

**Note:** HTTP support is marked as experimental. Enable in AI Settings.

### Setup Methods

1. **Via UI:** Open Raycast → "Manage MCP Servers" → "Install Server" → fill form
2. **Via JSON paste:** Copy JSON config → open "Install Server" → auto-fills form
3. **Via deeplink:** `raycast://mcp/install?<config-json-percent-encoded>`
4. **Manual edit:** Edit `mcp-config.json` directly

### Find Config File

1. Open Raycast
2. Search for "Manage MCP Servers"
3. Click "Show Config File in Finder"

## Notes

- macOS only
- Uses standard `mcpServers` JSON format (like Claude Desktop)
- @-mention servers in Quick AI and AI Chat: `@home-assistant`
- stdio is fully supported
- HTTP/SSE is experimental (v1.100.0+)
- See [Raycast MCP docs](https://manual.raycast.com/model-context-protocol) for details
