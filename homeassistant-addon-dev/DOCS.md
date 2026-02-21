# Home Assistant MCP Server (Dev Channel) - Documentation

**WARNING: This is the development channel. Expect bugs and breaking changes.**

This add-on receives updates with every commit to master. For stable releases, use the main "Home Assistant MCP Server" add-on.

## Configuration

The dev add-on uses the same configuration as the stable version. See the main add-on's documentation for full details.

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `backup_hint` | Backup strength preference | `normal` |
| `secret_path` | Custom secret path (optional) | auto-generated |
| `enable_webhook_proxy` | Enable remote access via webhook proxy | `false` |
| `remote_url` | Your external URL (optional, auto-detects Nabu Casa if blank) | `""` |

## Remote Access via Webhook Proxy (Dev Feature)

The dev addon includes an opt-in webhook proxy that enables remote MCP access through any reverse proxy — Nabu Casa, Cloudflare, DuckDNS, nginx, etc.

### How it works

When `enable_webhook_proxy` is turned on, the addon auto-installs a lightweight `mcp_proxy` custom integration into Home Assistant. This integration registers an unauthenticated webhook endpoint (`/api/webhook/<id>`) that proxies MCP requests to the addon, bypassing the ingress session cookie requirement that external MCP clients can't provide.

### Setup

1. Go to the addon's **Configuration** tab
2. Toggle `enable_webhook_proxy` **on**
3. *(Optional)* Set `remote_url` to your external URL (e.g. `https://ha.example.com`). Leave blank for Nabu Casa auto-detection.
4. Click **Save**, then **Start** (or **Restart**) the addon
5. Check the addon **Log** tab — on first install you'll see:
   ```
   ************************************************************
     RESTART HOME ASSISTANT to load the new integration,
     then restart this add-on to complete setup.
     (Settings > System > Restart)
   ************************************************************
   ```
6. Restart Home Assistant (Settings > System > Restart)
7. Restart the addon
8. Copy the remote URL from the logs:
   ```
   MCP Server URL (remote): https://xxxxx.ui.nabu.casa/api/webhook/mcp_xxxxxxxx
   ```
9. Paste that URL into your MCP client (Claude Desktop, Open WebUI, etc.)

### URL auto-detection

- **Nabu Casa subscribers**: Leave `remote_url` blank — the addon reads your Nabu Casa domain automatically
- **Other reverse proxies**: Set `remote_url` to your external URL (e.g. `https://ha.example.com`)

### Disabling

Toggle `enable_webhook_proxy` off and restart the addon. The proxy config and config entry are cleaned up automatically.

## Updates

The dev channel updates automatically with every commit to master. You may receive multiple updates per day.

To check for updates:
1. Go to Settings > Add-ons
2. Click on "Home Assistant MCP Server (Dev)"
3. Click "Check for updates"

## Switching to Stable

If you want to switch back to stable releases:
1. Uninstall this dev add-on
2. Install the main "Home Assistant MCP Server" add-on

Your configuration will need to be reconfigured.

## Reporting Issues

When reporting issues from the dev channel, please include:
- The commit SHA (shown in the add-on info)
- Steps to reproduce
- Any error logs from the add-on

Issues: https://github.com/homeassistant-ai/ha-mcp/issues
