# OIDC Authelia scope and diagnostics fix (closes #2189)

**Status:** approved 2026-08-10
**Issue:** [#2189](https://github.com/homeassistant-ai/ha-mcp/issues/2189)
**Branch:** `agent/fix-oidc-authelia` (worktree `worktree/fix-oidc-authelia/`)

## Goal

Make the standalone/container `ha-mcp-oidc` entrypoint interoperate with
OIDC providers such as Authelia that issue opaque access tokens, and make
`LOG_LEVEL=DEBUG` expose FastMCP's token-validation diagnostics.

## Root cause

The OIDC entrypoint constructs FastMCP's `OIDCProxy` without
`required_scopes`. `fastmcp-remote` does not supply scopes itself; the MCP
client selects them from the protected-resource metadata FastMCP derives from
`required_scopes`. The resulting authorization request therefore has no
`openid` scope.

FastMCP 3.4.6 also copies `required_scopes` into DCR's `default_scopes` and
`valid_scopes`. Passing `required_scopes=["openid"]` alone would fix the missing
ID token but make the MCP SDK reject an explicit request such as
`openid profile email offline_access` before the identity provider sees it.
Copying every discovery-advertised provider scope into `valid_scopes` is not a
safe alternative: FastMCP also publishes that list in protected-resource
metadata, and ordinary MCP clients may then request every advertised scope.

Authelia issues opaque access tokens by default and only returns an ID token
when `openid` is requested. Consequently:

- Default FastMCP verification attempts to parse the opaque access token as a
  JWT and rejects the first MCP request with `invalid_token`.
- `OIDC_VERIFY_ID_TOKEN=true` cannot help because the token response contains
  no ID token to verify.
- The preceding `/token` request still succeeds because FastMCP issues its
  local reference JWT before it revalidates the stored upstream token on the
  first `/mcp` request.

The sparse diagnostics are a separate logging integration defect. When FastMCP
logging is enabled, its `fastmcp` logger has its own handler and does not
propagate to the root logger, so ha-mcp's root `LOG_LEVEL` configuration does
not change FastMCP's effective level. When `FASTMCP_LOG_ENABLED=false`, FastMCP
instead leaves that namespace unconfigured; ha-mcp must not let it inherit the
root handler and accidentally restore the framework logs.

## Design

### OIDC scope and DCR compatibility

Add `ha_mcp.auth.oidc_compat.HaMcpOIDCProxy`, a narrowly scoped FastMCP 3.4.6
compatibility subclass that inherits `OIDCProxy.__init__` unchanged. Pass
`required_scopes=["openid"]` whenever `ha-mcp-oidc` constructs this proxy, then
call its explicit compatibility setup method. This entrypoint promises OIDC
rather than generic OAuth, so `openid` is a protocol requirement, not an
optional provider-specific permission.

The setup method verifies that `required_scopes`, DCR `default_scopes`, and
FastMCP's upstream default scope string still contain only `openid`, then sets
DCR `valid_scopes` to `None`. This keeps `openid` required, defaulted, advertised
in protected-resource metadata, included in upstream authorization, and
enforced on FastMCP tokens. At the same time, the MCP SDK no longer treats
`openid` as a complete allow-list: a client can explicitly request optional
scopes such as `profile`, `email`, or `offline_access`, and the upstream IdP's
client and user policy remains responsible for accepting or rejecting them.
Optional provider scopes are not advertised automatically, so ordinary MCP
clients do not request every scope from discovery metadata.

Normalize both boundaries that can otherwise omit the required scope. During
DCR, retain every explicitly requested optional scope and add `openid` when the
client supplied only optional scopes. Independently union `openid` into every
authorization request before FastMCP stores the transaction or builds the
upstream IdP URL. Registration normalization alone is insufficient because a
legacy or unusual client can still send authorization parameters that omit a
scope present in its registration. Together these guards ensure the upstream
request always contains `openid` without discarding client-requested optional
scopes.

Keep `OIDC_VERIFY_ID_TOKEN` opt-in. JWT-access-token providers continue using
the default verification path. Opaque-access-token providers use ID-token
verification after the now-guaranteed `openid` request causes them to return
an ID token.

### Persisted state compatibility

Override persisted-client loading narrowly in the compatibility subclass.
When a pre-fix DCR registration lacks a required scope, add `openid` while
preserving its existing optional scopes and persist the migrated registration.
Do not write when the registration is already compliant. This lazy migration
retains the client ID, secret, redirect URIs, and other DCR state.

After FastMCP loads a refresh token, reject it if it lacks `openid`. A token
created before the fix must not perpetuate a session without the required
scope, so the client performs authorization once and receives a compliant
token while keeping its migrated DCR registration. Most clients restart that
flow automatically. Manual client-side connector or authorization-cache
clearing is only a fallback for clients that do not restart authorization after
the rejected refresh token.

### Logging

Extend the common `_setup_logging` helper to set the `fastmcp` logger to the
same level as ha-mcp's root logger when FastMCP logging is enabled. This
preserves FastMCP's existing handler and formatting while making `LOG_LEVEL`
behave consistently for every ha-mcp entrypoint. When
`FASTMCP_LOG_ENABLED=false`, set the FastMCP namespace above `CRITICAL` so its
otherwise unconfigured, propagating child loggers cannot inherit ha-mcp's root
handler. This preserves the upstream opt-out and avoids requiring operators to
duplicate `LOG_LEVEL` as `FASTMCP_LOG_LEVEL`.

This logging bridge is intentionally common behavior: every ha-mcp entrypoint
that calls `_setup_logging` receives the FastMCP level synchronization and
disabled-logging protection. It is not part of the OIDC-only authentication
boundary and does not change any entrypoint's authentication model.

### Documentation

Update `docs/oidc.md` to:

- state that `openid` remains required, default, and advertised, is added to
  optional-only registrations and every authorization request, while retained
  client-requested optional scopes remain subject to upstream IdP policy;
- add Authelia to opaque-access-token provider compatibility guidance;
- tell Authelia users to allow `openid` and set
  `OIDC_VERIFY_ID_TOKEN=true`;
- clarify that they do not need to enable JWT access tokens in Authelia;
- explain lazy DCR migration, one-time reauthorization for legacy refresh
  tokens, retained registrations, and manual cache clearing as a fallback; and
- state that `LOG_LEVEL` covers enabled FastMCP/OIDC diagnostics while
  `FASTMCP_LOG_ENABLED=false` still disables FastMCP logging entirely.

The OIDC scope, persisted-state, and operator-configuration guidance applies
to the Docker/standalone `ha-mcp-oidc` entrypoint only. It does not alter the
Home Assistant add-on, custom component, or OAuth mode's authentication design.
The common logging bridge described above separately applies to every
entrypoint that uses `_setup_logging`.

## Testing

Follow red-green TDD with entrypoint coverage in
`tests/src/unit/test_oidc_entrypoint.py` and focused compatibility coverage in
`tests/src/unit/test_oidc_compat.py`:

1. Extend the pinned `OIDCProxy` mock and real-signature subset assertion for
   `required_scopes` and the explicit compatibility setup call.
2. Before adding `src/ha_mcp/auth/oidc_compat.py`, collect the new focused test
   file and require the import failure; after adding a no-op setup method,
   require the scope-setup assertion to fail with DCR `valid_scopes` still set
   to `["openid"]`.
3. Exercise real FastMCP/MCP behavior to prove optional multi-scope DCR is
   accepted, omitted scopes default to `openid`, optional-only DCR retains its
   scopes while adding `openid`, protected-resource metadata advertises only
   `openid`, and an optional-only authorization request reaches the upstream
   IdP with both `openid` and the requested optional scopes.
4. Prove a persisted legacy registration is migrated and written exactly once,
   a compliant registration is not rewritten, a legacy refresh token without
   `openid` is rejected, and a compliant refresh token remains usable.
5. Add a failing test proving every OIDC proxy construction receives exactly
   `["openid"]` and invokes compatibility setup once.
6. Add a failing test proving `_setup_logging("DEBUG")` lowers the effective
   FastMCP logger level to DEBUG despite its non-propagating handler.
7. Add a behavioral regression test proving `FASTMCP_LOG_ENABLED=false` keeps
   a FastMCP child record out of ha-mcp's root handler.
8. Implement the compatibility subclass, entrypoint integration, and logging
   changes, then rerun both focused OIDC unit modules.
9. Run `uv run ruff format --check` and `uv run ruff check` on all touched
   Python files, `uv run mypy src/`, and both OIDC unit files. There is no live
   OIDC/IdP fixture in `tests/src/e2e/`, so do not invent a local OIDC E2E
   command. Require terminal success from the pull request's full `E2E Tests`
   workflow and every applicable `HAOS E2E Tests` lane, including the embedded
   and in-addon lanes when scheduled. Do not report verification complete until
   those CI lanes pass. Before publishing, retain the recorded red evidence,
   restore any safe temporary regression mutations, and rerun the focused tests
   successfully.

No live Authelia instance or Home Assistant state is required: the defect is
the deterministic proxy, persisted-state, and logging behavior around FastMCP.

## Non-goals

- Changing Authelia's access-token format or requiring RFC 9068 JWT access
  tokens.
- Adding token introspection support to FastMCP.
- Changing `OIDC_AUDIENCE`, resource forwarding, redirect URI policy, or token
  endpoint authentication.
- Advertising every discovery-provided scope or bypassing the upstream IdP's
  optional-scope policy.
- Modifying add-on or custom-component authentication.
- Changing FastMCP itself.
