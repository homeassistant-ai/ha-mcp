# Roo Code

## Overview
Roo Code is a VS Code-based AI coding extension that supports MCP servers.

---

## Source: MicrosoftDocs/mcp

### Marketplace Installation
1. Open Marketplace
2. Search for MCP server name (e.g., "Microsoft Learn")
3. Click Install

---

## Source: Multiple repos

### Configuration Format
```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx",
      "args": ["@package/mcp-server"],
      "env": {
        "API_KEY": "your-key"
      }
    }
  }
}
```

---

## Notes
- VS Code extension (similar to Cline)
- Web-based UI for configuration
- Supports marketplace installation for popular servers
- Standard mcpServers JSON format
