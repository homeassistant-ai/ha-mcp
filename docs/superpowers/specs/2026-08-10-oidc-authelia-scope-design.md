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

Authelia issues opaque access tokens by default and only returns an ID token
when `openid` is requested. Consequently:

- Default FastMCP verification attempts to parse the opaque access token as a
  JWT and rejects the first MCP request with `invalid_token`.
- `OIDC_VERIFY_ID_TOKEN=true` cannot help because the token response contains
  no ID token to verify.
- The preceding `/token` request still succeeds because FastMCP issues its
  local reference JWT before it revalidates the stored upstream token on the
  first `/mcp` request.

The sparse diagnostics are a separate logging integration defect. FastMCP's
`fastmcp` logger has its own handler and does not propagate to the root logger,
so ha-mcp's root `LOG_LEVEL` configuration does not change FastMCP's effective
level.

## Design

### OIDC scope

Pass `required_scopes=["openid"]` whenever `ha-mcp-oidc` constructs
`OIDCProxy`. This entrypoint promises OIDC rather than generic OAuth, so
`openid` is a protocol requirement, not an optional provider-specific
permission. FastMCP will advertise the scope to MCP clients, include it in the
upstream authorization request, and retain it for FastMCP-token enforcement.

Keep `OIDC_VERIFY_ID_TOKEN` opt-in. JWT-access-token providers continue using
the default verification path. Opaque-access-token providers use ID-token
verification after the now-guaranteed `openid` request causes them to return
an ID token.

### Logging

Extend the common `_setup_logging` helper to set the `fastmcp` logger to the
same level as ha-mcp's root logger. This preserves FastMCP's existing handler
and formatting while making `LOG_LEVEL` behave consistently for every ha-mcp
entrypoint. It also avoids requiring operators to discover and duplicate the
setting as `FASTMCP_LOG_LEVEL`.

### Documentation

Update `docs/oidc.md` to:

- state that ha-mcp always requests the required `openid` scope;
- add Authelia to opaque-access-token provider compatibility guidance;
- tell Authelia users to allow `openid` and set
  `OIDC_VERIFY_ID_TOKEN=true`;
- clarify that they do not need to enable JWT access tokens in Authelia; and
- state that `LOG_LEVEL` covers FastMCP/OIDC diagnostics.

The guidance applies to the Docker/standalone `ha-mcp-oidc` entrypoint only.
It does not alter the Home Assistant add-on, custom component, or OAuth mode's
authentication design.

## Testing

Follow red-green TDD with unit-level regression coverage in
`tests/src/unit/test_oidc_entrypoint.py`:

1. Extend the pinned `OIDCProxy` mock and real-signature subset assertion for
   `required_scopes`.
2. Add a failing test proving every OIDC proxy construction receives exactly
   `["openid"]`.
3. Add a failing test proving `_setup_logging("DEBUG")` lowers the effective
   FastMCP logger level to DEBUG despite its non-propagating handler.
4. Implement the minimum production changes and rerun the focused tests.
5. Run `uv run ruff format --check` and `uv run ruff check` on the touched
   Python files, `uv run mypy src/`, and the full OIDC unit file. There is no
   OIDC E2E test or live-IdP fixture in `tests/src/e2e/`; the pull request's
   ordinary CI still runs the repository-wide E2E lanes. Before publishing,
   prove each regression test fails with its production fix temporarily
   reverted, then restore the fixes and rerun the tests successfully.

No live Authelia instance or Home Assistant state is required: the defect is
the deterministic constructor and logging configuration passed to FastMCP.

## Non-goals

- Changing Authelia's access-token format or requiring RFC 9068 JWT access
  tokens.
- Adding token introspection support to FastMCP.
- Changing `OIDC_AUDIENCE`, resource forwarding, redirect URI policy, or token
  endpoint authentication.
- Modifying add-on or custom-component authentication.
- Changing FastMCP itself.
