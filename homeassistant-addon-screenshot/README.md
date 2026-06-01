# HA MCP Dashboard Screenshot Engine

Headless-Chromium engine that renders a Home Assistant Lovelace dashboard to a
PNG. It backs the [ha-mcp](https://github.com/homeassistant-ai/ha-mcp)
**dashboard screenshot** beta feature — the `ha_get_dashboard_screenshot` tool
and the `include_screenshot` / `return_screenshot` options on the dashboard
config tools — so an AI assistant can *see* a dashboard it reads or creates.

This add-on is **optional**. Install it only if you enable dashboard screenshot
mode in ha-mcp. It runs Chromium, so it is heavier than the MCP server itself;
keeping it separate is what lets the default ha-mcp install stay lightweight.

## Credit

This add-on is **derived from the Puppet add-on by Paulus Schoutsen
(balloob)** — https://github.com/balloob/home-assistant-addons — and is
vendored largely verbatim from it under the Apache-2.0 license. All credit for
the screenshot engine goes to that project. See [`NOTICE.md`](./NOTICE.md) and
[`LICENSE`](./LICENSE) for attribution and the list of ha-mcp modifications.

## Setup

1. Install and start this add-on.
2. **Create a Home Assistant long-lived access token** (Profile > Security,
   bottom of the page — ideally under a dedicated, low-privilege user) and set
   it as this add-on's **`access_token`** option, then restart the add-on.
3. In ha-mcp, enable the **dashboard screenshot mode** beta feature.

A token is required: Home Assistant's frontend only renders for a browser
holding a valid user session, and the add-on's Supervisor token is not such a
credential (Home Assistant Core rejects it). Without an `access_token`, the
add-on serves only a configuration-instructions page.

## Options

- `access_token` — **required in practice.** A Home Assistant long-lived access
  token; injected into the browser to authenticate.
- `home_assistant_url` — base URL the engine's browser opens. Defaults to the
  internal `http://homeassistant:8123`. Override only if your instance must be
  reached via a different hostname/port.
- `keep_browser_open` — keep Chromium alive between requests (faster repeat
  renders, more memory).

## Security

The engine's HTTP listener performs **no inbound authentication** and holds a
Home Assistant credential, so it must stay on the internal Supervisor/Docker
network only — do not publish its port to an untrusted LAN or the internet.
Prefer a token scoped to a dedicated low-privilege Home Assistant user.

## Docker / Container (no Supervisor)

If you run Home Assistant in plain Docker (no add-on store), run this image as a
sidecar with `access_token` set, on an internal network, and point ha-mcp at it
with `HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL=http://<engine-host>:10000`. See
[`docker-compose.screenshot.yml`](../docker-compose.screenshot.yml).

## HTTP API (reference)

ha-mcp calls the engine for you; you normally don't hit it directly. The engine
serves on port `10000` and any path you request is rendered:
`GET /<dashboard-path>?viewport=WxH` returns an image. Supported query
parameters (inherited from upstream Puppet): `viewport=WxH`, `zoom=N`,
`wait=<ms>` (extra settle time for slow/chart cards), `format=png|jpeg|webp|bmp`,
`rotate=90|180|270`, `theme=<name>`, `dark`, `lang=<code>`, and `colors=` /
`invert` for e-ink palettes. ha-mcp's `ha_get_dashboard_screenshot` exposes the
common ones (width/height/zoom/wait_ms) as tool parameters.
