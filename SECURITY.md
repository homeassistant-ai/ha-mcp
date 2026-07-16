# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| latest  | ✅        |

## Threat Model

Understanding what ha-mcp does and doesn't defend against helps you write
accurate reports and helps us triage quickly.

### MCP clients are trusted principals

An authenticated MCP client (one that has the secret URL or completed the OAuth
flow) is a **trusted principal**. ha-mcp does not attempt to defend against a
malicious or compromised MCP client — that is equivalent to defending against a
malicious user who has your Home Assistant long-lived access token.

Consequences:
- Prompt injection that reaches an MCP client is the **client's responsibility**
  to defend against. If the LLM embedded in a client is tricked into calling a
  tool it shouldn't, that is a client-side control issue, not a server-side
  vulnerability.
- `python_transform` expressions are treated as **trusted code from a trusted
  client**. The sandbox exists to prevent accidental mistakes (e.g. a runaway
  loop), not to sandbox an adversarial party. Additionally, `python_transform`
  only ever receives data that ha-mcp fetched from the HA API and
  JSON-deserialized — no Python callables can reach the expression's `config`
  variable through any normal MCP or HA API call. See
  [python_sandbox.py](src/ha_mcp/utils/python_sandbox.py) for the explicit
  "not a security boundary" note.

### Local network is the trusted zone for standard mode

The HTTP entrypoints (`ha-mcp-web`, `ha-mcp-sse`) authenticate by URL-path
secrecy and are designed for loopback HTTP or LAN HTTP with a high-entropy
`MCP_SECRET_PATH`. Any peer that can reach the configured path is treated as
trusted — securing the local network is outside ha-mcp's scope.

