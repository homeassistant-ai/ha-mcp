---
name: Gemini CLI
company: Google
logo: /logos/gemini.svg
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

> **Note:** Use `url` only when the server actually serves SSE. If your client logs show `405 Method Not Allowed` on `GET /mcp`, the server is running in Streamable HTTP mode (POST-only, the default for `ha-mcp-web`) — switch to `httpUrl` (next section).

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

## Setup Examples

Concrete `~/.gemini/settings.json` snippets for the two most common deployments. Replace IPs and tokens with your own.

### Home Assistant built-in MCP integration (no `ha-mcp` container)

If you have the [Home Assistant MCP integration](https://www.home-assistant.io/integrations/mcp_server/) enabled, point Gemini CLI directly at Home Assistant's `/api/mcp` endpoint with a long-lived access token:

```json
{
  "mcpServers": {
    "home-assistant": {
      "httpUrl": "http://192.168.1.10:8123/api/mcp",
      "headers": {
        "Authorization": "Bearer eyJhbGciOiJIUzI1NiI...",
        "Accept": "application/json"
      }
    }
  }
}
```

### Self-hosted `ha-mcp` Docker container

If `ha-mcp` runs in its own container alongside Home Assistant:

```json
{
  "mcpServers": {
    "ha-mcp": {
      "httpUrl": "http://192.168.1.20:8086/mcp"
    }
  }
}
```

### Sample `docker-compose.yml` for `ha-mcp`

Minimal compose for a `ha-mcp-web` deployment paired with an `.env` file. The endpoint produced (`http://<host>:8086/mcp`) works with any HTTP-capable MCP client, not just Gemini CLI.

```yaml
services:
  ha-mcp:
    container_name: ha-mcp
    image: ghcr.io/homeassistant-ai/ha-mcp:latest
    command: ha-mcp-web
    ports:
      - "8086:8086"
    environment:
      - HOMEASSISTANT_URL=${HOMEASSISTANT_URL}
      - HOMEASSISTANT_TOKEN=${HOMEASSISTANT_TOKEN}
    restart: unless-stopped
```

Companion `.env`:

```bash
HOMEASSISTANT_URL=http://192.168.1.10:8123
HOMEASSISTANT_TOKEN=eyJhbGciOiJIUzI1NiI...
```

For production hardening (read-only filesystem, dropped privileges, resource limits), see Docker Compose's [security reference](https://docs.docker.com/compose/compose-file/05-services/#security_opt).

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
