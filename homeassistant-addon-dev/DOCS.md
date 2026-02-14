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
| `remote_url` | Remote base URL (optional, auto-detects Nabu Casa if blank) | `""` |

## Remote Access via Webhook Proxy

This feature allows external MCP clients (Claude.ai, Open WebUI, etc.) to connect to the add-on remotely through any reverse proxy setup — Nabu Casa, Cloudflare, DuckDNS, nginx, etc.

### How It Works

When enabled, the add-on auto-installs a lightweight `mcp_proxy` custom integration into Home Assistant. This integration registers an unauthenticated webhook endpoint that proxies MCP requests (including SSE streaming) to the add-on, bypassing the ingress session cookie requirement that external clients can't provide.

### Setup

1. Go to the add-on's **Configuration** tab
2. Set `enable_webhook_proxy` to **true**
3. *(Optional)* Set `remote_url` to your external URL (e.g. `https://my-ha.duckdns.org`). Leave blank to auto-detect Nabu Casa.
4. Click **Save**, then **Restart** the add-on
5. Check the add-on **Logs** — on first install you'll see:
   ```
   ************************************************************
     RESTART HOME ASSISTANT to load the new integration,
     then restart this add-on to complete setup.
     (Settings > System > Restart)
   ************************************************************
   ```
6. **Restart Home Assistant** (Settings > System > Restart) — this is a one-time step to load the `mcp_proxy` integration
7. **Restart the add-on** again
8. Copy the **MCP Server URL (remote)** from the add-on logs and paste it into your MCP client

### URL Auto-Detection

- If `remote_url` is left blank and Nabu Casa is enabled, the add-on auto-detects your Nabu Casa URL
- If `remote_url` is set, that URL is used as the base (works with any reverse proxy)
- The full remote URL is displayed in the add-on logs after setup

### Disabling

Set `enable_webhook_proxy` back to **false** and restart the add-on. The proxy config and config entry will be cleaned up automatically.

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
