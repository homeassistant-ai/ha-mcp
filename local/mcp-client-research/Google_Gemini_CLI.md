# Gemini CLI (Google)

## Overview
Google's Gemini CLI supports MCP servers via JSON configuration.

## Configuration File Location
- `~/.gemini/settings.json`

---

## Source: awesome-remote-mcp-servers

### HTTP Remote Configuration
Add to `~/.gemini/settings.json`:
```json
{
  "mcpServers": {
    "server-name": {
      "httpUrl": "https://your-server.com/sse"
    }
  }
}
```

---

## Source: MicrosoftDocs/mcp

### HTTP Configuration
Add to `.gemini/settings.json`:
```json
{
  "server-name": {
    "httpUrl": "https://your-server.com/api/mcp"
  }
}
```

---

## Source: playwright-mcp

### STDIO Configuration
Follow the MCP install guide with standard config format.

---

## Source: Multiple repos (general pattern)

### Standard Format
```json
{
  "mcpServers": {
    "server-name": {
      "httpUrl": "https://your-server.com/api/mcp"
    }
  }
}
```

Or without wrapper:
```json
{
  "server-name": {
    "httpUrl": "https://your-server.com/api/mcp"
  }
}
```

---

## Notes
- Uses `httpUrl` key (different from other clients)
- Configuration file at `~/.gemini/settings.json`
- Supports HTTP transport
- Part of Google's Gemini ecosystem
