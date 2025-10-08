# Home Assistant MCP Server Add-on

AI assistant integration for Home Assistant via Model Context Protocol (MCP).

Control and manage your Home Assistant setup using natural language through AI assistants like Claude.

## Features

- **20+ MCP Tools** for device control, automation management, and system queries
- **Zero Configuration** - Automatically discovers Home Assistant connection
- **Fuzzy Search** - Find entities even with typos
- **Backup & Restore** - Safe configuration management
- **Real-time Monitoring** - WebSocket-based state verification

## Installation

1. Add this repository to your Home Assistant add-on store
2. Install the "Home Assistant MCP Server" add-on
3. Configure your backup hint preference (optional)
4. Start the add-on

## Configuration

```yaml
backup_hint: normal
```

### Option: `backup_hint`

Controls when the MCP server recommends creating backups:

- `strong`: Before the first modification of day/session
- `normal`: Before operations that cannot be undone (default)
- `weak`: Rarely suggests backups
- `auto`: Intelligent detection (future)

## Usage

Once started, the add-on runs an MCP server that your AI assistant can connect to.

Configure your MCP client (Claude Desktop, etc.) to use this add-on's stdio interface.

## Support

For issues and feature requests, visit:
https://github.com/homeassistant-ai/ha-mcp/issues

## Documentation

Full documentation available at:
https://github.com/homeassistant-ai/ha-mcp
