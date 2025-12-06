# Continue.dev

## Overview
Continue is an open-source AI code assistant with robust MCP support.

## Configuration Location
- **Config file**: `~/.continue/config.json` or `.continue/config.json` in project
- **Drop-in configs**: `~/.continue/mcpServers/` directory (auto-loaded)

---

## Source: Official Continue Documentation (docs.continue.dev/customize/deep-dives/mcp)

### Method 1: config.json Format
```json
{
  "experimental": {
    "modelContextProtocolServer": {
      "transport": {
        "type": "stdio",
        "command": "uvx",
        "args": ["ha-mcp@latest"],
        "env": {
          "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
          "HOMEASSISTANT_TOKEN": "your-token"
        }
      }
    }
  }
}
```

### Method 2: Drop-in JSON Files
Copy JSON config files from Claude Desktop/Cursor/Cline directly into:
`~/.continue/mcpServers/`

Continue will automatically pick them up.

### YAML Format (alternative)
```yaml
mcpServers:
  - name: Home Assistant
    command: uvx
    args:
      - "ha-mcp@latest"
    env:
      HOMEASSISTANT_URL: "http://homeassistant.local:8123"
      HOMEASSISTANT_TOKEN: "your-token"
```

---

## Key Features (2025)
- OAuth authentication support for MCP servers
- SSE and Streamable HTTP transports
- Environment variable templating
- Automatic transport fallback

## Notes
- Available for VS Code and JetBrains
- Open-source (github.com/continuedev/continue)
- Supports both JSON and YAML config formats
