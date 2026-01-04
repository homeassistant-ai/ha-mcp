---
name: Codex
company: OpenAI
logo: /logos/openai.svg
transports: ['stdio', 'streamable-http']
configFormat: cli
configLocation: ~/.codex/config.toml
accuracy: 4
order: 13
---

## Configuration

Codex CLI supports MCP servers via TOML configuration with stdio and HTTP streaming transports.

### Config File Location

- **Configuration:** `~/.codex/config.toml`
- Shared between CLI and IDE extension

### stdio Configuration (Local)

```toml
[[mcp.servers]]
name = "home-assistant"
command = "uvx"
args = ["ha-mcp@latest"]

[mcp.servers.env]
HOMEASSISTANT_URL = "{{HOMEASSISTANT_URL}}"
HOMEASSISTANT_TOKEN = "{{HOMEASSISTANT_TOKEN}}"
```

### Streamable HTTP Configuration (Network/Remote)

```toml
[[mcp.servers]]
name = "home-assistant"
url = "{{MCP_SERVER_URL}}"
transport = "http-stream"
```

### With Authentication

```toml
[[mcp.servers]]
name = "home-assistant"
url = "{{MCP_SERVER_URL}}"
transport = "http-stream"

[mcp.servers.headers]
Authorization = "Bearer {{API_TOKEN}}"
```

## Quick Setup with CLI Commands

### stdio (Local)

```bash
codex mcp add homeassistant \
  --command uvx \
  --args ha-mcp@latest \
  --env HOMEASSISTANT_URL={{HOMEASSISTANT_URL}} \
  --env HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}}
```

### HTTP Streaming (Network/Remote)

```bash
codex mcp add home-assistant \
  --url {{MCP_SERVER_URL}} \
  --transport http-stream
```

## Management Commands

```bash
# List configured servers
codex mcp list

# Remove a server
codex mcp remove home-assistant

# Update server configuration
codex mcp update home-assistant --env HOMEASSISTANT_URL={{NEW_URL}}
```

## Notes

- Configuration uses TOML format (not JSON)
- Supports stdio and HTTP streaming transports
- OAuth 2.0 authentication available for remote servers
- Config file shared between CLI and IDE extension
