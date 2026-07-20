# CHANGELOG

<!-- version list -->


## v2.0.5 (2026-07-20)

### Bug Fixes

- When OAuth is off, serve the add-on's own corrected OAuth discovery documents
  plus an invisible auto-approve authorization server, so an MCP connector
  (claude.ai) that intermittently front-loads OAuth discovery resolves against
  the add-on instead of falling through to Home Assistant core's origin-root
  `/.well-known/oauth-authorization-server` — which omits
  `token_endpoint_auth_methods_supported: ["none"]` and has no
  `registration_endpoint`, so the connector reports "Automatic client
  registration isn't supported" and cannot connect (issue #1969). The webhook
  itself stays unauthenticated (URL-only clients are unaffected) and the OAuth
  flow completes with no Home Assistant login. Switching between OAuth-off and
  `ha_auth` needs no Home Assistant restart.
- Follow-up to the above (issue #1978): the none-mode auto-approve OAuth error
  responses now carry `Cache-Control: no-store` / `Pragma: no-cache`, matching
  the token responses and the custom-component twin.


## v2.0.4 (2026-07-18)

### Refactoring

- Reduce cyclomatic complexity in `start.py` and `mcp_proxy/__init__.py`
  below the C901 threshold by extracting private helpers (issue #925),
  synced from the dev flavor's 2.0.3.dev2. No behavior change.


## v2.0.3 (2026-07-18)

### Bug Fixes

- Write the proxy-config handoff file with restricted (0600) permissions like
  the OAuth creds file, falling back to a plain write with a logged warning
  when the filesystem cannot honor the mode.


## v2.0.2 (2026-07-13)

### Changed

- External URL help text now spells out that the URL must point directly at
  Home Assistant (its base address should reach your HA login page) and must
  not contain a port such as `:8123` — remote MCP clients can't reach a URL
  that carries a port. Clarifies the most common remote-connection failure
  (issue #1864).
- DOCS now note that users of the HA-MCP custom component (in-process server)
  don't need this add-on — the component ships its own webhook for remote
  access.


## v2.0.1 (2026-07-04)

### Added

- ha_auth debug observability: with debug logging enabled, a 401 on the webhook
  now logs WHY the bearer was rejected — no usable bearer, token rejected by
  Home Assistant's validator, or the validator raised — so provider-specific
  login issues (issue #1714's OIDC leg) are diagnosable from the add-on log
  alone. The token itself is never logged.


## v2.0.0 (2026-07-03)

> **POTENTIAL BREAKING CHANGE (OAuth users).** This release changes the default
> OAuth mode for *new* enables. Upgrades are engineered to be safe — existing
> OAuth setups are auto-detected and kept on the old (legacy) mode — but if you
> use OAuth, read the notes below. `enable_oauth` stays OFF by default; nothing
> changes for anyone not using OAuth.

### Added

- New default OAuth mode `ha_auth` that delegates authorization to Home
  Assistant's built-in OAuth: you sign in with your Home Assistant account and
  the connector's OAuth fields stay blank (the add-on advertises Client ID
  Metadata Documents, so no client id/secret is needed). It works with any
  hostname regardless of Home Assistant's external URL, and needs no Home
  Assistant restart to enable or disable. Validated live against claude.ai; also
  enables ChatGPT (#1725). Follow-up to #1714.
- Serve the OAuth metadata at the RFC 8414 / RFC 9728 / OIDC well-known
  locations (issue #1714): the authorization-server document at
  `/.well-known/oauth-authorization-server/api/mcp_proxy/oauth` (plus the
  `openid-configuration` variants), and the protected-resource document at the
  path-scoped `/.well-known/oauth-protected-resource/api/webhook/<id>`. The
  path-scoped document is claude.ai's first fallback probe when the 401's
  `WWW-Authenticate` pointer is missing, and a valid authorization-server
  document at the well-known path overrides a previously mis-cached per-URL
  client config — healing a broken connector with no client-side action.
- Click-to-restart Repair (HACS-style) now appears the moment a restart is
  needed, not only at the next HA boot: the integration registers a
  `refresh_repairs` service the add-on calls when the integration files were
  updated on disk or OAuth was enabled against stale loaded code. The stale
  "integration updated" notification is auto-dismissed once the new code
  actually loads.

### Changed

- OAuth's default for a first-time enable is now `ha_auth`. What this means for
  OAuth users:
  - OAuth setups from before this update keep working unchanged — legacy mode is
    auto-detected (from a configured or stored Client ID/Secret) and kept.
  - New / first-time OAuth enables default to the new `ha_auth` mode.
  - Anyone switching modes must delete and re-add their MCP connector: set
    `oauth_mode: ha_auth` (blank credentials) to move to the new mode, or
    `oauth_mode: legacy` to pin the previous client-id/secret flow.
  The legacy flow is unchanged and still available (deprecated).
- The add-on now refuses to start when its options file is present but
  unreadable or corrupt, instead of silently starting with OAuth defaulted off.
  A restart normally recovers on its own (the Supervisor rewrites the file from
  the saved configuration on every start).

### Fixed

- Repair cards now render with proper text: custom integrations load runtime
  translations from `translations/en.json`, which was missing.
- A bearer token containing non-ASCII characters now receives the standard 401
  discovery challenge in legacy OAuth mode, instead of an unhandled error that
  surfaced as an empty 200 response.

### Documentation

- Add a "Cloudflare users" troubleshooting section to DOCS.md: disable
  "Block AI training bots" and don't geo-block your AI provider's US IP
  ranges (Claude.ai connects from Anthropic's network, `160.79.104.0/21`).


## v1.2.3 (2026-07-01)

### Added

- Refuse to start when the other Webhook Proxy flavor is already running (stable refuses
  if the dev add-on `ha_mcp_webhook_proxy_dev` is running, and vice versa). Both flavors
  register the same root OAuth `/authorize` and `/token` routes, so only one may run at a
  time; the add-on now logs a clear error and raises a notification instead of colliding.
- Fail the OAuth integration setup loudly (a clear `ConfigEntryError`) if the other flavor
  already owns the root `/authorize` and `/token` views in this Home Assistant instance,
  instead of silently shadowing them — Home Assistant keeps those views bound until it
  restarts, even after the other add-on is stopped.
- Prompt for a Home Assistant restart when OAuth is enabled. Enabling OAuth needs a full
  HA restart to bind the root `/authorize`/`/token` views (restarting the add-on is not
  enough), so the integration now raises a Repair with a click-to-restart button and the
  option text says so. Disabling OAuth needs no restart.

### Fixed

- Correct the inbound-request debug-logging startup message: it claimed requests are
  logged to Home Assistant's log "NOT this addon log", but they are now mirrored into the
  add-on log too.
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
