# Attribution

The **HA MCP Dashboard Screenshot Engine** add-on (`ha_mcp_screenshot`) is
**derived from the Puppet add-on by Paulus Schoutsen (balloob)**:

- Source: https://github.com/balloob/home-assistant-addons (`puppet/`)
- License: Apache License 2.0 (see [`LICENSE`](./LICENSE))

The screenshot engine itself — the `ha-puppet/` Node service that drives
headless Chromium, injects the Home Assistant auth token into the browser, and
serves rendered PNGs over HTTP — is **vendored largely verbatim** from that
project and retains its Apache-2.0 license. The substance of how it works is
balloob's; full credit to that project.

## Modifications by the ha-mcp project (homeassistant-ai/ha-mcp)

- Rebranded the add-on metadata (`config.yaml`) as `ha_mcp_screenshot` and
  repackaged it as the opt-in engine behind the ha-mcp
  `ha_get_dashboard_screenshot` tool and the `include_screenshot` /
  `return_screenshot` parameters on the dashboard config tools.
- Built locally by the Supervisor (no `image:` key), matching the ha-mcp
  webhook-proxy add-on packaging; no host `ports:` mapping (reached only over
  the internal Supervisor network); optional preview UI via `ingress`.
- Removed `homeassistant_api` from `config.yaml`: the engine authenticates only
  with the user-configured long-lived `access_token`, never the Supervisor
  token, so it needs no Supervisor-granted HA API access.
- Hardened `ha-puppet/screenshot.js` login-screen detection so an
  absent/expired token fails with a clear 401 instead of crashing or returning
  a login-page PNG.

Authentication is unchanged from upstream in substance: it requires a
user-pasted Home Assistant long-lived access token. See [`README.md`](./README.md).
