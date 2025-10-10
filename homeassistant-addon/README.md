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
require_auth: false
```

### port

HTTP port for MCP communication (default: 9583). The add-on runs in streamable-http mode.

### path

URL path for the MCP endpoint (default: /mcp).

**Connection URL**: `http://<home-assistant-ip>:<port><path>`

Example: `http://192.168.1.100:9583/mcp`

**Security Note**: If your Home Assistant is accessible from the internet or untrusted networks, change the path to a secret value (e.g., `/my-secret-mcp-endpoint-8a7f3b2c`). The path acts as a shared secret - only those who know it can access the MCP server.

### require_auth

Enable Home Assistant token authentication (default: false).

When enabled, clients must provide a valid Home Assistant long-lived access token via the `Authorization: Bearer <token>` header. The add-on validates tokens against the Home Assistant API.

**Creating a Long-Lived Access Token:**

1. In Home Assistant, go to your Profile → Security
2. Scroll to "Long-Lived Access Tokens"
3. Click "Create Token"
4. Give it a name (e.g., "MCP Server")
5. Copy the token immediately (you won't see it again)

**Client Configuration Example (Claude Desktop):**

```json
{
  "mcpServers": {
    "home-assistant": {
      "url": "http://homeassistant.local:9583/mcp",
      "headers": {
        "Authorization": "Bearer eyJhbGci...your-token-here"
      }
    }
  }
}
```

**Security Considerations:**

- **Defense in Depth**: Combine with a secret path for additional security
- **Token Management**: Tokens can be revoked in Home Assistant UI anytime
- **Network Security**: Still recommended to keep the add-on on a trusted network
- **Optional**: Leave disabled if your HA is only accessible on a secure local network

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
