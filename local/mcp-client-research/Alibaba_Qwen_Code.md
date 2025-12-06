# Qwen Code (Alibaba)

## Overview
Qwen Code (Qwen Coder) is Alibaba's AI coding assistant that supports MCP servers.

## Configuration File Location
- `.qwen/settings.json`

---

## Source: MicrosoftDocs/mcp

### HTTP Configuration
Add to `.qwen/settings.json`:
```json
{
  "server-name": {
    "httpUrl": "https://your-server.com/api/mcp"
  }
}
```

---

## Source: Multiple repos

### Standard Configuration
```json
{
  "mcpServers": {
    "server-name": {
      "httpUrl": "https://your-server.com/api/mcp"
    }
  }
}
```

---

## Notes
- Uses `httpUrl` key (similar to Gemini CLI)
- Configuration file at `.qwen/settings.json`
- Supports HTTP transport
- Part of Alibaba's Qwen AI ecosystem
