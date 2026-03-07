# Webhook Proxy for HA MCP - Documentation

Remote access proxy for the Home Assistant MCP Server addon via webhooks.

## About

This addon enables remote access to your HA MCP Server through any reverse proxy — Nabu Casa, Cloudflare, DuckDNS, nginx, or any other. It does **not** run its own MCP server; instead it discovers your existing MCP Server addon (stable or dev) and proxies requests to it via a Home Assistant webhook.

## Prerequisites

- **Home Assistant MCP Server** addon must be installed and running (stable or dev channel)
- For Nabu Casa auto-detection: active Nabu Casa subscription with remote UI enabled
- For other setups: a working reverse proxy pointing at your HA instance

## Setup

1. **Install this addon** from the add-on store
2. **Start the addon** — on first run it will install the integration and create a notification asking you to restart Home Assistant
3. **Restart Home Assistant** (Settings > System > Restart) — the addon detects the restart and automatically finishes setup
4. **Copy the remote URL** from the addon logs:
   ```
   MCP Server URL (remote): https://xxxxx.ui.nabu.casa/api/webhook/mcp_xxxxxxxx
   ```
5. **Paste the URL** into your MCP client (Claude Desktop, Claude.ai, Open WebUI, etc.)

> **Note:** If something doesn't seem to work after restarting HA, try restarting the addon as well.

## Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `remote_url` | Your external URL (auto-detects Nabu Casa if blank) | `""` |
| `mcp_server_url` | Full MCP server URL override (auto-detects if blank) | `""` |
| `mcp_port` | MCP server port used during auto-discovery | `9583` |

### Auto-detection

When `mcp_server_url` is left blank (recommended), the addon automatically:

1. Finds the running MCP Server addon (tries stable `ha_mcp` first, then dev `ha_mcp_dev`)
2. Gets its container IP address from the Supervisor API
3. Discovers the secret path from the addon's options or logs
4. Constructs the target URL: `http://<ip>:<port>/<secret_path>`

### Manual URL override

If auto-detection doesn't work for your setup (e.g. non-standard port, custom networking), set `mcp_server_url` to the full MCP server URL:

```
http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw
```

### Remote URL

- **Nabu Casa subscribers**: Leave `remote_url` blank — auto-detected from cloud storage
- **Cloudflare/DuckDNS/nginx**: Set `remote_url` to your external URL (e.g. `https://ha.example.com`)

## How it works

1. The addon installs a lightweight `mcp_proxy` custom integration into Home Assistant
2. This integration registers an unauthenticated webhook endpoint (`/api/webhook/<id>`)
3. When a request hits the webhook, it is proxied to the MCP server addon
4. The addon stays alive with a periodic health check loop

The webhook bypasses the ingress session cookie requirement that external MCP clients cannot provide.

## Troubleshooting

### "No running MCP addon found"

The main MCP Server addon is not running. Install and start it first:
- Settings > Add-ons > Home Assistant MCP Server > Start

### "Could not discover secret path"

The addon could not find the secret path. Options:
1. Check that the MCP Server addon has started successfully and shows a URL in its logs
2. Set `mcp_server_url` manually in this addon's configuration

### "MCP server unreachable"

The health check cannot reach the MCP server. Check:
1. The MCP Server addon is still running
2. No network/firewall issues between addons
3. The port matches (default 9583)

### Integration not loading

If the `mcp_proxy` integration doesn't appear in Settings > Devices & Services:
1. Restart Home Assistant (Settings > System > Restart)
2. The addon will start automatically and retry setup

## Disabling / Uninstalling

- **Stopping** the addon is safe — the webhook URL stays the same and resumes working when the addon is restarted
- **Uninstalling** the addon does not automatically remove the custom integration files. To fully clean up after uninstalling:
  1. Delete `/config/custom_components/mcp_proxy/`
  2. Delete `/config/.mcp_proxy_config.json`
  3. Restart Home Assistant

## Support

**Issues:** https://github.com/homeassistant-ai/ha-mcp/issues
**Documentation:** https://github.com/homeassistant-ai/ha-mcp
