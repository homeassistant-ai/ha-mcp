# CHANGELOG

<!-- version list -->


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
