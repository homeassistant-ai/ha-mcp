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
| `enable_oauth` | **Beta.** Require OAuth 2.1 on the webhook URL | `false` |
| `oauth_client_id` | OAuth Client ID (auto-generated if blank) | `""` |
| `oauth_client_secret` | OAuth Client Secret (auto-generated if blank) | `""` |
| `regenerate_oauth_creds` | One-shot: wipe stored OAuth creds and generate fresh ones on next start | `false` |

> The OAuth options are hidden by default. Toggle **Show unused optional configuration options** at the bottom of the addon's Configuration tab to reveal them.

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

### Authentication

By default the webhook URL is **unauthenticated** — possession of the URL is the only credential, and the URL functions as a shared bearer secret. The MCP server exposed through the proxy includes powerful Home Assistant control tools, so you should **treat the URL like a password**.

#### Treat the URL as a secret (default mode)

- **Don't share the URL** in screenshots, chat transcripts, log paste-bins, or public configs. Mask the path segment after `/api/webhook/` if you need to share anything.
- **Avoid copying it into untrusted clients.** Anyone with the full URL can call your MCP tools.
- **Rotate immediately if you suspect it leaked.** See below.

#### Rotating the webhook URL

The URL stays the same across addon restarts because the webhook ID is persisted at `/data/webhook_id.txt` inside the addon container. To rotate:

1. **Stop** the Webhook Proxy addon.
2. **Delete** the persisted webhook ID file. Open the addon's filesystem (e.g. via the SSH/Terminal addon or a file browser) and remove `/data/webhook_id.txt` for the proxy addon. (Stopping then uninstalling and reinstalling the addon also achieves this.)
3. **Start** the addon. A fresh webhook ID and URL are generated on first launch.
4. **Copy the new URL** from the addon logs and paste it into your MCP client(s). The old URL is now dead.

The old webhook ID is not retained anywhere on disk after the file is deleted, and the integration's previous registration is dropped when the addon stops.

#### Enable OAuth (Beta) for stronger protection

If you want a real auth layer on top of the URL secret, turn on **Enable OAuth (Beta)**.

**What it does:** the integration runs a minimal OAuth 2.1 authorization-code-with-PKCE flow on top of the same webhook URL. MCP clients that support OAuth (Claude.ai, ChatGPT, Cursor, etc.) will discover the OAuth endpoints automatically from a 401 response, send the user through a one-screen consent page, and exchange a code for a bearer token. The webhook then requires the bearer token on every request.

**How to enable:**

1. Toggle **Show unused optional configuration options** at the bottom of the Configuration tab.
2. Set **Enable OAuth (Beta)** to on. Leave **OAuth Client ID** and **OAuth Client Secret** blank — the addon will generate strong values for you on first start.
3. Save and **restart the addon**.
4. Open the addon log and copy the displayed Client ID and Client Secret:
   ```
   OAuth (Beta) is ENABLED for this URL.
     OAuth Client ID:     hamcp-1a2b3c4d5e6f...
     OAuth Client Secret: (configured)
   ```
   The full secret is also printed once in plaintext on the line above the masked summary so you can copy it.
5. In your MCP client, configure the OAuth fields:
   - **Claude.ai:** when adding the connector, expand **Advanced settings** and paste the Client ID and Client Secret into the OAuth fields. Claude.ai completes the rest automatically (consent screen → token exchange → bearer token).
   - **Other clients:** configure the same Client ID + Client Secret in the client's OAuth settings if it supports OAuth 2.1 with manual client registration.

The generated values are persisted at `/data/oauth_creds.json` inside the addon, so they stay the same across restarts.

**Rotating the credentials** — three options:

1. **Pick your own new values:** type new strings into the Client ID and Client Secret fields, save, restart the addon. Your values overwrite the persisted file. Update your MCP client to match.
2. **Get fresh random values via the UI** (no filesystem access needed): turn on **Regenerate OAuth Credentials on Next Start** in the addon configuration, save, restart. The addon wipes the stored creds, generates a fresh pair, prints them in the log, and flips the regenerate toggle back to off. Update your MCP client to match.
3. **Get fresh random values manually:** stop the addon, delete `/data/oauth_creds.json` (e.g. via SSH/Terminal addon), start the addon. Equivalent to option 2 but requires filesystem access.

**To disable:** set **Enable OAuth (Beta)** back to off and restart the addon. The webhook returns to plain unauthenticated behavior — the URL works as before with no token required.

**Endpoints exposed when enabled:**

- `/.../api/webhook/<id>` — MCP webhook (now bearer-protected)
- `/.../api/mcp_proxy/oauth/protected-resource` — RFC 9728 metadata
- `/.../api/mcp_proxy/oauth/authorization-server` — RFC 8414 metadata
- `/.../api/mcp_proxy/oauth/authorize` — consent screen
- `/.../api/mcp_proxy/oauth/token` — token endpoint

**Notes:**

- Tokens are HMAC-signed and stateless — they survive HA restarts. Access tokens expire after 1 hour; refresh tokens after 30 days.
- Rotating the Client ID invalidates all outstanding tokens (the client_id is part of the token's signed payload).
- The signing key is generated once and persisted at `/config/.mcp_proxy_oauth_secret`. Delete that file to invalidate every token in one shot.
- **Beta status:** the OAuth flow is implemented against the [MCP 2025-06-18 spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) but real-world MCP-client coverage varies. The URL-as-secret mode (default) is the stable, documented path. Treat OAuth as opt-in until tested with your client. Report problems on GitHub.

## How it works

1. The addon installs a lightweight `mcp_proxy` custom integration into Home Assistant
2. By default this integration registers an **unauthenticated** webhook endpoint (`/api/webhook/<id>`) — the URL itself is the shared secret
3. When a request hits the webhook, it is proxied to the MCP server addon (after a bearer-token check, if **Enable OAuth** is on)
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
