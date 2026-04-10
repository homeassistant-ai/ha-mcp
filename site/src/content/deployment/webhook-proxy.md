---
name: Webhook Proxy Add-on
description: Remote access via existing reverse proxy (Nabu Casa, Cloudflare, etc.)
icon: webhook
forConnections: ['remote']
order: 5
---

## Overview

The Webhook Proxy add-on enables remote access to ha-mcp through any reverse proxy that already points at your Home Assistant instance — Nabu Casa, Cloudflare, DuckDNS, nginx, or any other. Instead of requiring a dedicated tunnel to port 9583, it proxies MCP traffic through HA's main port (8123) via a webhook.

**Best for users who already have Nabu Casa or another reverse proxy pointing at Home Assistant.**

## How It Works

```
AI Client → HTTPS → Your Reverse Proxy → HA (port 8123) → Webhook → MCP Server Add-on
```

1. The add-on installs a lightweight integration that registers a webhook endpoint
2. Incoming MCP requests hit `/api/webhook/<id>` on your existing HA URL
3. The webhook proxies requests to the MCP Server add-on running locally

## Prerequisites

- **Home Assistant MCP Server** add-on must be installed and running
- **Nabu Casa** subscription with remote UI enabled, **or** a working reverse proxy pointing at your HA instance

## Installation

1. **Install the MCP Server add-on first** (if not already installed)

2. **Install the Webhook Proxy add-on** from the same add-on store

3. **Start the add-on** — on first run it installs the integration and asks you to restart HA

4. **Restart Home Assistant** (Settings > System > Restart)

5. **Copy the remote URL** from the add-on logs:
   ```
   MCP Server URL (remote): https://xxxxx.ui.nabu.casa/api/webhook/mcp_xxxxxxxx
   ```

## Configuration

- **Nabu Casa users**: Leave all settings blank — everything is auto-detected
- **Other reverse proxies**: Set `remote_url` to your external HA URL (e.g., `https://ha.example.com`)

## Comparison with Cloudflare Tunnel

| | Webhook Proxy | Cloudflare Tunnel |
|---|---|---|
| **Setup** | Install add-on, restart HA | Install Cloudflared, configure tunnel |
| **Requires** | Existing reverse proxy / Nabu Casa (paid) | Cloudflare account (free) |
| **Routing** | Through HA web server (port 8123) | Direct to MCP port (9583) |
| **Port** | Uses HA's port 8123 | Dedicated port 9583 |
| **Best for** | Already have Nabu Casa / reverse proxy | No existing proxy, or want direct connection |
