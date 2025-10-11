# Home Assistant MCP Server Add-on

AI assistant integration for Home Assistant via Model Context Protocol (MCP).

## Capabilities

Control devices, manage automations/scripts/helpers, search entities with fuzzy matching, create backups, and query system states.

Full features: https://github.com/homeassistant-ai/ha-mcp

## Configuration

Both options are **Advanced** (hidden by default - enable "Advanced" mode in configuration UI).

### backup_hint (Advanced)

When to suggest backups:
- `normal` (default): Before irreversible operations
- `strong`: Before first modification of session
- `weak`: Rarely
- `auto`: Intelligent detection (future)

### secret_path (Advanced)

Custom secret path override. Leave empty for auto-generation (recommended).

- **Empty (default)**: Auto-generates 128-bit secure random path on first start
- Persisted to `/data/secret_path.txt` and reused on restarts

## Usage

**Auto-configuration** - The add-on automatically discovers your Home Assistant URL, authenticates, and generates a secure random endpoint path.

**Get your connection URL** from the add-on logs after startup:

```
🔐 MCP Server URL: http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw
```

Copy this complete URL to configure your AI client.

**Example (Claude Desktop):**

```json
{
  "mcpServers": {
    "home-assistant": {
      "url": "http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
      "transport": "http"
    }
  }
}
```

## Remote Access with Cloudflared Addon

For secure remote access (Claude.ai, ChatGPT.com, etc.) without port forwarding, install the **Cloudflared addon**:

[![Add Cloudflared Repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fbrenner-tobias%2Faddon-cloudflared)

**Configure Cloudflared addon** to expose HA MCP:

```yaml
additional_hosts:
  - hostname: ha-mcp.yourdomain.com  # Or use quick tunnel (see Cloudflared docs)
    service: http://localhost:9583
```

Your MCP URL becomes: `https://ha-mcp.yourdomain.com/private_zctpwlX7ZkIAr7oqdfLPxw`

**Cloudflared addon benefits:**
- No port forwarding
- Automatic DNS (if you have a domain) or quick tunnels (temporary URLs)
- Optional Cloudflare Zero Trust authentication
- Centrally managed with other Home Assistant services

See: [Cloudflared addon documentation](https://github.com/brenner-tobias/addon-cloudflared/blob/main/cloudflared/DOCS.md)

## Troubleshooting

**Add-on won't start:** Check logs for errors (invalid configuration, dependency failures)

**Can't connect:** Verify add-on is running, you copied the complete URL with secret path from logs, and your MCP client is configured correctly

**Lost the secret URL:** Check add-on logs or restart the add-on. Path is also stored in `/data/secret_path.txt`

**Operations failing:** Check add-on logs for error details

## Support

- Issues: https://github.com/homeassistant-ai/ha-mcp/issues
- Documentation: https://github.com/homeassistant-ai/ha-mcp
