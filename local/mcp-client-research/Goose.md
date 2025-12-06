# Goose

## Overview
Goose is an AI coding assistant that supports MCP servers.

---

## Source: playwright-mcp

### Via UI
1. Click install button OR
2. Navigate to `Advanced settings` → `Extensions` → `Add custom extension`
3. Set type to `STDIO`
4. Command: `npx @package/mcp-server`

---

## Source: Multiple repos

### Configuration
```json
{
  "extensions": {
    "server-name": {
      "type": "STDIO",
      "command": "npx @package/mcp-server"
    }
  }
}
```

---

## Notes
- Uses "Extensions" terminology
- Configuration via Advanced settings UI
- Supports STDIO transport type
- Standard command-based configuration
