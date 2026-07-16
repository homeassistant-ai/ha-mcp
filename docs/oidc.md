# OIDC Authentication for ha-mcp

OIDC mode gates access to the MCP server behind an external identity provider
(Authentik, Keycloak, Auth0, Google, etc.). Unlike [OAuth mode](OAUTH.md),
which collects a per-user Home Assistant Long-Lived Access Token via a consent
form, OIDC is purely an **access gate**: once a user authenticates through
your OIDC provider, all requests share the same Home Assistant credentials
configured on the server (`HOMEASSISTANT_TOKEN`).

## When to Use OIDC

**Use OIDC if you want:**
- Access controlled by an identity provider you already run (SSO, MFA, group
  policies) instead of a secret URL
- Every authenticated user to share one Home Assistant identity — there is no
  per-user HA authorization or isolation

**Use the private URL (secret-path) method if you want:**
- Simpler setup with no external identity provider (recommended for most users)

**Use OAuth mode instead if you want:**
- Per-user Home Assistant credentials rather than a shared server-side token

> **Note:** OIDC and OAuth both authenticate the *user*; only OAuth mode
> changes *which* Home Assistant credentials a request uses.

---

## Running

Start the server with the `ha-mcp-oidc` entrypoint instead of `ha-mcp-web`:

**Docker:**
```bash
docker run -d --name ha-mcp-oidc \
  -p 8086:8086 \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your-long-lived-access-token \
  -e OIDC_CONFIG_URL=https://auth.example.com/application/o/ha-mcp/.well-known/openid-configuration \
  -e OIDC_CLIENT_ID=ha-mcp-client \
  -e OIDC_CLIENT_SECRET=your-client-secret \
  -e MCP_BASE_URL=https://mcp.example.com \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-oidc
```

**uvx:**
```bash
export HOMEASSISTANT_URL=http://homeassistant.local:8123
export HOMEASSISTANT_TOKEN=your-long-lived-access-token
export OIDC_CONFIG_URL=https://auth.example.com/application/o/ha-mcp/.well-known/openid-configuration
export OIDC_CLIENT_ID=ha-mcp-client
export OIDC_CLIENT_SECRET=your-client-secret
export MCP_BASE_URL=https://mcp.example.com
uvx --from=ha-mcp@latest ha-mcp-oidc
```

The server must be reachable over HTTPS at `MCP_BASE_URL` — put a
TLS-terminating reverse proxy or tunnel in front of it (the same requirement
as [OAuth mode](OAUTH.md#1-expose-with-https)).

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HOMEASSISTANT_URL` | **Required.** URL of the Home Assistant instance | None |
| `HOMEASSISTANT_TOKEN` | **Required.** Shared Home Assistant long-lived access token (or supervisor token) used for every authenticated request | None |
| `OIDC_CONFIG_URL` | **Required.** OIDC discovery URL (`.well-known/openid-configuration`) | None |
| `OIDC_CLIENT_ID` | **Required.** OAuth client ID registered with your OIDC provider | None |
| `OIDC_CLIENT_SECRET` | **Required.** OAuth client secret from your OIDC provider | None |
| `MCP_BASE_URL` | **Required.** Public HTTPS URL where this server is accessible | None |
| `MCP_PORT` | Server port | `8086` |
| `MCP_SECRET_PATH` | MCP endpoint path | `/mcp` |
| `OIDC_JWT_SIGNING_KEY` | Optional. Secret key for signing FastMCP session JWTs. Set this to persist sessions across server restarts — without it, a new random key is generated on every startup and all sessions are invalidated. Generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"` | Derived from `OIDC_CLIENT_SECRET` |
| `OIDC_ALLOWED_CLIENT_REDIRECT_URIS` | Optional but **strongly recommended for internet-facing deployments**. Comma-separated list of redirect URI patterns accepted from dynamically-registered clients. With open dynamic client registration (DCR) and an allow-all redirect policy, a malicious dynamically-registered client can ride a victim's IdP session. | Unset (allow-all) |
| `OIDC_VERIFY_ID_TOKEN` | Optional. Set `true` for OIDC providers that issue opaque access tokens the default JWT verifier can't validate (e.g. Google always; Auth0 without an API audience configured). | `false` |
| `LOG_LEVEL` | Logging level | `INFO` |

## IdP Client Registration

When registering ha-mcp as an OAuth/OIDC application with your provider:

- **Redirect URI:** `<MCP_BASE_URL>/auth/callback`
- **Grant type:** Authorization Code
- **Token endpoint auth method:** **Client Secret Basic.** ha-mcp's OIDC
  client (via authlib) does not pass `token_endpoint_auth_method`, so it uses
  authlib's default, which is Client Secret Basic — not Client Secret Post.
  Providers whose client is locked to `client_secret_post` will fail the
  authorization code exchange; set the client to Client Secret Basic (or
  "Basic Auth") in your provider's application settings.

Example discovery URLs:
- Authentik: `https://auth.example.com/application/o/<app-slug>/.well-known/openid-configuration`
- Keycloak: `https://keycloak.example.com/realms/<realm>/.well-known/openid-configuration`
- Auth0: `https://<tenant>.auth0.com/.well-known/openid-configuration`
- Google: `https://accounts.google.com/.well-known/openid-configuration`

## Provider Compatibility

OIDC mode works out of the box with providers that issue **JWT access
tokens** — Authentik and Keycloak are known to work without extra
configuration.

Providers that issue **opaque access tokens** (not JWTs) need
`OIDC_VERIFY_ID_TOKEN=true` so FastMCP verifies the ID token instead of the
access token:
- **Google** always issues opaque access tokens.
- **Auth0** issues opaque access tokens unless the client requests a
  configured API audience.

## Connecting Claude.ai

Once OIDC is configured:

1. In Claude.ai, go to **Settings > Connectors > Add custom connector**
2. Enter the MCP endpoint URL: `https://mcp.example.com/mcp`
3. Claude.ai discovers the OIDC endpoints automatically
4. You're redirected to your OIDC provider to authenticate
5. After authentication, Claude.ai can access your Home Assistant

---

**Back to:** [Main Documentation](../README.md)
