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

> **Note:** Use `url` only when the server actually serves SSE. If your client logs show `405 Method Not Allowed` on `GET /mcp`, the server is running in Streamable HTTP mode — switch to `httpUrl` (next section). For Home Assistant: `ha-mcp-web` defaults to Streamable HTTP; for SSE use the separate `ha-mcp-sse` entry point (default port 8087).

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

Concrete `~/.gemini/settings.json` snippets for the two most common deployments. Replace hosts and tokens with your own. These JSON snippets are equivalent to using `gemini mcp add` (shown above) — pick whichever flow you prefer.

### Home Assistant built-in MCP integration (no `ha-mcp` container)

If you have the [Home Assistant MCP integration](https://www.home-assistant.io/integrations/mcp_server/) enabled, point Gemini CLI directly at Home Assistant's `/api/mcp` endpoint with a long-lived access token:

```json
{
  "mcpServers": {
    "home-assistant": {
      "httpUrl": "http://homeassistant.local:8123/api/mcp",
      "headers": {
        "Authorization": "Bearer <your-long-lived-access-token>"
      }
    }
  }
}
```

### Self-hosted `ha-mcp` Docker container

If `ha-mcp` runs in its own container on the same host as Home Assistant (replace the host with your own):

```json
{
  "mcpServers": {
    "ha-mcp": {
      "httpUrl": "http://homeassistant.local:8086/mcp"
    }
  }
}
```

#### Sample `docker-compose.yml` for `ha-mcp`

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
HOMEASSISTANT_URL=http://homeassistant.local:8123
HOMEASSISTANT_TOKEN=<your-long-lived-access-token>
```

For production hardening (read-only filesystem via `read_only`, dropped Linux capabilities via `cap_drop`, resource limits via `deploy.resources.limits`), see Docker Compose's [services reference](https://docs.docker.com/reference/compose-file/services/).

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