For internet-facing deployments use the OAuth entrypoint (`ha-mcp-oauth`) or,
for gating access behind an external identity provider instead of per-user HA
tokens, the OIDC entrypoint (`ha-mcp-oidc`; see [docs/oidc.md](docs/oidc.md)),
behind a TLS-terminating reverse proxy (see
[OAuth Mode](#oauth-mode--beta-warning) below). Deployment guidance:
[AGENTS.md → Docker](AGENTS.md#docker).

By default the HTTP entrypoints bind to `0.0.0.0` so they are reachable from
other machines on the LAN. To restrict to the local machine, set
`MCP_HOST=127.0.0.1` (or use `-p 127.0.0.1:8086:8086` at the Docker layer).

### Host/Origin validation (DNS-rebinding guard) is off by default

fastmcp ships a Host/Origin guard — a DNS-rebinding defense that only accepts
loopback `Host` headers and same-origin/loopback `Origin`s. ha-mcp defaults it
off (`FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=false`) across its Streamable-HTTP
entry points (`ha-mcp-web`, `ha-mcp-oauth`, `ha-mcp-oidc`, the add-on, and the
in-process component server). The supported
deployment model — reverse proxies, tunnels (Cloudflare, Nabu Casa), and direct
LAN access — presents `Host` headers ha-mcp cannot enumerate, and the guard
would otherwise reject them with `421`/`403` (including the plain browser landing
page, a no-`Origin` navigation that still trips the `Host` check).

This does not change the boundary defined above. URL-path secrecy (standard
mode), the OAuth / Home Assistant session gates (OAuth and in-process modes),
and the external identity provider gate (OIDC mode) remain the authentication
boundary, and the local network is already the trusted zone — so the
DNS-rebinding class this guard addresses is out of scope regardless. A
DNS-rebinding attacker's browser still cannot reach the secret MCP path — only
the public OAuth/OIDC discovery documents at fixed well-known paths (the
landing page shares the secret path) — and the loopback settings sidecar enforces
its own Host/Origin allow-list independent of this setting.

Operators who front ha-mcp differently can re-enable the guard by setting
`FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=true` and pinning
`FASTMCP_HTTP_ALLOWED_HOSTS` / `FASTMCP_HTTP_ALLOWED_ORIGINS`.

### Standard mode is single-tenant

The secret-URL model (`ha-mcp-web`, `ha-mcp-sse`) assumes a single operator.
All MCP clients that share the same `MCP_SECRET_PATH` get identical access —
there is no per-client authorization or isolation. Reports that assume "client A
shouldn't be able to see client B's data" don't apply to standard mode; that
isolation model only exists in OAuth mode (scoped per HA token).

### ha-mcp does not add or restrict Home Assistant permissions

ha-mcp uses the long-lived access token the operator provides. That token's
permissions in Home Assistant are what they are. If the configured token is an
admin token, ha-mcp can perform admin-level operations. Reports stating "ha-mcp
can do X" where X is permitted by the configured token are not vulnerabilities —
they are the intended behavior. Restricting HA permissions is done in Home
Assistant (e.g. by creating a non-admin user and generating a token for that
user).

### OAuth Bearer token design

In OAuth mode, access and refresh tokens are HMAC-signed, stateless Bearer
tokens. The token payload contains the user's Home Assistant long-lived access
token (LLAT). This is **by design**:

- The LLAT is the authorization boundary. Revoking it in Home Assistant
  immediately invalidates all derived tokens — that is the intended revocation
  path.
- Tokens are HMAC-signed (preventing forgery and tampering) but not encrypted.
  Encrypting the payload would not improve security: the LLAT must ultimately
  be sent to Home Assistant in cleartext over HTTPS to authenticate API calls.
  Anyone with access to the MCP server process can observe the LLAT regardless
  of token format.
- A party that captures a token can decode it to recover the LLAT. This is
  equivalent to capturing any other Bearer token that grants the same access —
  including the standard OAuth `client_credentials` model used by many MCP
  clients, where a static `client_secret` stored at the AI provider grants
  full service access. The trust boundary is identical; the only difference is
  packaging.
- Token revocation at the ha-mcp level is a no-op: there is no server-side
  token store. Revoke the LLAT in Home Assistant instead.

The consent form explains this revocation path. Reports about token opacity
(the LLAT being visible inside the token) will be closed as by-design.

### In-process server (`ha_mcp_tools` in-process server entry)

The `ha_mcp_tools` component's **in-process MCP server** config entry can run the
ha-mcp server in-process inside Home Assistant and expose it through a Home
Assistant webhook (see [docs/in-process-server.md](docs/in-process-server.md)).
It offers three authentication postures, selected by the **Authentication
mode** option in the entry options:

- **Secret webhook URL (default, `none`).** The webhook id is a high-entropy
  random string and *is* the credential — the same secret-URL trust model as
  standard mode above, except the URL is designed to be reached remotely through
  Home Assistant's own remote access (Nabu Casa or a TLS-terminating reverse
  proxy). Any party that has the full webhook URL is a trusted principal; keep
  the URL secret.
- **Home Assistant account (`ha_auth`).** Home Assistant Core is the OAuth
  authorization server: the entry serves the discovery documents and
  validates inbound Bearer tokens against Home Assistant's own auth, so access
  is gated by a Home Assistant login — and restricted to **administrator**
  users. The server acts with its own provisioned admin token (the caller's
  bearer is never forwarded), so accepting any valid login would grant every
  household member admin-equivalent control; non-admin, inactive, and
  system-generated users are rejected. This is distinct from the beta OAuth mode
  below — no bespoke authorization server or self-issued token is involved, and
  revoking the user's Home Assistant token/session revokes access.
- **Legacy OAuth (`legacy`).** A self-hosted OAuth 2.1 authorization server the
  component runs at the Home Assistant root (`/authorize` + `/token`), for
  OAuth-only MCP clients that HA Core's native OAuth cannot serve (Google Gemini
  Spark's cross-origin Client-ID-Metadata-Document redirect, GitHub Copilot
  CLI's dynamic registration). The credential is a **static `client_id` +
  `client_secret`** the component generates (or the admin overrides), plus a
  signing key — all persisted in the config entry. Its security properties:
  - **The client secret is the boundary.** Anyone holding the `client_id` +
    `client_secret` can complete the flow and mint tokens; there is no
    per-user identity. Access is **admin-equivalent** — the same provisioned
    admin token backs it as the other modes. Keep the secret secret.
  - **Self-issued Bearer tokens**, HMAC-signed and stateless, carrying
    `{kind, iat, exp, jti, cid}` — **no** Home Assistant LLAT (unlike the beta
    OAuth mode in "OAuth Bearer token design" above; that section's
    LLAT-revocation model does **not** apply here). Access tokens live 1 hour,
    refresh tokens 30 days.
  - **Revocation is rotation + restart.** Regenerating the credential or
    changing the `client_id`/`client_secret` override rotates the signing key,
    which invalidates every outstanding token — but only once Home Assistant
    restarts, because the root `/authorize`/`/token` views cannot be rebound
    without a restart (a repair issue prompts for it). Until that restart the
    previous credential keeps working; the startup log withholds the rotated
    credential during that window so a still-valid old token cannot read it.
  - **The consent endpoint is unauthenticated** (no HA session) — it is a
    plain human-approval page. This is safe because the authorization code is
    inert without the `client_secret` at the token endpoint (the client is
    authenticated before any code is redeemed) and PKCE S256 binds the code to
    the caller.
  - **Redirect URIs are validated to a spec floor, not exact-matched:** any
    `https://` URL (or `http://` loopback per RFC 8252, for CLI clients on
    variable ports) with a valid host/port and no fragment is accepted. There
    is deliberately **no** per-client redirect allowlist — the mode exists
    precisely for clients whose redirect URIs cannot be pre-registered (Spark's
    is cross-origin; Copilot CLI's loopback port varies). A permissive
    redirect is not exploitable for token theft here (the code is inert without
    the secret), so this is an accepted deviation from RFC 9700's exact-match
    guidance, scoped to this single-tenant self-hosted AS.
  - **TLS is required in practice** — the endpoints ride Home Assistant's own
    HTTP, so expose them only over HA's HTTPS remote access (Nabu Casa or a
    TLS-terminating reverse proxy), never plaintext over the internet.
  - **Route ownership:** the component and the Webhook Proxy add-on both bind
    the root `/authorize`/`/token`; only one may own them per Home Assistant
    instance. A cross-integration guard refuses to enable legacy mode (with a
    repair prompt) rather than clash silently.

The connect notification deliberately carries no secrets: Home Assistant
shows persistent notifications to every authenticated user, so the webhook
URL (the credential in the default posture) is surfaced only on
administrator-only surfaces - the entry's Configure screen, the sidebar
panel, and the log. A local-only option removes the webhook entirely.

The server reaches Home Assistant with a dedicated admin token the component
provisions and stores in the config entry. The token is handed to the server
in-memory (never through the Home Assistant process environment); removing the
entry revokes it, and disabling the config entry stops the server. As with
standard mode, that token's Home Assistant permissions define what the server can
do.

The component also adds an admin-only **settings panel** to the Home Assistant
sidebar that reverse-proxies the server's web settings UI over Home Assistant's
own HTTP. Because a browser cannot attach a Bearer token to a panel view, access
is gated by a short-lived, HttpOnly session cookie that an authenticated
**administrator** obtains through Home Assistant, and every proxied request
re-validates that the session still maps to an active admin. The loopback secret
path is never exposed to the browser and no token or secret is placed in a URL;
the proxy returns 503 whenever the server is not running.

## Scope

**In scope** — please report these:

- Authentication bypass in standard (LLAT), OAuth, or OIDC mode
- OAuth mode: XSS, SSRF, open redirect, or credential exfiltration via the
  consent form or token endpoint (i.e. an unauthenticated party obtaining a
  token or extracting credentials without completing the consent flow)
- Unintended information disclosure via API responses (e.g. secrets returned to
  a client that shouldn't have them)
- Privilege escalation within the MCP tool surface
- Dependency vulnerabilities with a credible exploit path

**Out of scope** — these will not be actioned:

- Vulnerabilities in Home Assistant itself →
  report to [home-assistant/core](https://github.com/home-assistant/core/security)
- Vulnerabilities in Nabu Casa or other remote access infrastructure
- Attacks requiring physical access to the HA host
- "The LLM performed a destructive action using valid, authorized tools" —
  this is a configuration or usage issue, not a security vulnerability.
  Tool visibility controls (`ENABLED_TOOL_MODULES`, group toggles) exist for
  this purpose.
- Prompt injection that only travels through read-only tool return values —
  the MCP client controls what the LLM sees and acts on; hardening that path
  is the client's responsibility (see [Threat Model](#threat-model) above).
- `python_transform` issues: the sandbox is not a security boundary between
  trusted and untrusted code. Problems with `python_transform` behavior are
  bugs, not security vulnerabilities.
- LAN-peer access to standard-mode HTTP endpoints: the local network is the
  trusted zone (see [Threat Model](#threat-model) above).
- DNS rebinding against the HTTP entrypoints: fastmcp's Host/Origin guard is off
  by default; URL-path secrecy is the boundary and the local network is the
  trusted zone (see [Threat Model](#threat-model) above).
- OAuth token containing an encoded LLAT: this is the Bearer token design
  (see [Threat Model](#threat-model) above).
- OAuth token revocation not preventing further HA API access: revoke the LLAT
  in Home Assistant instead.
- Saved custom tools (code mode, off by default) visible to other clients of the
  same server process: they are shared, persistent scaffolding — not per-user
  data — and `run_saved` executes with the caller's own HA token, granting no
  access the caller lacks. Any client that can reach ha-mcp is a trusted
  principal (see [Threat Model](#threat-model) above).
- Vulnerabilities that are only exploitable due to a misconfigured deployment
  (e.g., standard-mode instance exposed to the internet without TLS, or a
  network-reachable HTTP entrypoint using the default `MCP_SECRET_PATH`).

## OAuth Mode — Beta Warning

The OAuth consent-flow mode (`ha-mcp-oauth` entrypoint) is **experimental**
and carries a larger attack surface than the standard LLAT setup.

- Not recommended for production without TLS and network access restrictions
- Requires explicit opt-in (`ha-mcp-oauth`); the default entrypoint is unaffected
- CVEs were published and fixed in v7.x (XSS: GHSA-pf93-j98v-25pv;
  SSRF: GHSA-fmfg-9g7c-3vq7). Upgrade to the latest release before deploying.

If you choose to run OAuth mode, restrict the consent endpoint to trusted
networks and place it behind a TLS-terminating reverse proxy.

## Reporting a Vulnerability

Use the private reporting page at:
**https://github.com/homeassistant-ai/ha-mcp/security/advisories/new**

We aim to acknowledge reports within 7 days and to provide an initial
assessment shortly after. Remediation time depends on severity and complexity.
We practice coordinated disclosure and will agree a timeline with you,
typically within 90 days of the initial report. Severity is assessed using
CVSS base scores where applicable.

**Requirements for a valid report:**
- Reports must be made in good faith
- Demonstrate a real, reproducible issue with steps to reproduce
- Accurately reflect severity and impact — overstated reports are deprioritized
- Low-quality or AI-generated submissions without a working proof of concept
  will be closed without action
