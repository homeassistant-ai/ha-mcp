# Raycast (Raycast)

## Overview
Raycast is a macOS productivity app with native MCP support since v1.98.0 (May 2025).

## Configuration File Location
- **macOS**: `mcp-config.json` in Extension's support directory
- Find via: Manage MCP Servers command → Show Config File in Finder

---

## Source: Official Raycast Documentation (manual.raycast.com/model-context-protocol)

### Configuration Format
```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your-token"
      }
    }
  }
}
```

### Quick Install Method
1. Copy the JSON config above
2. Open Raycast → Manage MCP Servers command
3. Press Cmd + N
4. Paste the JSON
5. Modify fields as needed (API keys, etc.)

### Via Manage Servers Command
1. Search for "Manage MCP Servers" in Raycast
2. Use "Install Server" command
3. Enter server details

---

## Key Features
- Auto-fills form if you copy JSON before opening Install Server
- Servers accessible via @-mention in Quick AI and AI Chat
- Server version displayed in Manage Servers
- HTTP client compatible with Atlassian's MCP server (v1.101.0+)

## PATH Notes
- Raycast passes PATH from default SHELL to process
- If command works in terminal, it should work in Raycast
- Restart Raycast after PATH changes

## Notes
- macOS only
- Uses standard mcpServers JSON format (like Claude Desktop)
- Supports custom server development in JavaScript
- Recent updates include DENO_PATH and NODE_PATH support
