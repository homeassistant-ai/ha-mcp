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

For internet-facing deployments use the OAuth entrypoint (`ha-mcp-oauth`)
behind a TLS-terminating reverse proxy (see
[OAuth Mode](#oauth-mode--beta-warning) below). Deployment guidance:
[AGENTS.md → Docker](AGENTS.md#docker).

By default the HTTP entrypoints bind to `0.0.0.0` so they are reachable from
other machines on the LAN. To restrict to the local machine, set
`MCP_HOST=127.0.0.1` (or use `-p 127.0.0.1:8086:8086` at the Docker layer).

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

## Scope

**In scope** — please report these:

- Authentication bypass in standard (LLAT) or OAuth mode
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
- OAuth token containing an encoded LLAT: this is the Bearer token design
  (see [Threat Model](#threat-model) above).
- OAuth token revocation not preventing further HA API access: revoke the LLAT
  in Home Assistant instead.
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

Reports are assessed within 48 hours; fixes may take an additional 24–48 hours.
We aim for coordinated disclosure and will work with you to agree on a
disclosure timeline, typically within 90 days of the initial report.
Severity is assessed using CVSS base scores where applicable.

**Requirements for a valid report:**
- Reports must be made in good faith
- Demonstrate a real, reproducible issue with steps to reproduce
- Accurately reflect severity and impact — overstated reports are deprioritized
- Low-quality or AI-generated submissions without a working proof of concept
  will be closed without action
