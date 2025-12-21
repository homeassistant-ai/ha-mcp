---
name: Gemini CLI
company: Google
logo: /logos/google.svg
transports: ['stdio', 'sse', 'streamable-http']
configFormat: cli
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

## Quick Setup with CLI Commands

### stdio (Local)

```bash
gemini mcp add --scope user homeassistant \
  -e HOMEASSISTANT_URL={{HOMEASSISTANT_URL}} \
  -e HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}} \
  uvx -- ha-mcp@latest
```

### HTTP (Network/Remote)

```bash
gemini mcp add --transport http home-assistant {{MCP_SERVER_URL}}
```

### SSE (Network/Remote)

```bash
gemini mcp add --transport sse home-assistant {{MCP_SERVER_URL}}
```

## Management Commands

```bash
# Check MCP status in chat
/mcp

# Reload config after manual edits
/mcp refresh

# List configured servers
gemini mcp list

# Remove a server
gemini mcp remove home-assistant
```

## Notes

- Uses `url` for SSE transport
- Uses `httpUrl` for HTTP streaming transport (not `url`)
- Supports all three transport types natively
- OAuth 2.0 authentication available for remote servers
