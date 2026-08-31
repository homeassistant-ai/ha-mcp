# CHANGELOG

<!-- version list -->

<!--
Dev channel: forked from the stable Webhook Proxy add-on at v1.2.2 and versioned
independently (1.2.2.dev1, 1.2.2.dev2, …). The entries below are inherited stable
history from before the fork.
-->


## v3.0.3.dev2 (2026-08-30)

### Security

- The webhook id is no longer published by any anonymous discovery URL. The
  fixed-path protected-resource document (`/api/mcp_proxy_dev/oauth/protected-resource`)
  handed the full webhook URL to any unauthenticated GET while `ha_auth` or
  `legacy` mode was on; it is removed. The webhook's 401 challenge now points at
  the RFC 9728 path-scoped document, whose URL already contains the id.
- After the operator rotates the webhook id (`/data/webhook_id.txt`), the old
  id's discovery URL stops answering immediately instead of serving the new id
  until the next Home Assistant restart. A restart Repair is raised so the new
  id's discovery URL gets bound; existing installs that ran `ha_auth` or `legacy`
  reachable from the internet before this version may wish to rotate once.

### Bug Fixes

- `ha_auth`: percent-encode the query forwarded to core's `/auth/authorize`, so a
  native-app client's loopback callback is not rejected by reverse proxies that
  block `=http://` in query strings (Nginx Proxy Manager "Block Common Exploits").


## v3.0.1.dev1 (2026-08-16)

Version line rebased onto the 3.0.0 stable base by the promote PR
(`rebase_dev_version`). The OAuth surface overhaul developed on this channel as
2.1.1.dev4 and .dev5 shipped in stable 3.0.0 — see the stable CHANGELOG for the
user-facing notes; dev and stable are code-identical as of that promotion.


## v2.1.1.dev3 (2026-07-31)

Internal: lint cleanup (ruff pylint rules) — no behavior change.


## v2.1.1.dev2 (2026-07-30)

Documentation: warn Tailscale Funnel users that Claude.ai connectors require the
standard HTTPS port 443 (#2080).


## v2.1.1.dev1 (2026-07-26)

Version line rebased onto the 2.1.0 stable base (no code changes — dev and
stable are identical as of the 2.1.0 promotion). Future promotions carry this
rebase inside the promote PR itself (`rebase_dev_version`).


## v2.0.5.dev3 (2026-07-26)

### Bug Fixes

- Drop the 5-minute wall-clock `total` timeout from the relay's HTTP client so a
  long-lived MCP response stream (the upcoming spec's `subscriptions/listen`) is
  no longer cut every 300 s, forcing the client to re-subscribe. The `sock_read`
  idle timeout still bounds a dead stream, and a new finite `connect` bound
  keeps connection-pool acquisition from hanging new requests when long-lived
  streams occupy the pool.


## v2.0.5.dev2 (2026-07-25)

### Features

- Authorization responses from the add-on's own OAuth servers (the legacy
  `/authorize` view and the none-mode auto-approve `/authorize` view) now carry
  the RFC 9207 `iss` parameter naming the issuer that produced them, on both the
  success redirect and the error redirects. The value is the same issuer the
  add-on's RFC 8414 metadata document advertises. Clients that do not implement
  RFC 9207 ignore the extra query parameter, so existing connectors are
  unaffected.


## v2.0.5.dev1 (2026-07-20)

### Bug Fixes

- Rebase the dev channel onto the 2.0.5 stable base and fold in the #1978
  post-merge parity fixes now shipping in stable: the none-mode auto-approve
  OAuth error responses carry `Cache-Control: no-store` / `Pragma: no-cache`
  (matching the token responses and the custom-component twin), and the
  `_active_oauth_mode` docstring documents its `none_autoapprove` return value.


## v2.0.3.dev3 (2026-07-19)

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


## v2.0.3.dev2 (2026-07-18)

### Refactoring

- Reduce cyclomatic complexity in `start.py` and `mcp_proxy_dev/__init__.py`
  below the C901 threshold by extracting private helpers (issue #925). No
  behavior change.


## v2.0.3.dev1 (2026-07-18)

Version line re-based onto the stable series (stable is 2.0.2, so dev now
leads it as 2.0.3.devN); the 1.2.3.devN entries below predate this rule.

### Bug Fixes

- Write the proxy-config handoff file with restricted (0600) permissions like
  the OAuth creds file, falling back to a plain write with a logged warning
  when the filesystem cannot honor the mode.


## v1.2.3.dev6 (2026-07-05)

### Documentation

- Note that this proxy is unnecessary with the HA-MCP custom component's
  in-process server, which has its own built-in webhook for remote access.
  The proxy remains for the MCP Server add-on (and, via the
  `mcp_server_url` option, other external servers).


## v1.2.3.dev5 (2026-07-04)

### Added

- ha_auth debug observability: with debug logging enabled, a 401 on the webhook
  now logs WHY the bearer was rejected — no usable bearer, token rejected by
  Home Assistant's validator, or the validator raised — so provider-specific
  login issues (issue #1714's OIDC leg) are diagnosable from the add-on log
  alone. The token itself is never logged.


## v1.2.3.dev4 (2026-07-02)

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


## v1.2.3.dev3 (2026-07-02)

### Added

- Click-to-restart Repair (HACS-style) now appears the moment a restart is
  needed, not only at the next HA boot: the integration registers a
  `refresh_repairs` service the add-on calls when the integration files
  were updated on disk (new `update_restart_required` issue) or OAuth was
  enabled against stale loaded code (`oauth_restart_required`, which
  previously surfaced only after the very restart it prompts for). The
  stale "integration updated" notification is auto-dismissed once the new
  code actually loads.

### Fixed

- Repair cards now render with proper text: the integration shipped only
  `strings.json`, but custom integrations load runtime translations from
  `translations/en.json`, which was missing.

### Documentation

- Add a "Cloudflare users" troubleshooting section to DOCS.md: disable
  "Block AI training bots" and don't geo-block your AI provider's US IP
  ranges (Claude.ai connects from Anthropic's network, `160.79.104.0/21`)


## v1.2.3.dev2 (2026-07-02)

### Added

- Serve the OAuth metadata at the RFC 8414 / RFC 9728 / OIDC well-known
  locations (issue #1714): the authorization-server document at
  `/.well-known/oauth-authorization-server/api/mcp_proxy_dev/oauth` (plus the
  `openid-configuration` variants), and the protected-resource document at the
  path-scoped `/.well-known/oauth-protected-resource/api/webhook/<id>`.
  Captured live against claude.ai: the path-scoped document is its first
  fallback probe when the 401's `WWW-Authenticate` pointer is missing, and a
  valid authorization-server document at the well-known path overrides a
  previously mis-cached (HA-core) per-URL client config — healing a broken
  connector with no client-side action. Purely additive routes; HA core's
  root well-known endpoints are untouched.


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
