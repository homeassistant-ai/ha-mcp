---
name: Windsurf
company: Codeium
logo: /logos/windsurf.svg
transports: ['stdio', 'sse', 'streamable-http']
configFormat: json
configLocation: ~/.codeium/windsurf/mcp_config.json
accuracy: 4
order: 5
---

## Configuration

Windsurf uses a JSON configuration file similar to Claude Desktop.

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

### HTTP Configuration (Network/Remote)

Windsurf uses `serverUrl` key (not `url`) for HTTP connections:

```json
{
  "mcpServers": {
    "home-assistant": {
      "serverUrl": "{{MCP_SERVER_URL}}"
    }
  }
}
```

## Notes

- Uses `serverUrl` key for HTTP (not `url`)
- Restart Windsurf after config changes
- Native support for stdio and HTTP transports
