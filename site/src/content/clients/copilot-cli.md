---
name: Copilot CLI
company: GitHub
logo: /logos/copilot-cli.svg
transports: ['stdio', 'sse', 'streamable-http']
configFormat: ui
configLocation: ~/.copilot/mcp-config.json
accuracy: 5
order: 8
---

## Configuration

Copilot CLI supports MCP servers via the interactive `/mcp add` command. Supports Local, STDIO, HTTP, and SSE server types.

### Adding an MCP Server

1. Start Copilot CLI: `copilot`
2. Use the `/mcp add` slash command
3. Fill in the form fields using **Tab** to navigate between them
4. Press **Ctrl+S** to save, or **Esc** to cancel

### stdio Configuration (Local)

Use these values in the `/mcp add` form:

| Field | Value |
|-------|-------|
| Server Name | `home-assistant` |
| Server Type | Press **2** for STDIO |
| Command | `uvx ha-mcp@latest` |
| Environment Variables | `HOMEASSISTANT_URL=http://homeassistant.local:8123 HOMEASSISTANT_TOKEN=your_token` |
| Tools | `*` |

### HTTP Configuration (Network/Remote)

Use these values in the `/mcp add` form:

| Field | Value |
|-------|-------|
| Server Name | `home-assistant` |
| Server Type | Press **3** for HTTP |
| URL | `{{MCP_SERVER_URL}}` |
| Tools | `*` |

### SSE Configuration (Network/Remote)

| Field | Value |
|-------|-------|
| Server Name | `home-assistant` |
| Server Type | Press **4** for SSE |
| URL | `{{MCP_SERVER_URL}}` |
| Tools | `*` |

## Useful Commands

```bash
# Check MCP server status in an interactive session
/mcp

# Start Copilot CLI
copilot
```

## Notes

- Server Type options: 1 = Local, 2 = STDIO, 3 = HTTP, 4 = SSE
- Environment variables format: `KEY=VALUE` pairs separated by spaces
- Tools field: use `*` for all tools, or a comma-separated list
- MCP server config stored in `~/.copilot/mcp-config.json`
- Set `COPILOT_HOME` env var to change config directory
- GitHub MCP server is included by default
