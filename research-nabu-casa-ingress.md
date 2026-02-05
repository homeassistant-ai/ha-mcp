# Research: Nabu Casa Ingress for ha-mcp Remote Access

## Goal

Enable Nabu Casa subscribers to access ha-mcp remotely without needing Cloudflare Tunnel setup. This would dramatically simplify WAN access for HAOS users.

## Current State

- Add-on listens on port 9583 via `host_network: true`
- Remote access requires Cloudflare Tunnel (domain + CF account + second add-on + YAML config)
- `ingress: true` is **NOT** currently enabled in `homeassistant-addon/config.yaml`
- No ingress-related code exists anywhere in the codebase

## How Nabu Casa Remote Access Works

Nabu Casa uses **SniTun** (not Cloudflare) -- a custom open-source TCP multiplexer:

1. HA opens an **outbound** TCP connection to SniTun proxy servers (no port forwarding needed)
2. External requests to `https://<id>.ui.nabu.casa` are routed via TLS SNI inspection
3. It's a **layer 4 (TCP) proxy** -- forwards raw encrypted bytes, doesn't inspect HTTP
4. End-to-end TLS encryption maintained; proxy cannot decrypt traffic

**What Nabu Casa tunnels:** ALL traffic to the HA web server (port 8123), including:
- REST API (`/api/*`) with Bearer token auth
- WebSocket API (`/api/websocket`)
- **Add-on Ingress panels** (`/api/hassio_ingress/<token>/...`)

**What it does NOT tunnel:** Arbitrary add-on ports (like 9583). The only way to get ha-mcp accessible through Nabu Casa is via **Ingress**.

## Ingress Proxy Technical Details

### HTTP Methods
The Supervisor ingress proxy allows **ALL HTTP methods** (`METH_ANY` wildcard):
- GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD all pass through
- Source: `supervisor/api/ingress.py` uses `web.route(hdrs.METH_ANY, ...)`
- Source: `homeassistant/components/hassio/ingress.py` registers handlers for all 7 methods

### Path Rewriting
- Request to `/api/hassio_ingress/<token>/some/path` arrives at the add-on as just `/some/path`
- Query parameters are preserved
- `X-Ingress-Path` header provides the base path for generating absolute URLs
- The add-on's existing secret path mechanism works unchanged through ingress

### Response Streaming
- Responses without `Content-Length` or > 4MB use `StreamResponse` with chunk-based iteration
- `X-Accel-Buffering: no` header is set to prevent nginx buffering
- SSE responses (no Content-Length, infinite stream) take the streaming path

### Request Body Streaming
- `ingress_stream: true` config option streams POST request bodies instead of buffering
- Without it, entire POST body is read into memory first
- Relevant for MCP's streamable-http transport

### WebSocket
- Full bidirectional WebSocket proxying (TEXT, BINARY, PING, PONG)
- Not needed for MCP streamable-http but good to know

## SSE Compression Bug (THE BLOCKER)

