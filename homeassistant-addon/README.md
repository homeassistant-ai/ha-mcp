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
5. Check the add-on logs for the connection URL
6. Configure your AI assistant to connect via HTTP to the displayed URL

## Configuration

```yaml
backup_hint: normal
port: 9583
path: /mcp
```

### port

HTTP port for MCP communication (default: 9583). The add-on runs in streamable-http mode.

### path

URL path for the MCP endpoint (default: /mcp).

**Connection URL**: `http://<home-assistant-ip>:<port><path>`

Example: `http://192.168.1.100:9583/mcp`

### backup_hint

Controls when backups are suggested before operations:

- `strong`: Before first modification of session
- `normal`: Before operations that cannot be undone (default)
- `weak`: Rarely suggests backups
- `auto`: Intelligent detection (future)

## Connection

After starting the add-on, check the logs to find the connection URL. The URL format is:

```
http://<home-assistant-ip>:9583/mcp
```

Replace `<home-assistant-ip>` with your Home Assistant's IP address or hostname.

## Support

- **Issues**: https://github.com/homeassistant-ai/ha-mcp/issues
- **Documentation**: https://github.com/homeassistant-ai/ha-mcp
