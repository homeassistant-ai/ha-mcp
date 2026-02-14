# Comparison: Official HA MCP Server vs. ha-mcp

A detailed comparison of the [official Home Assistant MCP Server integration](https://www.home-assistant.io/integrations/mcp_server/) (`mcp_server`) and the [ha-mcp](https://github.com/homeassistant-ai/ha-mcp) project.

## Architecture

| | Official `mcp_server` | `ha-mcp` |
|---|---|---|
| **Runs where** | Inside Home Assistant Core as a built-in integration | External standalone server (Docker, binary, or `uv run`) |
| **Protocol layer** | Wraps HA's **Assist API / Intent system** | Directly calls HA **REST API + WebSocket API** |
| **Transport** | Streamable HTTP (`/api/mcp`) | Stdio, HTTP streaming, SSE, and OAuth 2.1 |
| **Auth** | OAuth (IndieAuth) + long-lived tokens; uses HA's built-in auth | Long-lived access token (env var); OAuth 2.1 mode with DCR for claude.ai |
| **Entity access control** | HA's "Exposed Entities" UI — per-entity, per-assistant | All entities accessible (no built-in filtering) |
| **Setup** | Enable in HA Settings > Integrations | Requires external deployment + env config |

## Tool Count & Scope

| | Official | ha-mcp |
|---|---|---|
| **Total tools** | ~30 (intent-based + a few specialized) | **97 tools** across 32 modules |
| **Approach** | Intent-driven (same as voice assistant) | Direct API — each HA capability gets a dedicated tool |

## What the Official Integration Has That ha-mcp Does Not

1. **Native HA integration** — zero external dependencies, runs inside HA itself, no Docker/binary needed.
2. **Exposed Entities access control** — granular per-entity visibility using HA's built-in UI. ha-mcp exposes everything the token can access.
3. **First-party OAuth/IndieAuth** — uses HA's authentication system natively; MCP clients like Claude Desktop can connect with just a URL, no token setup.
4. **`GetLiveContext` tool** — returns real-time state of *all* exposed entities in one call, purpose-built for LLM context windows. ha-mcp requires `ha_search_entities` + `ha_get_state` calls.
5. **Intent-level abstraction** — the LLM says "turn on the kitchen light" and HA's intent system handles resolution (name matching, area matching, etc.) using the same logic as voice assistants.
6. **MCP Prompts** — provides built-in MCP prompts that instruct the LLM how to interact with HA (ha-mcp doesn't expose MCP prompts).
7. **Seamless HA updates** — as HA Core adds new intents/domains, the MCP server automatically gains them.
8. **`HassMediaSearchAndPlay`** — media search and playback intent (ha-mcp has `ha_call_service` for media but not a dedicated search-and-play tool).
9. **Timer intents** — `HassStartTimer`, `HassCancelTimer`, `HassPauseTimer`, etc. are first-class tools.
10. **Broadcast intent** — `HassBroadcast` for announcing to speakers.

## What ha-mcp Has That the Official Integration Does Not

The official integration explicitly states: **"No administrative tasks can be performed."** ha-mcp's entire value proposition is that administrative tasks *can* be performed.

1. **Automation CRUD** — create, read, update, delete automations (`ha_config_set_automation`, `ha_config_get_automation`, `ha_config_remove_automation`). The official integration cannot create or edit automations.
2. **Script CRUD** — full script management. Official can *execute* exposed scripts but not create/edit them.
3. **Dashboard management** — 9 tools for full Lovelace dashboard CRUD, card manipulation, resource management, inline resource hosting via Cloudflare Worker. Not available in official at all.
4. **Helper entity management** — create/update/delete `input_boolean`, `input_number`, `input_text`, `input_select`, `input_datetime`, `counter`, `timer` helpers. Official cannot manage helpers.
5. **Blueprint import & management** — import blueprints from GitHub/community, list and inspect them.
6. **Deep configuration search** (`ha_deep_search`) — searches across automations, scripts, helpers, dashboards, areas simultaneously.
7. **Fuzzy entity search** — typo-tolerant search across entity IDs and friendly names with scoring and pagination.
8. **WebSocket real-time verification** — after a service call, ha-mcp monitors WebSocket `state_changed` events to *confirm* the device actually changed state (with timeout detection). Official just fires the intent.
9. **Bulk control with operation tracking** — `ha_bulk_control` for batch entity operations with per-operation status tracking and async completion monitoring.
10. **Area/Floor/Zone/Label/Group CRUD** — create/edit/delete areas, floors, zones, labels, groups. Official has no administrative tools.
11. **Device registry management** — rename, update, remove devices and entities.
12. **HACS integration** — 6 tools to search, install, and manage HACS custom components.
13. **History & statistics** — `ha_get_history` and `ha_get_statistics` for querying entity state history with time filtering.
14. **Automation trace debugging** — `ha_get_automation_traces` to inspect execution history, triggers, conditions, and actions.
15. **Template evaluation** — `ha_eval_template` for Jinja2 template rendering in HA context.
16. **Backup & restore** — create and restore system backups.
17. **File system access** — read/write/list files in HA's config directory.
18. **Camera snapshots** — `ha_get_camera_image` fetches camera images.
19. **Calendar CRUD** — create/delete calendar events (official can only *read* events).
20. **Todo list CRUD** — full create/update/delete (official has `HassListAddItem`/`HassListCompleteItem` but ha-mcp has full CRUD).
21. **System administration** — restart HA, reload core, check config validity, get system health, check for updates.
22. **Integration management** — enable/disable integrations, delete config entries.
23. **Structured error codes** — 38 standardized error codes with actionable suggestions. Official returns raw `HomeAssistantError`.
24. **Domain documentation on-demand** — `ha_get_domain_docs` provides progressive disclosure of schema docs.
25. **Tool module filtering** — `ENABLED_TOOL_MODULES` env var to expose only a subset of tools (e.g., "automation" preset).
26. **Add-on information** — query installed add-on details.
27. **Voice assistant exposure** — check which entities are exposed to Alexa/Google Home.
28. **SSE transport** — dedicated SSE mode for real-time streaming clients.

## Connectivity & Network Access

### Access Scenarios Matrix

| Scenario | Official `mcp_server` | ha-mcp |
|---|---|---|
| **Same machine** (e.g., Claude Desktop on HA host) | Need `mcp-proxy` shim (stdio-to-HTTP bridge) | Native stdio — just `ha-mcp` |
| **LAN** (e.g., Claude Desktop on laptop, HA on Raspberry Pi) | Need `mcp-proxy` + point at `http://ha-ip:8123/api/mcp` | `ha-mcp-web` on port 8086, direct HTTP |
| **Remote / WAN** (e.g., claude.ai, ChatGPT) | HA must be internet-accessible via Nabu Casa ($65/yr) or Cloudflare Tunnel | `ha-mcp-oauth` + Cloudflare Tunnel (free) |
| **HA Add-on** | N/A (it *is* HA) | Dedicated add-on, port 9583, auto-configured |

### Scenario 1: Same Machine (Claude Desktop)

**Official integration:**
- Not "one click" at all. Claude Desktop speaks **stdio**, the official integration speaks **Streamable HTTP**. You need a bridge.
- Install `mcp-proxy` (`uv tool install mcp-proxy`)
- Configure `claude_desktop_config.json` with the proxy command pointing to `http://localhost:8123/api/mcp`
- Community reports confusion — wrong `mcp-proxy` versions, path issues, spawn errors like `ENOENT`
- **Update (2025.7+):** Claude Desktop added a "Connectors" feature for remote MCP servers, which does simplify this for Claude Desktop specifically. But Cursor and other stdio-only clients still need the proxy.

**ha-mcp:**
- Native stdio: add `ha-mcp` to `claude_desktop_config.json` directly
- No proxy needed
- Set `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` env vars, done
- Also ships pre-built binaries (no Python/uv install required)

**Winner: ha-mcp** — no proxy shim, native stdio support.

### Scenario 2: Local Network (Claude Desktop on another machine)

**Official integration:**
- Claude Desktop Connectors: point at `http://ha-ip:8123/api/mcp` with a long-lived access token
- Or use `mcp-proxy` in `claude_desktop_config.json` pointing to the LAN address
- Works but requires generating a token in HA UI > Security > Long-Lived Access Tokens

**ha-mcp:**
- Run `ha-mcp-web` (binds `0.0.0.0:8086`)
- Point client at `http://ha-ip:8086/mcp`
- Or run as HA add-on (port 9583 with auto-generated secret path)
- Also needs a pre-configured token (set via env var)

**Roughly equivalent** — both require a token and an HTTP URL. The official integration is slightly simpler since HA is already running and serving on port 8123.

### Scenario 3: Remote / Internet (claude.ai, ChatGPT)

This is the hardest scenario and where the "one click" claim falls apart for both.

**Official integration:**
- claude.ai / ChatGPT need an **HTTPS URL** reachable from the internet
- Your HA instance must be publicly accessible. Options:
  - **Nabu Casa** ($65/year) — easiest, provides `https://xxxxx.ui.nabu.casa` URL. The `/api/mcp` endpoint is automatically available through this URL.
  - **Cloudflare Tunnel** — free, but requires setup (Cloudflared add-on or CLI, domain configuration)
  - **Reverse proxy** (Nginx/Caddy) + port forwarding + SSL cert
- Then configure the MCP client with the public URL + token
- Community reports: users struggle to figure out how to expose it, with forum posts going unanswered

**ha-mcp:**
- Dedicated **OAuth 2.1 mode** (`ha-mcp-oauth`) designed specifically for this:
  - Start server, run Cloudflare quick tunnel (`cloudflared tunnel --url http://localhost:8086`), set `MCP_BASE_URL`
  - Users get a **consent form** — enter their own HA URL + token. No pre-shared credentials needed.
  - Multi-user capable (each user authenticates independently)
- Also needs HTTPS tunnel, but:
  - Documented step-by-step for Cloudflare
  - Quick tunnel works for testing (one command, no account needed)
  - The OAuth consent form means **no token management** on the server side
- Has a setup wizard that walks through the process

**Winner: ha-mcp** — purpose-built OAuth mode with consent form. Official requires you to solve the "make HA internet-accessible" problem yourself with no MCP-specific guidance.

### Scenario 4: Home Assistant Add-on

**Official integration:**
- N/A — it *is* Home Assistant. No add-on concept.

**ha-mcp:**
- Installable as HA add-on from the repo
- Auto-configures using Supervisor API token (no manual token)
- Auto-generates 128-bit entropy secret path
- Runs on fixed port 9583
- Pair with Cloudflared add-on for remote access

### The "One Click" Claim — Reality

| Client | Official | ha-mcp |
|---|---|---|
| **Claude Desktop (Connectors)** | ~2 clicks IF HA is already internet-accessible | N/A (stdio mode doesn't need connectors) |
| **Claude Desktop (local, stdio)** | Install `mcp-proxy` + edit JSON config + generate token = ~10 steps | Edit JSON config + set env vars = ~5 steps |
| **claude.ai (web)** | Set up Nabu Casa OR Cloudflare Tunnel + generate token + configure client = 15-30 min | Run `ha-mcp-oauth` + quick Cloudflare tunnel + set `MCP_BASE_URL` = 10-15 min (consent form handles per-user auth) |
| **Cursor** | `mcp-proxy` + JSON config | JSON config pointing to `ha-mcp` binary |
| **ChatGPT** | Same as claude.ai — needs internet-accessible HA | Same as claude.ai — needs HTTPS tunnel |

### Key Connectivity Differentiator

The official integration piggybacks on HA's existing network setup — if you already have Nabu Casa or a Cloudflare tunnel for the HA UI, the MCP endpoint comes for free at `/api/mcp`. ha-mcp requires its *own* network exposure (separate port, separate tunnel), but provides more flexible transport options and a dedicated OAuth consent flow for multi-user remote access.

## ha-mcp Transport Modes

| Command | Transport | Port | Auth | Use Case |
|---------|-----------|------|------|----------|
| `ha-mcp` | stdio | N/A | Pre-token | Claude Desktop, local CLI |
| `ha-mcp-web` | HTTP | 8086 | Pre-token | LAN clients, single-user |
| `ha-mcp-sse` | SSE | 8087 | Pre-token | Legacy SSE clients |
| `ha-mcp-oauth` | HTTP+OAuth | 8086 | OAuth consent form | claude.ai, ChatGPT, multi-user |
| **Add-on** | HTTP | 9583 | Supervisor | Home Assistant Supervisor |

## Summary

The **official integration** is the right choice when you want a simple, secure experience that stays within HA's permission model and only need basic device control (on/off, brightness, climate, media). It benefits from native auth, entity exposure controls, and automatic updates with new HA releases.

**ha-mcp** is for power users who want AI assistants to perform *administrative* tasks — creating automations, managing dashboards, debugging automation traces, managing HACS packages, bulk device control with verification, and full system administration. It trades the simplicity and access control of the official integration for a dramatically broader tool surface (97 vs ~30 tools) and direct API access.

The fundamental architectural difference: the official integration says *"no administrative tasks can be performed"* — ha-mcp's entire value proposition is that administrative tasks *can* be performed.

## Sources

- [Official HA MCP Server Integration](https://www.home-assistant.io/integrations/mcp_server/)
- [HA LLM API Developer Docs](https://developers.home-assistant.io/docs/core/llm/)
- [HA Built-in Intents](https://developers.home-assistant.io/docs/intent_builtin/)
- [Cannot connect Claude or Cursor to HA MCP_Server](https://community.home-assistant.io/t/cannot-connect-claude-or-cursor-to-ha-mcp-server/874621)
- [Expose MCP integration to the web (unanswered)](https://community.home-assistant.io/t/expose-mcp-integration-to-the-web/899039)
- [ha-mcp Community Forum Thread](https://community.home-assistant.io/t/brand-new-claude-ai-chatgpt-integration-ha-mcp/937847)