**Issue:** [home-assistant/supervisor#6470](https://github.com/home-assistant/supervisor/issues/6470) (filed Jan 2026)

**Problem:** HA Core's compression middleware incorrectly applies `Content-Encoding: deflate` to `text/event-stream` responses through ingress. The client tries to decompress, buffers everything, and SSE events only arrive after connection closes.

**Root cause:** `should_compress()` in HA Core doesn't exclude `text/event-stream`:
```python
# text/event-stream falls through to the final `return True`
def should_compress(content_type, path=None):
    if content_type.startswith("image/"): ...
    if content_type.startswith("application/"): ...
    return not content_type.startswith(("video/", "audio/", "font/"))
```

**Fix:** [home-assistant/core#160704](https://github.com/home-assistant/core/pull/160704) -- adds `text/event-stream` to exclusion list. **Not yet merged** as of Feb 2026.

**Impact on MCP:** MCP streamable-http uses SSE for:
1. POST responses returning as `text/event-stream` (primary data flow)
2. GET endpoint for persistent SSE stream (server-initiated messages)

Both would be broken by this bug.

**Possible workaround:** ha-mcp could try setting `Content-Encoding: identity` or `Cache-Control: no-transform` on SSE responses to prevent the compression middleware from touching them. Needs testing.

## Will Ingress Break Existing Users?

**No.** It is purely additive:
- `ports: 9583/tcp: 9583` stays unchanged
- `host_network: true` stays unchanged
- MCP server still listens on 9583, unchanged
- Cloudflare Tunnel users connect to `<host>:9583`, unchanged
- Ingress is a second, parallel access path managed by the Supervisor
- Canonical example: SSH add-on has both `ingress: true` + `ports: 22/tcp` for years

## Implementation Plan

### Step 1: config.yaml (DONE on this branch)

Added after `host_network: true`:
```yaml
# Enable ingress for Nabu Casa remote access (coexists with direct port access)
ingress: true
ingress_port: 0        # Dynamic port allocation (required with host_network)
ingress_stream: true   # Stream POST request bodies for MCP transport
```

### Step 2: start.py changes (TODO)

The Supervisor provides the dynamically assigned ingress port. The add-on needs to also listen on it. Two options:

**Option A: Two server instances** -- start the MCP server on both port 9583 AND the ingress port. More complex but clean separation.

**Option B: Reverse proxy** -- use a lightweight internal proxy (or just configure the MCP server to bind to both). Simpler if FastMCP supports multiple binds.

**Option C: Use ingress_port: 9583** -- point ingress at the existing port. Zero start.py changes. But the HA docs recommend `ingress_port: 0` with `host_network: true` to avoid conflicts. Worth testing if 9583 works -- if it does, this is the simplest path (config.yaml only, no code changes).

The ingress port is available via:
- Supervisor API: `GET /addons/self/info` returns `ingress_port`
- Possibly `INGRESS_PORT` env var (needs verification)

### Step 3: Documentation (TODO)

Add Nabu Casa as a remote access option in `site/src/content/connections/remote.md`.

### Step 4: Log the ingress URL (TODO)

In `start.py`, detect ingress and log the Nabu Casa URL pattern:
```
MCP Server URL (Nabu Casa): https://<your-id>.ui.nabu.casa/api/hassio_ingress/<token>/<secret-path>
```

The ingress token is available via the Supervisor API.

## Key Config Options Reference

| Option | Type | Default | Purpose |
|--------|------|---------|---------|
| `ingress` | bool | false | Enable ingress |
| `ingress_port` | int/0 | 8099 | Port add-on listens on for ingress (0 = dynamic) |
| `ingress_entry` | string | `/` | URL entry point for panel |
| `ingress_stream` | bool | false | Stream POST bodies instead of buffering |
| `panel_icon` | string | `mdi:puzzle` | Sidebar panel icon |
| `panel_title` | string | add-on name | Sidebar panel title |
| `panel_admin` | bool | true | Restrict panel to admin users |

## Key Source Code References

- Supervisor ingress proxy: `home-assistant/supervisor` `supervisor/api/ingress.py`
- HA Core ingress proxy: `home-assistant/core` `homeassistant/components/hassio/ingress.py`
- SniTun: `github.com/NabuCasa/snitun`
- hass-nabucasa library: `github.com/NabuCasa/hass-nabucasa`
- SSH add-on (ingress + ports example): `home-assistant/addons` `ssh/config.yaml`
- ha-mcp add-on config: `homeassistant-addon/config.yaml`
- ha-mcp add-on startup: `homeassistant-addon/start.py`
- ha-mcp server entry: `src/ha_mcp/__main__.py` (runs `stateless_http=True`)
- Existing remote docs: `site/src/content/connections/remote.md`

## Open Questions

1. **Can `ingress_port: 9583` work with `host_network: true`?** If yes, zero code changes needed.
2. **How does the Supervisor communicate the dynamic ingress port?** Env var? API only?
3. **Will the SSE compression bug workaround work?** Test `Content-Encoding: identity` header.
4. **When will core#160704 merge?** This is the upstream fix for the SSE bug.
5. **DELETE method**: Already blocked since HA 2026.1 over Cloudflare anyway. ha-mcp runs `stateless_http=True` so DELETE on MCP endpoint is a no-op regardless.
