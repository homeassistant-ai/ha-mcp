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
[mcp_servers.home-assistant]
command = "uvx"
args = ["ha-mcp@latest"]

[mcp_servers.home-assistant.env]
HOMEASSISTANT_URL = "{{HOMEASSISTANT_URL}}"
HOMEASSISTANT_TOKEN = "{{HOMEASSISTANT_TOKEN}}"
```

### Streamable HTTP Configuration (Network/Remote)

```toml
[mcp_servers.home-assistant]
url = "{{MCP_SERVER_URL}}"
```

### With Authentication

```toml
[mcp_servers.home-assistant]
url = "{{MCP_SERVER_URL}}"

[mcp_servers.home-assistant.headers]
Authorization = "Bearer {{API_TOKEN}}"
```

## Quick Setup with CLI Commands

### stdio (Local)

```bash
codex mcp add homeassistant --env HOMEASSISTANT_URL={{HOMEASSISTANT_URL}} --env HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}} -- uvx ha-mcp@latest
```

### HTTP Streaming (Network/Remote)

```bash
codex mcp add home-assistant --url {{MCP_SERVER_URL}}
```

## Management Commands

```bash
# List configured servers
codex mcp list

# Show server details
codex mcp get home-assistant

# Remove a server
codex mcp remove home-assistant
```

## Notes

- Configuration uses TOML format (not JSON)
- Supports stdio and HTTP streaming transports
- OAuth 2.0 authentication available for remote servers
- Config file shared between CLI and IDE extension
