---
name: JetBrains IDEs
company: JetBrains
logo: /logos/jetbrains.svg
transports: ['stdio']
configFormat: json
configLocation: Settings → Tools → AI Assistant → MCP Servers
accuracy: 4
order: 9
httpNote: stdio only - use mcp-remote for HTTP servers
---

## Configuration

JetBrains IDEs (IntelliJ, PyCharm, WebStorm, etc.) support MCP via the AI Assistant plugin.

**Requirements:**
- IDE version 2025.1 or later
- AI Assistant plugin 251.26094.80.5+
- Node.js 18+ (for NPM-based servers)

### stdio Configuration (Local)

Configure via **Settings** → **Tools** → **AI Assistant** → **Model Context Protocol (MCP)**:

Click "Add" and use the JSON format:

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

### HTTP Configuration (via mcp-remote)

JetBrains IDEs only support stdio natively. Use `mcp-remote` for HTTP servers:

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "npx",
      "args": ["-y", "mcp-remote@latest", "{{MCP_SERVER_URL}}"]
    }
  }
}
```

### Import from Claude Desktop

The GUI includes an **"Import from Claude"** button that automatically imports all configured MCP servers from Claude Desktop's configuration file.

## Notes

- Requires JetBrains AI Assistant plugin
- stdio transport only (use mcp-remote for HTTP)
- Works with all JetBrains IDEs
- Restart IDE after config changes
- Version 2025.2+ has built-in MCP server capabilities (IDE as server)
