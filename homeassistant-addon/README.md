# Home Assistant MCP Server Add-on

AI assistant integration for Home Assistant via Model Context Protocol (MCP).

## Features

- **20+ MCP Tools** for device control, automation management, and system queries
- **Zero Configuration** - Automatically discovers Home Assistant connection
- **Secure by Default** - Auto-generated secret paths with 128-bit entropy
- **Fuzzy Search** - Find entities even with typos
- **Backup & Restore** - Safe configuration management
- **Real-time Monitoring** - WebSocket-based state verification

## Installation

1. Add this repository to your Home Assistant add-on store
2. Install the "Home Assistant MCP Server" add-on
3. Start the add-on
4. **Check the add-on logs** for your unique MCP server URL
5. Copy the URL and configure your AI assistant

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

### secret_path (Advanced)

**Hidden option** - Enable "Advanced" mode in the configuration UI to see this option.

Custom secret path to override the auto-generated one. Leave empty (default) for automatic generation.

- When empty, the addon generates a secure 128-bit random path on first start
- The path is persisted to `/data/secret_path.txt` and reused on restarts
- Custom paths are useful for migration or specific security requirements

## Connection

After starting the add-on, **check the logs** to find your MCP server URL:

```
🔐 MCP Server URL: http://<home-assistant-ip>:9583/private_zctpwlX7ZkIAr7oqdfLPxw

   Secret Path: /private_zctpwlX7ZkIAr7oqdfLPxw

   ⚠️  IMPORTANT: Copy this exact URL - the secret path is required!
```

The URL format is:
```
http://<home-assistant-ip>:9583/private_<random-token>
```

Replace `<home-assistant-ip>` with your Home Assistant's IP address or hostname.

**Example URL**: `http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw`

## Support

- **Issues**: https://github.com/homeassistant-ai/ha-mcp/issues
- **Documentation**: https://github.com/homeassistant-ai/ha-mcp
