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
3. Configure the add-on (see below)
4. Start the add-on
5. Configure your AI assistant to connect via HTTP to the exposed port

## Configuration

```yaml
backup_hint: normal
port: 9583
```

### port

HTTP port for MCP communication (default: 9583). The add-on runs in streamable-http mode.

### backup_hint

Controls when backups are suggested before operations:

- `strong`: Before first modification of session
- `normal`: Before operations that cannot be undone (default)
- `weak`: Rarely suggests backups
- `auto`: Intelligent detection (future)

## Support

- **Issues**: https://github.com/homeassistant-ai/ha-mcp/issues
- **Documentation**: https://github.com/homeassistant-ai/ha-mcp
