# Home Assistant MCP Server Add-on

AI assistant integration for Home Assistant via Model Context Protocol (MCP).

## Features

- **20+ MCP Tools** for device control, automation management, and system queries
- **Zero Configuration** - Automatically discovers Home Assistant connection
- **Fuzzy Search** - Find entities even with typos
- **Backup & Restore** - Safe configuration management
- **Real-time Monitoring** - WebSocket-based state verification

## Installation

1. Add this repository to your Home Assistant add-on store
2. Install the "Home Assistant MCP Server" add-on
3. Start the add-on
4. Configure your AI assistant (Claude Desktop, etc.) to connect via stdio

## Configuration

```yaml
backup_hint: normal
```

### backup_hint

Controls when backups are suggested before operations:

- `strong`: Before first modification of session
- `normal`: Before operations that cannot be undone (default)
- `weak`: Rarely suggests backups
- `auto`: Intelligent detection (future)

## Support

- **Issues**: https://github.com/homeassistant-ai/ha-mcp/issues
- **Documentation**: https://github.com/homeassistant-ai/ha-mcp
