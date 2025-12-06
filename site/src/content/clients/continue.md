---
name: Continue
company: Continue.dev
logo: /logos/continue.svg
transports: ['stdio', 'sse', 'streamable-http']
configFormat: yaml
configLocation: .continue/mcpServers/
accuracy: 4
order: 11
---

## Configuration

Continue uses YAML or JSON configuration files with full transport support.

**Important:** MCP can only be used in **Agent Mode** - click the agent selector near the chat input.

### Config File Location

Place config files in `.continue/mcpServers/` directory at your workspace root.

### stdio Configuration (Local)

Create `.continue/mcpServers/home-assistant.yaml`:

```yaml
name: Home Assistant MCP
version: 0.0.1
schema: v1
mcpServers:
  - name: home-assistant
    command: uvx
    args:
      - "ha-mcp@latest"
    env:
      HOMEASSISTANT_URL: "{{HOMEASSISTANT_URL}}"
      HOMEASSISTANT_TOKEN: "{{HOMEASSISTANT_TOKEN}}"
```

Or JSON format (Claude Desktop compatible):

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

```yaml
mcpServers:
  - name: home-assistant
    type: sse
    url: "{{MCP_SERVER_URL}}"
    requestOptions:
      headers:
        Authorization: "Bearer {{API_TOKEN}}"
```

### Streamable HTTP Configuration (Network/Remote)

```yaml
mcpServers:
  - name: home-assistant
    type: streamable-http
    url: "{{MCP_SERVER_URL}}"
    requestOptions:
      headers:
        Authorization: "Bearer {{API_TOKEN}}"
```

## Notes

- **Agent Mode Required** - MCP only works in agent mode
- Supports both YAML and JSON formats
- Claude Desktop JSON configs can be copied directly to `.continue/mcpServers/`
- Supports all three transport types: stdio, SSE, and streamable-http
- Available for VS Code and JetBrains
- See [Continue MCP docs](https://docs.continue.dev/customize/deep-dives/mcp) for details
