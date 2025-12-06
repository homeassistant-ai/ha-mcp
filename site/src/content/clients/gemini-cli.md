---
name: Gemini CLI
company: Google
logo: /logos/google.svg
transports: ['stdio', 'sse', 'streamable-http']
configFormat: json
configLocation: ~/.gemini/settings.json
accuracy: 4
order: 8
---

## Configuration

Gemini CLI supports MCP servers via JSON configuration with full transport support.

### Config File Locations

- **User settings:** `~/.gemini/settings.json`
- **Project settings:** `.gemini/settings.json` (higher precedence)

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

Gemini CLI uses `url` key for SSE transport:

```json
{
  "mcpServers": {
    "home-assistant": {
      "url": "{{MCP_SERVER_URL}}"
    }
  }
}
```

### Streamable HTTP Configuration (Network/Remote)

Gemini CLI uses `httpUrl` key for HTTP streaming transport:

```json
{
  "mcpServers": {
    "home-assistant": {
      "httpUrl": "{{MCP_SERVER_URL}}"
    }
  }
}
```

### With Headers (Authentication)

```json
{
  "mcpServers": {
    "home-assistant": {
      "httpUrl": "{{MCP_SERVER_URL}}",
      "headers": {
        "Authorization": "Bearer {{API_TOKEN}}"
      }
    }
  }
}
```

## Management Commands

```bash
# Check MCP status
gemini
/mcp

# Reload config after manual edits
/mcp refresh

# Add server via CLI
gemini mcp add <config>

# List servers
gemini mcp list
```

## Notes

- Uses `url` for SSE transport
- Uses `httpUrl` for HTTP streaming transport (not `url`)
- Supports all three transport types natively
- OAuth 2.0 authentication available for remote servers
