---
name: Cloudflare Tunnel
description: Expose ha-mcp to the internet securely
icon: cloudflare
forConnections: ['remote']
order: 4
---

## Overview

Cloudflare Tunnel creates a secure outbound connection from your network to Cloudflare's edge. No port forwarding or public IP required.

## Quick Tunnel (Easiest)

For testing or temporary access:

```bash
# Install cloudflared
# macOS: brew install cloudflared
# Linux: See https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

# Start tunnel
cloudflared tunnel --url http://localhost:8086
```

This gives you a temporary URL like `https://random-words.trycloudflare.com`

Your MCP URL: `https://random-words.trycloudflare.com/mcp`

## Persistent Tunnel (Recommended)

For permanent access with custom domain:

1. **Create tunnel:**
   ```bash
   cloudflared tunnel create ha-mcp
   ```

2. **Configure tunnel** (`~/.cloudflared/config.yml`):
   ```yaml
   tunnel: ha-mcp
   credentials-file: /path/to/.cloudflared/<tunnel-id>.json

   ingress:
     - hostname: ha-mcp.yourdomain.com
       service: http://localhost:8086
     - service: http_status:404
   ```

3. **Add DNS record:**
   ```bash
   cloudflared tunnel route dns ha-mcp ha-mcp.yourdomain.com
   ```

4. **Run tunnel:**
   ```bash
   cloudflared tunnel run ha-mcp
   ```

Your MCP URL: `https://ha-mcp.yourdomain.com/mcp`

## With HA Add-on (Cloudflared Add-on)

If using Home Assistant OS, install the Cloudflared add-on:

[![Add Cloudflared](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fbrenner-tobias%2Faddon-cloudflared)

Configure in add-on settings:
```yaml
additional_hosts:
  - hostname: ha-mcp.yourdomain.com
    service: http://localhost:9583
```

## Disable "Block AI Training Bots"

> **This is the most common connection issue for Cloudflare users.** If your LLM client can't connect but visiting the URL in your browser works, this setting is almost certainly the cause.

Cloudflare's "Block AI training bots" feature blocks requests from AI/LLM clients by default. You must disable it for your MCP connection to work:

1. Log in to [Cloudflare](https://dash.cloudflare.com)
2. In the left sidebar, click **Domains**, then click **Overview**
3. Click on the domain you use for connecting to Home Assistant
4. On the right side of the page, find **"Control AI Crawlers"**
5. Under **"Block AI training bots"**, open the dropdown
6. Select **"do not block (allow crawlers)"**

![Cloudflare AI Crawlers Setting](https://github.com/user-attachments/assets/3b949eff-951f-40d7-a460-17dfbfea045f)

## Security

- Traffic is encrypted end-to-end
- No inbound ports to open
- Optional: Add Cloudflare Access for authentication
