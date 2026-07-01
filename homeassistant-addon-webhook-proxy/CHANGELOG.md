# CHANGELOG

<!-- version list -->


## v1.2.3 (2026-07-01)

### Added

- Refuse to start when the other Webhook Proxy flavor is already running (stable refuses
  if the dev add-on `ha_mcp_webhook_proxy_dev` is running, and vice versa). Both flavors
  register the same root OAuth `/authorize` and `/token` routes, so only one may run at a
  time; the add-on now logs a clear error and raises a notification instead of colliding.

### Fixed

- Harden OAuth setup: create the signing-key and credential files with `0600` in the
  `open()` syscall (closing a brief chmod-after-write race), unregister the webhook if
  OAuth setup fails so no dangling registration is left behind, and defensively reject a
  non-object JWT payload instead of raising `AttributeError`.


## v1.2.2 (2026-06-29)

### Fixed

- Remove the `/` from the add-on name ("Nabu Casa / Webhook Proxy for HA MCP" ->
  "Nabu Casa - Webhook Proxy for HA MCP"). Home Assistant Supervisor builds the
  pre-update backup filename from the add-on name and validates it against
  `^[^/]+\.tar$`, so the slash made "Update" with "Create backup before update"
  enabled fail with `does not match regular expression` (issue #1707).

### Documentation

- Correct the "Log inbound requests" option description. It still said requests
  are logged to the Home Assistant log "NOT this addon log", which contradicts
  the v1.2.1 mirroring — the lines now appear in this addon's own log as well
  (issue #1708).


## v1.2.1 (2026-06-28)

### Added

- Mirror inbound-request debug lines into the addon's own log. When "Log
  inbound requests" is on, the lines that were previously only visible in the
  Home Assistant log (Settings → System → Logs) now also appear on the addon's
  Log tab, so you can confirm a client is reaching the server without leaving
  the addon page.

### Fixed

- Log a shutdown reason and run cleanup on a Supervisor stop. The addon now
  handles `SIGTERM`/`SIGINT`, so stopping it unregisters the webhook (as the
  docs describe) and records why it exited, instead of being killed mid-loop
  with no log line and the webhook left registered.

- Append a "fully restart Home Assistant" hint to the OAuth stale-registration
  errors (`invalid_client` and the browser "Invalid client id" page). The OAuth
  HTTP views only refresh on a full HA restart, so a regenerate / OAuth toggle /
  reinstall can otherwise leave a stale error with no obvious fix. (Client-side
  protocol errors and the upstream 502/500 paths don't get the hint — a restart
  isn't the fix there.)

### Documentation

- Warn that the Claude.ai connector must be deleted and re-created when OAuth
  is toggled on/off or the webhook URL changes — Claude.ai caches the
  authentication mode and URL per connector, so reusing the old one fails (for
  example `invalid client id` on the consent page).


## v1.2.0 (2026-06-15)

### Added

- Add a "Log inbound requests" debug toggle. When enabled, every request that
  reaches the webhook proxy is logged to the Home Assistant log (method, masked
  path, source address, whether an `Authorization` header was present, and the
  upstream response status) — making it easy to confirm whether an MCP client
  such as Claude.ai is actually reaching the server.

### Documentation

- Document the Claude.ai web custom-connector flow end to end (add the
  connector, click **Connect**, then **Allow** on the authorization page) and
  add a quick public-reachability check for diagnosing "Couldn't reach MCP
  server".

## v1.1.0 (2026-05-09)

### Added

- Optional OAuth 2.1 authentication mode for the webhook proxy (beta)
  ([#1184](https://github.com/homeassistant-ai/ha-mcp/pull/1184))

## v1.0.2 (2026-05-03)

### Fixed

- Surface webhook registration failures instead of silently loading
  ([#1101](https://github.com/homeassistant-ai/ha-mcp/pull/1101))

## v1.0.1 (2026-03-07)

### Fixed

- Correct webhook proxy Dockerfile COPY paths for Supervisor builds
  ([#725](https://github.com/homeassistant-ai/ha-mcp/pull/725))

## v1.0.0 (2026-03-06)

### Added

- Nabu Casa and other generic remote access via the webhook proxy
  ([#554](https://github.com/homeassistant-ai/ha-mcp/pull/554))
