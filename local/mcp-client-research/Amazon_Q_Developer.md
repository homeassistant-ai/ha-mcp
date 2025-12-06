# Amazon Q Developer (AWS)

## Overview
Amazon Q Developer (and Amazon Q CLI) supports MCP servers.

---

## Source: Multiple repos (awslabs/mcp)

### Configuration
Amazon Q Developer supports MCP servers through AWS integration.

Configuration may be done via:
1. AWS Console settings
2. CLI configuration
3. IDE plugin settings (for JetBrains, VS Code)

---

## Source: awslabs/mcp

### Standard Configuration Format
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
- Part of AWS developer tools ecosystem
- Supports Bedrock integration
- Available as CLI and IDE extensions
- Check AWS documentation for latest configuration
