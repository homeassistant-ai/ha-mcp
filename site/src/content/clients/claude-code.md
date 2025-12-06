---
name: Claude Code
company: Anthropic
logo: /logos/anthropic.svg
transports: ['stdio', 'sse', 'streamable-http']
configFormat: cli
accuracy: 5
order: 2
---

## Configuration

Claude Code uses CLI commands to add MCP servers. Supports stdio, SSE, and streamable HTTP transports natively.

### stdio Configuration (Local)

```bash
claude mcp add home-assistant \
  --env HOMEASSISTANT_URL={{HOMEASSISTANT_URL}} \
  --env HOMEASSISTANT_TOKEN={{HOMEASSISTANT_TOKEN}} \
  -- uvx ha-mcp@latest
```

### HTTP Configuration (Network/Remote)

```bash
# Streamable HTTP (recommended)
claude mcp add --transport http home-assistant {{MCP_SERVER_URL}}

# SSE transport
claude mcp add --transport sse home-assistant {{MCP_SERVER_URL}}
```

## Useful Commands

```bash
# List configured servers
claude mcp list

# Remove a server
claude mcp remove home-assistant

# Add with scope (user = global, local = project only)
claude mcp add home-assistant --scope user -- uvx ha-mcp@latest
```

## Notes

- Supports both stdio and HTTP transports natively
- No restart required - changes take effect immediately
- Environment variables are stored securely
